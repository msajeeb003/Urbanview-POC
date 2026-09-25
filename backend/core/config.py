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
# staff tokens on a reachable server (staging / prod): long random strings only
MIN_TOKEN_LENGTH = 24


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

    # background jobs (jobs/): lifecycle, retries with exponential backoff, LLM cost tracking
    celery_task_always_eager: bool = False  # run tasks inline in the caller (tests only)
    job_max_attempts: int = Field(default=3, ge=1)
    job_retry_base_seconds: int = Field(default=30, ge=1)
    job_retry_max_seconds: int = Field(default=900, ge=1)
    llm_price_eur_per_mtok_input: float = Field(default=3.0, ge=0)
    llm_price_eur_per_mtok_output: float = Field(default=15.0, ge=0)
    llm_price_table: str | None = None  # per-model override: 'model=in:out,...' (EUR / MTok)

    # S3-compatible private object storage
    s3_endpoint_url: str | None = None
    # the address browsers reach the bucket at (signed links); unset = S3_ENDPOINT_URL. On one
    # server the API talks to http://minio:9000 while visitors fetch https://files.<domain>.
    s3_public_endpoint_url: str | None = None
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
    smtp_use_ssl: bool = False  # implicit TLS (port 465) instead of STARTTLS
    smtp_timeout_seconds: int = Field(default=15, ge=1, le=120)

    # transactional e-mail (core.mail, jobs.tasks.email) and the magic-link login
    mail_app_name: str = "UrbanView"  # the brand name in subjects and bodies
    mail_reply_to: str | None = None  # None = ORDER_SUPPORT_EMAIL
    # addresses or @domains that may be mailed; enforced in staging (empty = nothing goes out)
    mail_allowlist: Annotated[list[str], NoDecode] = []
    admin_base_url: str = "http://localhost:3001"  # magic links point here
    magic_link_expires_seconds: int = Field(default=900, ge=60, le=3600)
    staff_session_days: int = Field(default=30, ge=1, le=365)

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
    # display-shaped panels (GET /v1/parcels/{id}/panel, /v1/zones/{id}/panel): Redis entries per
    # entity per data state; keys change on every publish or admin change, 0 disables the cache
    panel_cache_ttl_seconds: int = Field(default=3600, ge=0, le=7 * 86400)

    # location resolution (api.services.resolver)
    location_resolver: Literal["postgis", "nodata"] = "postgis"
    # A planned urban parcel counts as "corresponding" to a cadastral parcel when their overlap is
    # at least this many m² AND this share of the cadastral area (filters digitising slivers).
    locate_min_overlap_m2: float = Field(default=1.0, ge=0)
    locate_min_overlap_fraction: float = Field(default=0.02, ge=0, le=1)

    # geocoding proxy (GET /v1/geocode, core.geocode)
    geocoder_provider: Literal["photon", "nominatim"] = "photon"
    geocoder_base_url: str | None = None  # None = the provider's public instance
    geocoder_contact: str | None = None  # email or URL identifying the operator (OSM policies)
    geocoder_user_agent: str | None = None  # None = "<app_name>/<app_version> (+<contact>)"
    geocoder_language: str | None = None  # None = the municipality locale
    geocoder_timeout_ms: int = Field(default=1500, ge=100)
    geocoder_max_results: int = Field(default=8, ge=1, le=20)
    geocoder_min_query_length: int = Field(default=2, ge=1)
    geocoder_cache_ttl_seconds: int = Field(default=600, ge=0)  # 0 disables the cache
    geocoder_min_interval_ms: int | None = Field(default=None, ge=0)  # None = provider policy
    geocoder_throttle_wait_ms: int = Field(default=400, ge=0)
    geocoder_failure_backoff_seconds: int = Field(default=5, ge=0)

    # source viewer (GET /v1/source, api.services.source): lifetime of the signed URLs
    source_url_expires_seconds: int = Field(default=900, ge=60, le=86400)

    # staff routes (/v1/admin/*): "token:role[:subject],..." with roles admin | reviewer | expert;
    # empty = every role-gated route answers 401 (core.auth)
    admin_api_tokens: SecretStr | None = None
    admin_upload_max_mb: int = Field(default=100, ge=1, le=2048)  # POST /v1/admin/files

    # publish pipeline (jobs.publish_pipeline) and the tiles pointer (GET /v1/tiles/current)
    tippecanoe_bin: str = "tippecanoe"
    tile_join_bin: str = "tile-join"
    tiles_min_zoom: int = Field(default=8, ge=0, le=22)
    tiles_max_zoom: int = Field(default=16, ge=0, le=22)
    tiles_url_expires_seconds: int = Field(default=3600, ge=60, le=86400)
    publish_keep_versions: int = Field(default=3, ge=2, le=50)  # archives kept for rollback
    publish_tmp_dir: str | None = None  # scratch dir for GeoJSON + tiles; None = system temp

    # expert-analysis orders (POST /v1/orders): guest checkout by bank transfer
    order_price_tiers: str = "500:100,inf:200"  # "<max m²>:<EUR>,…,inf:<EUR>" (BRD band 50–200)
    order_turnaround_business_days: int = Field(default=5, ge=1, le=60)
    order_max_per_email_per_day: int = Field(default=5, ge=1, le=100)
    order_report_link_expires_seconds: int = Field(default=7 * 24 * 3600, ge=300, le=30 * 86400)
    order_bank_beneficiary: str = "UrbanView d.o.o. (placeholder)"
    order_bank_iban: str = "ME00 0000 0000 0000 0000 00 (placeholder)"
    order_bank_name: str | None = None
    order_bank_swift: str | None = None
    order_support_email: str = "support@urbanview.io"
    order_public_base_url: str = "http://localhost:3000"  # links in e-mails point here

    @field_validator(
        "geocoder_base_url",
        "geocoder_contact",
        "geocoder_user_agent",
        "geocoder_language",
        "geocoder_min_interval_ms",
        "publish_tmp_dir",
        "mail_reply_to",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """``GEOCODER_X=`` in an env file means "use the default", not an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("order_price_tiers")
    @classmethod
    def _tiers_well_formed(cls, value: str) -> str:
        from core.pricing import parse_price_tiers

        parse_price_tiers(value)
        return value

    @field_validator("admin_api_tokens")
    @classmethod
    def _tokens_well_formed(cls, value: SecretStr | None) -> SecretStr | None:
        """A malformed ADMIN_API_TOKENS fails at startup, not on the first admin request."""
        if value is not None:
            from core.auth import parse_api_tokens

            parse_api_tokens(value.get_secret_value())
        return value

    @field_validator("cors_origins", "rate_limit_exempt_paths", "mail_allowlist", mode="before")
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
        if self.admin_api_tokens is not None:
            from core.auth import parse_api_tokens

            tokens = parse_api_tokens(self.admin_api_tokens.get_secret_value())
            if any(len(token) < MIN_TOKEN_LENGTH for token in tokens):
                problems.append(
                    f"ADMIN_API_TOKENS tokens must be at least {MIN_TOKEN_LENGTH} characters"
                )
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
