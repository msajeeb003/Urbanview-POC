"""Schemas for the staff pipeline API (``/v1/admin/files``, ``/documents``, ``/jobs``)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.extraction.manifest import PreprocessSummary

DocumentStatus = Literal["adopted", "in_progress", "superseded"]
SHORT_CODE_PATTERN = r"^[\w][\w .\-/]*$"


def _trimmed(value: object) -> object:
    """Surrounding spaces never count (a short code is typed by hand)."""
    return value.strip() if isinstance(value, str) else value


FileRole = Literal["text", "drawing", "both"]
# Where a document stands in the pipeline (DocumentOut.state), first match wins: no file
# attached; an extraction queued / running; items waiting for review; a file's latest extraction
# failed; everything accepted is published; everything is decided; a finished run with nothing
# to review yet; files never extracted.
DocumentState = Literal[
    "no_files",
    "processing",
    "ready_for_review",
    "failed",
    "published",
    "reviewed",
    "not_extracted",
]
# The per-file job filter of the documents list: the latest extraction / geometry job of a file.
JobStateFilter = Literal["queued", "running", "succeeded", "failed"]
# One file's extraction, from its latest run and that run's job (the job decides while it exists).
ExtractionState = Literal["none", "queued", "extracting", "retrying", "ready_for_review", "failed"]
JobKind = Literal["extract", "geo", "publish", "email"]
JobType = Literal[
    "extract_document",
    "process_geometry",
    "publish_approved",
    "send_email",
    "import_market_data",
    "refresh_heatmaps",
    "import_zones",
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
        default=None, description="document | file | publish_run | email | market_import"
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


class DocumentFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int = Field(gt=0, description="A stored planning_document PDF (or a gis file)")
    role: FileRole = Field(
        default="text",
        description="text = read by the extraction job, drawing = by the geometry job, both",
    )


class DocumentFilesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[DocumentFileIn] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _distinct(self) -> DocumentFilesIn:
        ids = [f.file_id for f in self.files]
        if len(ids) != len(set(ids)):
            raise ValueError("a file is listed more than once")
        return self


class DocumentFileRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: FileRole


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int | None = Field(
        default=None,
        gt=0,
        description="Single-file form (kept for older clients): the same as files=[{file_id}]",
    )
    files: list[DocumentFileIn] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "The version's files in display order; more can be attached later "
            "(POST /v1/admin/documents/{id}/files). The first text / both PDF is the primary file"
        ),
    )
    name: str = Field(min_length=1, max_length=300)
    short_code: str | None = Field(
        default=None,
        max_length=40,
        pattern=SHORT_CODE_PATTERN,
        description=(
            "Short reference staff use for the plan (the pilot's short_code), e.g. DUP-NG12; a new "
            "version keeps the previous one unless it is given"
        ),
    )
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

    _short_code = field_validator("short_code", mode="before")(_trimmed)

    @field_validator("adopted_on")
    @classmethod
    def _adopted_in_the_past(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("adopted_on cannot be in the future")
        return value

    @model_validator(mode="after")
    def _one_file_list(self) -> DocumentIn:
        """``file_id`` joins ``files`` (first, as a text file) unless it is listed there."""
        if self.file_id is not None and all(f.file_id != self.file_id for f in self.files):
            self.files = [DocumentFileIn(file_id=self.file_id, role="text"), *self.files]
        ids = [f.file_id for f in self.files]
        if len(ids) != len(set(ids)):
            raise ValueError("a file is listed more than once")
        return self


class DocumentPatchIn(BaseModel):
    """What ``PATCH /v1/admin/documents/{id}`` may change on the current version; a field left
    out stays as it is, ``null`` clears the optional ones. A status change takes effect in
    location resolution and the panels at once (they read adopted documents only) and in the map
    tiles at the next publish."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=300)
    short_code: str | None = Field(default=None, max_length=40, pattern=SHORT_CODE_PATTERN)
    status: DocumentStatus | None = None
    zone_id: int | None = Field(default=None, gt=0)
    source: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=1000)
    adopted_on: date | None = None
    licence_note: str | None = Field(default=None, max_length=2000)

    _short_code = field_validator("short_code", mode="before")(_trimmed)

    @field_validator("adopted_on")
    @classmethod
    def _adopted_in_the_past(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("adopted_on cannot be in the future")
        return value

    @model_validator(mode="after")
    def _something(self) -> DocumentPatchIn:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        for key in ("name", "status"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be cleared")
        return self


class MunicipalityRef(BaseModel):
    id: str
    name: str


class ZoneImportIn(BaseModel):
    """A zone GeoPackage from QGIS, uploaded first (``POST /v1/admin/files``, kind ``gis``)."""

    model_config = ConfigDict(extra="forbid")

    file_id: int = Field(gt=0, description="The stored GeoPackage (zones layer + documents table)")
    dry_run: bool = Field(default=False, description="Validate only: nothing staged")


class VersionRef(BaseModel):
    id: int
    version: int
    status: DocumentStatus
    is_current_version: bool
    name: str | None = None
    registered_by: str | None = None
    registered_at: datetime | None = None
    file_count: int = 0


class ReviewSummary(BaseModel):
    """Review state of the document's staged extraction items (``/v1/admin/review``)."""

    pending: int
    approved: int
    amended: int
    rejected: int
    total: int
    can_publish: bool = Field(description="No pending items and at least one approved / amended")
    publish_blockers: list[str] = Field(default_factory=list)


class ItemCounts(BaseModel):
    """Review items read from one file of the document (not superseded)."""

    pending: int = 0
    approved: int = 0
    amended: int = 0
    rejected: int = 0
    published: int = 0
    total: int = 0


class DocumentFileOut(BaseModel):
    """One file of a document version with where it stands: its latest extraction run (and that
    run's job), its latest geometry job, the review items read from it and whether it may be
    removed (not once any of its items is approved, amended or published)."""

    file_id: int
    role: FileRole
    position: int
    is_primary: bool = Field(description="The version's primary file (planning_documents.file_id)")
    kind: FileKind
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None = None
    scanned_pages: list[int] | None = Field(
        default=None, description="From the PDF pre-processing; null until it has run"
    )
    redraw_pages: list[int] | None = Field(
        default=None,
        description=(
            "Pages to redraw in QGIS: scanned sheets by the week-1 assessment's rule (class C, "
            "core.gis.sheets); [] = none, null = the pages have not been read yet"
        ),
    )
    uploaded_at: datetime
    added_by: str | None = None
    added_at: datetime
    preprocessing: PreprocessSummary | None = None
    extraction_state: ExtractionState = Field(
        default="none",
        description="none | queued | extracting | retrying | ready_for_review | failed",
    )
    extraction_error: str | None = Field(default=None, description="Why it failed")
    extraction: ExtractionRunOut | None = Field(
        default=None, description="The latest extraction run of this file for this version"
    )
    extraction_job: JobOut | None = Field(default=None, description="That run's job")
    geometry_job: JobOut | None = Field(
        default=None, description="The latest process_geometry job of the file"
    )
    items: ItemCounts = Field(default_factory=ItemCounts)
    can_remove: bool
    remove_blocker: str | None = Field(
        default=None, description="items_accepted | values_published | extraction_active"
    )


class GeoreferenceSheet(BaseModel):
    sheet: str
    page: int | None = None
    points: int = Field(description="Enabled control points on the sheet")
    rmse_m: float | None = Field(default=None, description="Null when the sheet has no point")


class GeoreferenceOut(BaseModel):
    """The document version's latest georeferencing run (``core.gis.georef``): the transform's
    residual report per sheet, the snapping to the cadastral base and the validation. Rows are
    written by the georeferencing CLI; the publish job serves a staged run's geometry."""

    dataset_version: str
    status: Literal["staged", "invalid", "published", "superseded"]
    source: Literal["extraction", "manual_redraw", "gis_file"]
    crs: str = Field(
        description="The plan's projected CRS the control points are in (a GIS file: its own)"
    )
    method: Literal["helmert", "affine", "native"] = Field(
        description="native: a GIS file in its own CRS, reprojected without a fit"
    )
    rmse_m: float | None = Field(default=None, description="Null for a native GIS file")
    max_rmse_m: float | None = Field(default=None, description="The document's threshold")
    max_residual_m: float | None = None
    points_used: int
    sheets: list[GeoreferenceSheet] = Field(default_factory=list)
    snap_tolerance_m: float | None = None
    snapped_vertices: int | None = None
    snapped_ratio: float | None = Field(
        default=None, description="Share of planned parcel / block vertices moved onto the cadastre"
    )
    near_misses: int | None = Field(
        default=None, description="Vertices near a cadastral vertex but beyond the tolerance"
    )
    cadastral_overlap_share: float | None = Field(
        default=None, description="Share of the planned parcel area lying on cadastral parcels"
    )
    systematic_offset_m: float | None = Field(
        default=None,
        description=(
            "Mean distance, in one direction, from planned vertices to the cadastral vertices "
            "they follow (within three tolerances): the overlay check, near 0 when aligned"
        ),
    )
    errors: list[str] = Field(default_factory=list, description="Validation error codes")
    warnings: list[str] = Field(default_factory=list, description="Validation warning codes")
    created_at: datetime
    published_at: datetime | None = None


class DocumentOut(BaseModel):
    id: int
    lineage_id: int = Field(description="Id of the first version; all versions share it")
    version: int
    is_current_version: bool
    municipality_id: str = Field(description="Multi-city scoping is data: every row carries it")
    name: str
    short_code: str | None = None
    type: str
    status: DocumentStatus
    source: str | None = None
    source_url: str | None = None
    zone_id: int | None = None
    zone_name: str | None = None
    amends_document_id: int | None = None
    licence_note: str | None = None
    adopted_on: date | None = None
    file: FileSummary | None = Field(default=None, description="The primary file")
    page_count: int | None = None
    files: list[DocumentFileOut] = Field(
        default_factory=list, description="Every file of this version, in display order"
    )
    state: DocumentState = Field(
        default="no_files",
        description=(
            "no_files | processing | ready_for_review | failed | published | reviewed | "
            "not_extracted (first match, in that order)"
        ),
    )
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
    georeference: GeoreferenceOut | None = Field(
        default=None,
        description="The latest georeferencing run of this version: RMSE per sheet, snapping",
    )


class DocumentList(BaseModel):
    municipality: MunicipalityRef = Field(description="The municipality these documents belong to")
    items: list[DocumentOut]
    total: int = Field(default=0, description="Matching documents before paging")
    limit: int
    offset: int


class CoverageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live: bool
