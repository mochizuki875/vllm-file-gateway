from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from gateway.config import Settings
from gateway.converter import ConversionError, convert_document, media_type_for, sha256_file
from gateway.database import Database, StoredFile
from gateway.errors import GatewayError


@dataclass(frozen=True)
class ResolvedDocument:
    filename: str
    derived_dir: Path
    manifest: dict[str, object]


class FileService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.database.create_schema()
        for file_id in self.database.pending():
            await self.queue.put(file_id)
        self.tasks = [
            asyncio.create_task(self._worker(), name="conversion-worker"),
            asyncio.create_task(self._janitor(), name="file-janitor"),
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def create_file(
        self,
        filename: str,
        media_type: str,
        chunks: list[bytes],
        purpose: str,
        tenant_id: str,
    ) -> StoredFile:
        safe_name = Path(filename).name
        extension = Path(safe_name).suffix.lower()
        if extension not in {".pdf", ".pptx", ".docx", ".xlsx"}:
            raise GatewayError(400, "unsupported_file_type", "Unsupported file type.", "file")
        content_size = sum(len(chunk) for chunk in chunks)
        if content_size > self.settings.max_file_bytes:
            raise GatewayError(400, "file_too_large", "File exceeds the 50 MiB limit.", "file")
        file_id = f"file_{uuid.uuid4().hex}"
        relative_dir = Path("files") / tenant_id / file_id
        file_dir = self.settings.gateway_data_dir / relative_dir
        file_dir.mkdir(parents=True)
        source = file_dir / f"source{extension}"
        digest = hashlib.sha256()
        with source.open("wb") as output:
            for chunk in chunks:
                output.write(chunk)
                digest.update(chunk)
        validate_signature(source, extension)
        created_at = int(time.time())
        record = StoredFile(
            id=file_id,
            tenant_id=tenant_id,
            filename=safe_name,
            media_type=media_type_for(extension),
            purpose=purpose,
            byte_size=content_size,
            sha256=digest.hexdigest(),
            status="uploaded",
            source_path=str(relative_dir / source.name),
            manifest_path=None,
            created_at=created_at,
            expires_at=created_at + self.settings.file_ttl_seconds,
        )
        self.database.add(record)
        await self.queue.put(file_id)
        return record

    async def _worker(self) -> None:
        while True:
            file_id = await self.queue.get()
            try:
                await asyncio.to_thread(self._convert, file_id)
            finally:
                self.queue.task_done()

    def _convert(self, file_id: str) -> None:
        with self.database.sessions() as session:
            record = session.get(StoredFile, file_id)
            if not record or record.deleted_at is not None:
                return
            source = self.settings.gateway_data_dir / record.source_path
            file_dir = source.parent
        self.database.update_status(file_id, "processing")
        try:
            convert_document(source, file_dir / "derived")
            manifest_path = str(Path(record.source_path).parent / "manifest.json")
            self.database.update_status(file_id, "processed", manifest_path=manifest_path)
        except Exception as error:
            self.database.update_status(file_id, "failed", error_message=str(error)[:1000])

    async def _janitor(self) -> None:
        while True:
            for record in self.database.expired():
                self.delete(record.id, record.tenant_id)
            await asyncio.sleep(30)

    def delete(self, file_id: str, tenant_id: str) -> bool:
        record = self.database.mark_deleted(file_id, tenant_id)
        if not record:
            return False
        source = self.settings.gateway_data_dir / record.source_path
        shutil.rmtree(source.parent, ignore_errors=True)
        return True

    def resolve_stored(self, file_id: str, tenant_id: str, param: str) -> ResolvedDocument:
        record = self.database.get(file_id, tenant_id)
        if not record:
            raise GatewayError(404, "file_not_found", "File not found.", param)
        if record.status in {"uploaded", "processing"}:
            raise GatewayError(409, "file_not_ready", "The file is still being processed.", param)
        if record.status == "failed":
            raise GatewayError(422, "file_processing_failed", "File processing failed.", param)
        if not record.manifest_path:
            raise GatewayError(422, "file_processing_failed", "File manifest is missing.", param)
        manifest_path = self.settings.gateway_data_dir / record.manifest_path
        return ResolvedDocument(
            record.filename,
            manifest_path.parent / "derived",
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )


class DocumentResolver:
    def __init__(self, settings: Settings, files: FileService) -> None:
        self.settings = settings
        self.files = files

    async def resolve(
        self,
        reference: dict[str, object],
        tenant_id: str,
        param: str,
        temporary_directories: list[tempfile.TemporaryDirectory[str]],
    ) -> ResolvedDocument:
        sources = [key for key in ("file_id", "file_data", "file_url") if reference.get(key)]
        if len(sources) != 1:
            raise GatewayError(400, "invalid_file_reference", "Specify exactly one file source.", param)
        if sources[0] == "file_id":
            return self.files.resolve_stored(str(reference["file_id"]), tenant_id, f"{param}.file_id")
        temporary = tempfile.TemporaryDirectory(prefix="gateway-request-", dir=self.settings.gateway_data_dir / "work")
        temporary_directories.append(temporary)
        root = Path(temporary.name)
        if sources[0] == "file_data":
            filename = require_filename(reference, param)
            content = decode_file_data(str(reference["file_data"]), param)
        else:
            content, filename = await self._download(str(reference["file_url"]), param)
            filename = str(reference.get("filename") or filename)
        if len(content) > self.settings.max_file_bytes:
            raise GatewayError(400, "file_too_large", "File exceeds the 50 MiB limit.", param)
        extension = Path(filename).suffix.lower()
        source = root / f"source{extension}"
        source.write_bytes(content)
        validate_signature(source, extension)
        try:
            convert_document(source, root / "derived")
        except ConversionError as error:
            code = "too_many_pages" if "20 page" in str(error) else "file_processing_failed"
            raise GatewayError(400, code, str(error), param) from error
        return ResolvedDocument(
            Path(filename).name,
            root / "derived",
            json.loads((root / "manifest.json").read_text(encoding="utf-8")),
        )

    async def _download(self, url: str, param: str) -> tuple[bytes, str]:
        current = url
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            for _ in range(4):
                await validate_public_https_url(current, param)
                async with client.stream("GET", current, headers={"Accept": "*/*"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            break
                        current = urljoin(current, location)
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise GatewayError(400, "invalid_file_url", "Unable to download file URL.", param)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_file_bytes:
                            raise GatewayError(400, "file_too_large", "Downloaded file is too large.", param)
                        chunks.append(chunk)
                    name = Path(unquote(urlsplit(current).path)).name or "download.pdf"
                    return b"".join(chunks), name
        raise GatewayError(400, "invalid_file_url", "Too many redirects.", param)


def document_parts(
    document: ResolvedDocument,
    input_kind: str,
    max_images: int,
) -> list[dict[str, object]]:
    pages = document.manifest["documents"][0]["pages"]  # type: ignore[index]
    parts: list[dict[str, object]] = []
    for page in pages:  # type: ignore[union-attr]
        number = page["page_number"]
        text = (document.derived_dir / page["text_path"]).read_text(encoding="utf-8")
        label = f'<document filename="{document.filename}" page="{number}">\n{text}\n</document>'
        if input_kind == "responses":
            parts.append({"type": "input_text", "text": label})
            if number <= max_images:
                image = base64.b64encode((document.derived_dir / page["image_path"]).read_bytes()).decode("ascii")
                parts.append(
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": f"data:image/webp;base64,{image}",
                    }
                )
        else:
            parts.append({"type": "text", "text": label})
            if number <= max_images:
                image = base64.b64encode((document.derived_dir / page["image_path"]).read_bytes()).decode("ascii")
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{image}"}}
                )
    return parts


def require_filename(reference: dict[str, object], param: str) -> str:
    filename = Path(str(reference.get("filename") or "")).name
    if not filename:
        raise GatewayError(400, "invalid_file_data", "filename is required for file_data.", f"{param}.filename")
    return filename


def decode_file_data(value: str, param: str) -> bytes:
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise GatewayError(400, "invalid_file_data", "file_data is not valid base64.", f"{param}.file_data") from error


async def validate_public_https_url(url: str, param: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise GatewayError(400, "invalid_file_url", "Only public HTTPS URLs on port 443 are allowed.", param)
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise GatewayError(400, "invalid_file_url", "File URL hostname cannot be resolved.", param) from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise GatewayError(400, "invalid_file_url", "File URL resolves to a non-public address.", param)


def validate_signature(path: Path, extension: str) -> None:
    if extension not in {".pdf", ".pptx", ".docx", ".xlsx"}:
        path.unlink(missing_ok=True)
        raise GatewayError(400, "unsupported_file_type", "Unsupported file type.", "file")
    signature = path.read_bytes()[:8]
    valid = signature.startswith(b"%PDF-") if extension == ".pdf" else signature.startswith(b"PK\x03\x04")
    if not valid:
        path.unlink(missing_ok=True)
        raise GatewayError(400, "unsupported_file_type", "File content does not match its extension.", "file")