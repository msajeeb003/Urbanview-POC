"""Schemas for the staff pipeline API (``/v1/admin/files``, ``/documents``, ``/jobs``)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.extraction.manifest import PreprocessSummary

DocumentStatus = Literal["adopted", "in_progress", "superseded"]
JobKind = Literal["extract", "geo", "publish", "email"]
JobType = Literal[
    "extract_document",
    "preprocess_file",
    "process_geometry",
    "publish_approved",
    "send_email",
    "import_market_data",
]
JobStatus = Literal["queued", "running", "retrying", "succeeded", "failed", "cancelled"]
TARGET_PATTERN = r"^(document|file|publish_run|email):[0-9]+$"


class FileKind(StrEnum):
    planning_document = "planning_document"
    gis = "gis"
    cadastral_extract = "cadastral_extract"
    expert_report = "expert_report"  # written by the order flow, never uploaded here
    market_data = "market_data"  # statistics tables, the client's range sheets (core.market)


class JobCostOut(BaseModel):
    """What the job cost; fields stay ``null`` until it has run (and succeeded, for LLM cost)."""

    wall_time_ms: int | None = Field(default=None, description="Wall time of the last attempt")
    llm_model: str | None = None
    llm_tokens_in: int | None = None
    llm_tokens_out: int | None = None
    estimated_cost_eur: float | None = Field(
        default=None, description="From the configured LLM prices (LLM_PRICE_*)"
    )


class PageFailure(BaseModel):
    page: int
    chunk: str = Field(description="The manifest's chunk id")
    task: str = Field(description="The extraction task that failed on it")
    error: str


class ExtractionRunOut(BaseModel):
    """One extraction run of a document version's file (``extraction_runs``)."""

    id: int
    status: Literal["queued", "extracting", "ready_for_review", "failed"]
    document_id: int
    file_id: int | None = None
    file_sha256: str
    model: str = Field(description="The configured model (part of the idempotency key)")
    model_version: str | None = Field(default=None, description="The model id the API reported")
    prompt_version: str
    schema_version: str
    pages_total: int | None = None
    pages_processed: int = Field(default=0, description="Pages read by a successful chunk")
    pages_skipped: list[int] = Field(
        default_factory=list, description="Scanned pages nobody could read (no OCR)"
    )
    pages_failed: list[PageFailure] = Field(
        default_factory=list, description="Pages of chunks that failed after their retry"
    )
    pages_failed_count: int = Field(default=0, description="Distinct failed pages")
    chunks_total: int = 0
    chunks_done: int = 0
    chunks_failed: int = 0
    items_written: int = 0
    items_low_confidence: int = 0
    items_unmatched: int = Field(
        default=0, description="Items whose parcel / block matches no geometry (kept as printed)"
    )
    items_superseded: int = Field(default=0, description="Earlier items this run superseded")
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_eur: float | None = None
    error: str | None = None
    superseded_by_run_id: int | None = None
    job_id: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _count_failed_pages(self) -> ExtractionRunOut:
        self.pages_failed_count = len({f.page for f in self.pages_failed})
        return self


class JobOut(BaseModel):
    id: int
    kind: JobKind
    type: JobType
    queue: str
    status: JobStatus
    document_id: int | None = None
    file_id: int | None = None
    target_type: str | None = Field(
        default=None, description="document | file | publish_run | email"
    )
    target_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = Field(
        default=None, description="Idempotency key: one active job per key"
    )
    attempts: int = Field(default=0, description="Attempts of the current run")
    max_attempts: int = 3
    manual_retries: int = Field(default=0, description="POST /retry count")
    next_retry_at: datetime | None = None
    celery_task_id: str | None = None
    requested_by: str
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    cost: JobCostOut = Field(default_factory=JobCostOut)
    progress: dict[str, Any] | None = Field(
        default=None, description="Per-step progress written by the running task"
    )
    extraction_run: ExtractionRunOut | None = Field(
        default=None, description="extract_document jobs: the run's summary as it stands"
    )
    status_url: str = Field(description="GET here for the current status")


class JobList(BaseModel):
    items: list[JobOut]
    total: int = Field(description="Matching jobs before paging")
    limit: int
    offset: int


class JobCostRow(BaseModel):
    target_type: str | None
    target_id: int | None
    jobs: int
    succeeded: int
    failed: int
    llm_tokens_in: int
    llm_tokens_out: int
    estimated_cost_eur: float
    wall_time_ms: int
    last_finished_at: datetime | None = None


