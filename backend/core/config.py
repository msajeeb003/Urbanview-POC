"""Application settings.

Everything comes from the environment (plus a local ``.env`` in dev). No secrets live in code:
the defaults below are dev conveniences and are rejected outside ``APP_ENV=dev``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_DATABASE_URL = "postgresql+asyncpg://urbanview:urbanview@localhost:5432/urbanview"


class AppEnv(StrEnum):
    dev = "dev"
    staging = "staging"
    prod = "prod"


def _parse_list(value: Any) -> Any:
    """Accept a JSON array or a comma-separated string for list-valued settings."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # application
    app_env: AppEnv = AppEnv.dev
    app_name: str = "urbanview-api"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    municipality_id: str = "podgorica"

    # database (PostgreSQL + PostGIS, asyncpg)
    database_url: str = DEV_DATABASE_URL
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_echo: bool = False

    # redis / celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # S3-compatible private object storage
    s3_endpoint_url: str | None = None
    s3_bucket: str = "urbanview-dev"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    s3_use_path_style: bool = True

    # smtp
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    smtp_from: str = "UrbanView <no-reply@urbanview.io>"

    # per-IP rate limiting (fixed window)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_exempt_paths: Annotated[list[str], NoDecode] = ["/health", "/health/ready"]
    rate_limit_redis_timeout_ms: int = Field(default=250, ge=1)
    rate_limit_fail_open_seconds: int = Field(default=5, ge=0)
    trust_proxy_headers: bool = False

    # http
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    slow_request_ms: int = Field(default=2000, ge=1)

    # location resolution (api.services.resolver)
    location_resolver: Literal["postgis", "nodata"] = "postgis"
    # A planned urban parcel counts as "corresponding" to a cadastral parcel when their overlap is
    # at least this many m² AND this share of the cadastral area (filters digitising slivers).
    locate_min_overlap_m2: float = Field(default=1.0, ge=0)
    locate_min_overlap_fraction: float = Field(default=0.02, ge=0, le=1)

    @field_validator("cors_origins", "rate_limit_exempt_paths", mode="before")
    @classmethod
    def _lists_from_env(cls, value: Any) -> Any:
        return _parse_list(value)

    @model_validator(mode="after")
    def _guard_non_dev(self) -> Settings:
        """Refuse to start staging/prod on dev defaults or without storage credentials."""
        if self.app_env is AppEnv.dev:
            return self
        problems: list[str] = []
        if self.database_url == DEV_DATABASE_URL:
            problems.append("DATABASE_URL must be set explicitly (dev default not allowed)")
        if self.s3_access_key is None or self.s3_secret_key is None:
            problems.append("S3_ACCESS_KEY and S3_SECRET_KEY are required")
        if problems:
            raise ValueError(
                f"invalid settings for APP_ENV={self.app_env.value}: " + "; ".join(problems)
            )
        return self

    @property
    def is_dev(self) -> bool:
        return self.app_env is AppEnv.dev

    @property
    def is_prod(self) -> bool:
        return self.app_env is AppEnv.prod


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
