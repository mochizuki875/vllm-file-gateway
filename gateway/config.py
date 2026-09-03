from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vllm_model: str = Field(min_length=1)
    vllm_base_url: AnyHttpUrl
    vllm_api_key: str = Field(min_length=1)
    gateway_api_key: str = Field(min_length=1)
    gateway_data_dir: Path = Path("gateway-data")
    file_ttl_seconds: int = 21_600
    max_file_bytes: int = 50 * 1024 * 1024
    max_document_images: int = Field(default=8, ge=1)
    request_timeout_seconds: float = 300.0

    @field_validator("vllm_base_url")
    @classmethod
    def require_v1_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.path.rstrip("/") != "/v1":
            raise ValueError("VLLM_BASE_URL must end with /v1")
        return value

    @field_validator("file_ttl_seconds")
    @classmethod
    def require_six_hour_ttl(cls, value: int) -> int:
        if value != 21_600:
            raise ValueError("FILE_TTL_SECONDS must be 21600")
        return value

    @property
    def database_url(self) -> str:
        database_path = (self.gateway_data_dir / "gateway.db").resolve()
        return f"sqlite:///{database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