class JobCostSummary(BaseModel):
    rows: list[JobCostRow] = Field(description="One row per target, most expensive first")
    total_jobs: int
    total_llm_tokens_in: int
    total_llm_tokens_out: int
    total_estimated_cost_eur: float
    total_wall_time_ms: int


class StoredFileOut(BaseModel):
    id: int
    kind: FileKind
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None = Field(description="PDFs only")
    uploaded_by: str
    uploaded_at: datetime
    document_ids: list[int] = Field(default_factory=list, description="Documents registered on it")
    jobs: list[JobOut] = Field(
        default_factory=list, description="Recent geometry and pre-processing jobs, newest first"
    )
    preprocessing: PreprocessSummary | None = Field(
        default=None,
        description="PDF pre-processing: pages, vector / scanned pages, tables, chunks, sections",
    )
    extraction: ExtractionRunOut | None = Field(
        default=None,
        description=(
            "The latest extraction run over the file: queued -> extracting -> ready_for_review "
            "| failed, with the pages that failed"
        ),
    )


class UploadResult(BaseModel):
    file: StoredFileOut
    created: bool = Field(description="false when the checksum was already known (no new object)")


class FileList(BaseModel):
    items: list[StoredFileOut]
    limit: int
    offset: int


class FileSummary(BaseModel):
    id: int
    kind: FileKind
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None = None
    uploaded_by: str
    uploaded_at: datetime


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int = Field(gt=0, description="A stored file of kind planning_document")
    name: str = Field(min_length=1, max_length=300)
    type: str = Field(
        min_length=1,
        max_length=20,
        description="A key of the municipality's document_types (DUP / PUP / PGR)",
    )
    status: DocumentStatus
    source: str | None = Field(default=None, max_length=200, description="e.g. eRegistri")
    source_url: str | None = Field(default=None, max_length=1000)
    zone_id: int | None = Field(default=None, gt=0)
    licence_note: str | None = Field(default=None, max_length=2000)
    adopted_on: date | None = Field(
        default=None, description="Adoption date (official gazette), not in the future"
    )
    amends_document_id: int | None = Field(default=None, gt=0)
    replaces_document_id: int | None = Field(
        default=None,
        gt=0,
        description="Register a new version of this document (it must be the current version)",
    )

    @field_validator("adopted_on")
    @classmethod
    def _adopted_in_the_past(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("adopted_on cannot be in the future")
        return value


class VersionRef(BaseModel):
    id: int
    version: int
    status: DocumentStatus
    is_current_version: bool
    registered_at: datetime | None = None


class ReviewSummary(BaseModel):
    """Review state of the document's staged extraction items (``/v1/admin/review``)."""

    pending: int
    approved: int
    amended: int
    rejected: int
    total: int
    can_publish: bool = Field(description="No pending items and at least one approved / amended")
    publish_blockers: list[str] = Field(default_factory=list)


class DocumentOut(BaseModel):
    id: int
    lineage_id: int = Field(description="Id of the first version; all versions share it")
    version: int
    is_current_version: bool
    name: str
    type: str
    status: DocumentStatus
    source: str | None = None
    source_url: str | None = None
    zone_id: int | None = None
    zone_name: str | None = None
    amends_document_id: int | None = None
    licence_note: str | None = None
    adopted_on: date | None = None
    file: FileSummary | None = None
    page_count: int | None = None
    has_coverage: bool = Field(description="A coverage geometry exists (geometry job ran / seeded)")
    coverage_live: bool = Field(description="Takes part in location resolution")
    coverage_live_changed_at: datetime | None = None
    coverage_live_changed_by: str | None = None
    registered_by: str | None = None
    registered_at: datetime | None = None
    created_at: datetime
    versions: list[VersionRef] = Field(default_factory=list)
    jobs: list[JobOut] = Field(default_factory=list, description="Recent extraction jobs")
    review: ReviewSummary | None = None
    preprocessing: PreprocessSummary | None = Field(
        default=None,
        description=(
            "PDF pre-processing of the document's file: scanned pages (listed; they need manual "
            "handling), vector pages, tables, chunks, planning sections"
        ),
    )
    extraction: ExtractionRunOut | None = Field(
        default=None,
        description=(
            "The latest extraction run of this version: queued -> extracting -> "
            "ready_for_review | failed, pages failed / skipped, items written"
        ),
    )


class DocumentList(BaseModel):
    items: list[DocumentOut]
    limit: int
    offset: int


class CoverageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live: bool
