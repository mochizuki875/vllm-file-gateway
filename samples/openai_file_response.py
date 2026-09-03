from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from openai import APIConnectionError, OpenAI


DEFAULT_BASE_URL = "http://localhost:8080/v1"
DEFAULT_MODEL = "gemma-4-26B-A4B"
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_PROCESSING_TIMEOUT = 300.0


def wait_until_processed(
    client: OpenAI,
    file_id: str,
    timeout: float,
    poll_interval: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remote_file = client.files.retrieve(file_id)
        status = remote_file.status
        if status == "processed":
            return
        if status == "error":
            raise RuntimeError(f"File processing failed: {file_id}")
        time.sleep(poll_interval)
    raise TimeoutError(f"File processing did not finish within {timeout:g} seconds: {file_id}")


def run(file_path: Path, prompt: str, timeout: float) -> None:
    base_url = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    client = OpenAI(
        base_url=base_url,
        api_key=os.getenv("OPENAI_API_KEY", "dummy"),
    )
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    uploaded_file = None

    try:
        with file_path.open("rb") as source:
            uploaded_file = client.files.create(file=source, purpose="user_data")
        print(f"uploaded: {uploaded_file.id} (status={uploaded_file.status})")

        wait_until_processed(
            client,
            uploaded_file.id,
            timeout=timeout,
            poll_interval=DEFAULT_POLL_INTERVAL,
        )
        print(f"processed: {uploaded_file.id}")

        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded_file.id},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
        )
        print("response:")
        print(response.output_text)
    except APIConnectionError as error:
        raise RuntimeError(
            f"Gatewayに接続できません: {base_url}\n"
            "別のターミナルで次を実行してください:\n"
            "  ./.venv/bin/uvicorn gateway.app:app --host 0.0.0.0 --port 8080"
        ) from error
    finally:
        if uploaded_file is not None:
            deleted = client.files.delete(uploaded_file.id)
            print(f"deleted: {deleted.id} (deleted={deleted.deleted})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a document, call the Responses API, and delete the file.",
    )
    parser.add_argument("file", type=Path, help="PDF, PPTX, DOCX, or XLSX file")
    parser.add_argument(
        "--prompt",
        default="この文書の内容を要約してください。",
        help="Prompt sent with the uploaded document",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PROCESSING_TIMEOUT,
        help="Maximum seconds to wait for document conversion",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.file.is_file():
        raise SystemExit(f"File not found: {args.file}")
    try:
        run(args.file, args.prompt, args.timeout)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()