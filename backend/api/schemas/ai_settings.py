"""Schemas of the admin console's AI extraction page (``/v1/admin/ai``, role ``admin``).

The Anthropic API key is write-only: ``AiKeyIn`` carries it in, nothing carries it out (only the
last four characters of the active, the server's and the saved key)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from api.schemas.admin import JobOut, JobStatus

AiCheckStatus = Literal[
    "ok",
    "no_key",
    "invalid_key",
    "permission_denied",
    "no_credit",
    "model_unavailable",
    "rate_limited",
    "overloaded",
    "network_error",
    "error",
]
KeySource = Literal["server_env", "console", "none"]
ChecklistKey = Literal["api_key", "key_verified", "worker", "reviewer_account", "smtp"]
WorkerStateName = Literal["ready", "no_worker", "unreachable", "eager"]
UsageType = Literal["extract_document", "import_market_data", "ai_check", "total"]

ANTHROPIC_CONSOLE_KEYS_URL = "https://console.anthropic.com/settings/keys"
ANTHROPIC_CONSOLE_BILLING_URL = "https://console.anthropic.com/settings/billing"


class AiKeyIn(BaseModel):
    api_key: SecretStr = Field(
        description="Write-only: an Anthropic API key (sk-ant-…); never returned"
    )


class AiKeyStateOut(BaseModel):
    """Which key the API sees (the worker resolves the same way): never the value."""

    configured: bool = Field(description="A usable key from the API's view")
    source: KeySource = Field(description="Where the active key comes from")
    last4: str | None = Field(default=None, description="The active key's last 4 characters")
    server_env_key: bool = Field(description="ANTHROPIC_API_KEY is set in the API process")
    server_env_last4: str | None = None
    console_key_stored: bool = Field(description="A key is saved in this console")
    console_key_last4: str | None = None
    console_key_active: bool = Field(
        description="The saved key is in use: stored, readable and no server environment key"
    )
    console_key_readable: bool | None = Field(
        default=None,
        description="The saved key decrypts with this server's SECRETS_ENCRYPTION_KEY "
        "(null: nothing saved or no encryption key)",
    )
    console_note_en: str | None = Field(
        default=None, description="Why the saved key is inactive or unusable"
    )
    set_by: str | None = Field(default=None, description="Who saved the console key")
    set_at: datetime | None = Field(default=None, description="When the console key was saved")


class AiModelSettingsOut(BaseModel):
    """The model settings of the server (read-only: deploy/.env)."""

    model: str = Field(description="EXTRACTION_MODEL")
    effort: str | None = Field(default=None, description="EXTRACTION_EFFORT (null: default)")
    adaptive_thinking: bool
    max_tokens: int
    refusal_fallback: bool
    timeout_seconds: int
    market_model: str = Field(description="MARKET_MODEL, else EXTRACTION_MODEL")
    market_effort: str | None = None
    base_url_host: str = Field(description="The host of ANTHROPIC_BASE_URL")
    base_url_is_default: bool = Field(description="The host is api.anthropic.com")
    price_input_eur_per_mtok: float = Field(
        description="LLM_PRICE_TABLE's entry for the model, else LLM_PRICE_EUR_PER_MTOK_INPUT"
    )
    price_output_eur_per_mtok: float


class AiCheckOut(BaseModel):
    """The latest connection test (an ``ai_check`` job) and what it found."""

    job: JobOut
    status: AiCheckStatus | None = Field(
        default=None,
        description="Null while queued / running / retrying; error for a failed job",
    )
    detail_en: str | None = None
    model_requested: str | None = None
    model_answered: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_eur: float | None = None
    key_source: KeySource | None = Field(default=None, description="The key the test used")
    key_last4: str | None = None
    checked_at: datetime | None = None
    matches_current_key: bool = Field(
        description="The test used the key the API sees now (source, last 4, when saved)"
    )


class AiWorkerOut(BaseModel):
    state: WorkerStateName
    workers: int = Field(description="Workers consuming the extraction queue")
    detail_en: str | None = None
    checked_at: datetime


class AiUsageRowOut(BaseModel):
    type: UsageType
    jobs: int
    succeeded: int
    failed: int
    tokens_in: int
    tokens_out: int
    estimated_cost_eur: float
    last_finished_at: datetime | None = None


class AiLastRunOut(BaseModel):
    job_id: int
    status: JobStatus
    document_id: int | None = None
    requested_at: datetime
    finished_at: datetime | None = None


class AiUsageOut(BaseModel):
    rows: list[AiUsageRowOut] = Field(
        description="extract_document, import_market_data, ai_check (zeros when absent)"
    )
    total: AiUsageRowOut
    since: datetime | None = Field(default=None, description="The first AI job's request time")
    last_extraction: AiLastRunOut | None = None
    currency: Literal["EUR"] = "EUR"
    note_en: str


class AiChecklistItemOut(BaseModel):
    key: ChecklistKey
    ok: bool | None = Field(description="Null: could not be determined, or in progress")
    required: bool
    label_en: str
    detail_en: str


class AiLinksOut(BaseModel):
    api_keys_url: str = ANTHROPIC_CONSOLE_KEYS_URL
    billing_url: str = ANTHROPIC_CONSOLE_BILLING_URL


class AiStatusOut(BaseModel):
    key: AiKeyStateOut
    encryption_ready: bool = Field(description="SECRETS_ENCRYPTION_KEY is set on the server")
    encryption_note_en: str | None = None
    model: AiModelSettingsOut
    check: AiCheckOut | None = Field(default=None, description="The latest connection test")
    worker: AiWorkerOut
    usage: AiUsageOut
    checklist: list[AiChecklistItemOut] = Field(
        description="api_key, key_verified, worker, reviewer_account, smtp (in that order)"
    )
    ready: bool = Field(description="Every required checklist item is ok")
    links: AiLinksOut = Field(default_factory=AiLinksOut)
    generated_at: datetime
