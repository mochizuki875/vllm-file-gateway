from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import BigInteger, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from gateway.config import Settings


class Base(DeclarativeBase):
    pass


class StoredFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64), default="user_data")
    byte_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_path: Mapped[str] = mapped_column(String(1024))
    manifest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def to_openai(self) -> dict[str, object]:
        status = "processed" if self.status == "processed" else "error" if self.status == "failed" else "uploaded"
        return {
            "id": self.id,
            "object": "file",
            "bytes": self.byte_size,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "filename": self.filename,
            "purpose": self.purpose,
            "status": status,
        }


class Database:
    def __init__(self, settings: Settings) -> None:
        settings.gateway_data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def add(self, record: StoredFile) -> None:
        with self.sessions.begin() as session:
            session.add(record)

    def get(self, file_id: str, tenant_id: str) -> StoredFile | None:
        now = int(time.time())
        with self.sessions() as session:
            return session.scalar(
                select(StoredFile).where(
                    StoredFile.id == file_id,
                    StoredFile.tenant_id == tenant_id,
                    StoredFile.deleted_at.is_(None),
                    StoredFile.expires_at > now,
                )
            )

    def list(self, tenant_id: str, limit: int, order: str, after: str | None) -> list[StoredFile]:
        now = int(time.time())
        ordering = StoredFile.created_at.asc() if order == "asc" else StoredFile.created_at.desc()
        with self.sessions() as session:
            statement = select(StoredFile).where(
                StoredFile.tenant_id == tenant_id,
                StoredFile.deleted_at.is_(None),
                StoredFile.expires_at > now,
            )
            if after:
                cursor = session.get(StoredFile, after)
                if cursor and cursor.tenant_id == tenant_id:
                    comparison = StoredFile.created_at > cursor.created_at if order == "asc" else StoredFile.created_at < cursor.created_at
                    statement = statement.where(comparison)
            return list(session.scalars(statement.order_by(ordering).limit(limit)))

    def update_status(
        self,
        file_id: str,
        status: str,
        manifest_path: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            record = session.get(StoredFile, file_id)
            if record:
                record.status = status
                record.manifest_path = manifest_path
                record.error_message = error_message

    def mark_deleted(self, file_id: str, tenant_id: str) -> StoredFile | None:
        with self.sessions.begin() as session:
            record = session.get(StoredFile, file_id)
            if not record or record.tenant_id != tenant_id or record.deleted_at is not None:
                return None
            record.deleted_at = int(time.time())
            record.status = "deleted"
            return record

    def expired(self) -> list[StoredFile]:
        now = int(time.time())
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(StoredFile).where(
                        StoredFile.deleted_at.is_(None),
                        StoredFile.expires_at <= now,
                    )
                )
            )

    def pending(self) -> list[str]:
        now = int(time.time())
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(StoredFile.id).where(
                        StoredFile.status.in_(("uploaded", "processing")),
                        StoredFile.deleted_at.is_(None),
                        StoredFile.expires_at > now,
                    )
                )
            )

    def source(self, record: StoredFile, data_dir: Path) -> Path:
        return data_dir / record.source_path
