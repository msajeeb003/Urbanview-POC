"""Schemas for the expert review queue (``/v1/admin/review``) and the audit trail
(``/v1/admin/audit``). A review item carries everything a reviewer needs to open the cited page
and check the value: the parameter with its labels, the AI value with unit, the reviewer's
corrected value when amended, the target (zone / block / urban parcel / market data), and the
source payload (document, page, bbox, raw text snippet, confidence, a signed link to the page),
and the staged payload: the value as the document printed it and what the contract did with it.
Notes are trimmed: a reason made of spaces is no reason (422)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReviewStatus = Literal["pending", "approved", "amended", "rejected"]
EntityType = Literal["urban_parcel", "zone", "block", "document", "market_data"]
# Queue order: pending first then page / parcel / field (default); page / parcel / field whatever
# the status; pending first then the lowest confidence.
ReviewSort = Literal["pending", "page", "confidence"]


class ReviewValue(BaseModel):
    text: str | None = None
    number: float | None = None
    unit: str | None = None


class ReviewTarget(BaseModel):
    entity_type: EntityType
    urban_parcel_id: int | None = None
    urban_parcel_number: str | None = None
    block_id: int | None = None
    block_ref: str | None = None
    zone_id: int | None = None
    zone_name: str | None = None
    label: str | None = Field(
        default=None,
        description=(
            "The parcel number / block label as the document prints it; the only reference "
            "when no geometry matches (flag target_unmatched)"
        ),
    )
    matched: bool = Field(
        default=True,
        description="false: a parcel / block value without a geometry id; it cannot publish",
    )


class ReviewPrevious(BaseModel):
    """The previous extraction run's item for the same target and field."""

    id: int
    status: ReviewStatus
    value: ReviewValue = Field(description="Its effective value (amended if amended)")
    run_id: int | None = None


class PageLinkOut(BaseModel):
    url: str = Field(description="Signed, short-lived URL to the cited page (image or PDF#page)")
    kind: Literal["page_image", "pdf_page"]
    content_type: Literal["image/png", "application/pdf"]
    expires_at: datetime


class ReviewSource(BaseModel):
    document_id: int
    document_name: str
    registry_url: str | None = None
    file_id: int | None = Field(
        default=None,
        description="The stored file the value was read from (a document may have several)",
    )
    file_name: str | None = None
    page: int | None = None
    bbox: list[float] | None = None
    bbox_space: Literal["pdf-points-bottom-left"] = "pdf-points-bottom-left"
    note: str | None = Field(default=None, description="e.g. 'table 3 – UP 12'")
    raw_text: str | None = Field(default=None, description="The text the value was read from")
    confidence: float | None = Field(default=None, description="Extractor confidence 0..1")
    extraction_method: Literal["text", "table", "ocr"] | None = Field(
        default=None, description="How the value was read; null for manual or seeded items"
    )
    link: PageLinkOut | None = Field(
        default=None, description="null when the document file is not stored or the page is unknown"
    )


class ReviewPayloadTable(BaseModel):
    table: str | None = None
    row: str | None = Field(default=None, description="Row label as printed, e.g. 'UP 12'")
    column: str | None = Field(default=None, description="Column header as printed")
    cell: str | None = Field(default=None, description="Grid cell id, e.g. 'r4c11'")


class ReviewPayloadFloors(BaseModel):
    notation: str
    below_ground: int
    above_ground: int
    attic: int


class ReviewPayload(BaseModel):
    """The staged payload of an extracted item (``planning_parameter_extractions.payload``, read
    with the reader of its schema version): the value as the document printed it, the
    contract's normalisation rules, the derived floor count or land-use class, the table cell.
    Null for manual and seeded items."""

    schema_version: str
    task: str = Field(description="The extraction task that read it, e.g. parameter_table")
    path: str = Field(description="Where in the canonical result the value sits")
    field_key: str
    urban_parcel_number: str | None = None
    block_ref: str | None = None
    stated_value: str = Field(description="The value exactly as printed")
    stated_unit: str | None = Field(default=None, description="How the unit was printed")
    value: float | str = Field(description="The canonical value the validator made of it")
    unit: str | None = None
    normalisation: list[str] = Field(
        default_factory=list, description="Rules applied, in order: decimal_comma, ratio_to_percent"
    )
    floors: ReviewPayloadFloors | None = Field(
        default=None, description="Counted from the plan's floor notation"
    )
    land_use_class: str | None = Field(
        default=None, description="The product-wide land-use class of the wording"
    )
    table: ReviewPayloadTable | None = None
    flags: list[str] = Field(default_factory=list)


class ReviewRun(BaseModel):
    """The extraction run that wrote the item: its job and what the whole run cost."""

    id: int
    job_id: int | None = None
    model_version: str | None = None
    estimated_cost_eur: float | None = Field(default=None, description="The whole run's cost")
    items_written: int | None = None
    finished_at: datetime | None = None


class ReviewItem(BaseModel):
    id: int
    status: ReviewStatus
    parameter_key: str
    label_en: str
    label_me: str
    value_type: Literal["text", "number"]
    field_unit: str | None = Field(
        default=None, description="The field dictionary's unit (the canonical one: %, m, m²)"
    )
    extracted: ReviewValue = Field(description="The AI value; never overwritten")
    amended: ReviewValue | None = Field(default=None, description="The reviewer's corrected value")
    effective: ReviewValue = Field(
        description="What would publish: amended if amended, else extracted"
    )
    target: ReviewTarget
    source: ReviewSource
    extracted_by: str
    extracted_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    published: bool = Field(description="Already copied to the serving table; decisions are closed")
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "What the extraction validator asks the reviewer to look at: low_confidence, "
            "out_of_range, unit_assumed, bbox_ambiguous, found_under_other_parcel ..."
        ),
    )
    schema_version: str | None = Field(
        default=None, description="Extraction contract version the item was stored in"
    )
    prompt_version: str | None = Field(default=None, description="Prompt set that produced it")
    run_id: int | None = Field(
        default=None, description="The extraction run that wrote it; null = manual or seeded"
    )
    run: ReviewRun | None = Field(default=None, description="That run's job and cost")
    payload: ReviewPayload | None = Field(
        default=None, description="The staged payload: as printed and how it was normalised"
    )
    change: Literal["new", "same", "changed"] | None = Field(
        default=None, description="Against the previous run's item for the same target and field"
    )
    previous: ReviewPrevious | None = None
    superseded: bool = Field(
        default=False,
        description="Replaced by a later run or decision; kept for history, decisions closed",
    )
    superseded_at: datetime | None = None
    superseded_by_run_id: int | None = None


class ReviewPage(BaseModel):
    items: list[ReviewItem]
    total: int
    limit: int
    offset: int


def _trim(value: Any) -> Any:
    """Notes are trimmed; an optional note made of spaces is no note."""
    return value.strip() if isinstance(value, str) else value


def _trim_to_none(value: Any) -> Any:
    return (value.strip() or None) if isinstance(value, str) else value


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)

    _note = field_validator("note", mode="before")(_trim_to_none)


class AmendIn(BaseModel):
    """A correction, checked with the extraction contract's rules
    (``core.extraction.corrections``): numbers in the document's conventions and the field's unit
    (ha for an area is converted), impossible values refused, a value outside the field's usual
    range only with ``confirm_out_of_range``, floors in the plan's notation, a land use the
    document or the profile knows. 422 ``validation_error`` with the rule's ``type`` otherwise."""

    model_config = ConfigDict(extra="forbid")

    value: float | str = Field(description="The corrected value: a number or a text")
    unit: str | None = Field(default=None, max_length=30)
    note: str = Field(
        min_length=1, max_length=2000, description="What was wrong (required, not blank)"
    )
    confirm_out_of_range: bool = Field(
        default=False,
        description="Keep a number outside the field's usual range (the plan really says so)",
    )

    _note = field_validator("note", mode="before")(_trim)

    @field_validator("value", mode="before")
    @classmethod
    def _no_booleans_no_blanks(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("value must be a number or a text")
        if isinstance(value, str) and not value.strip():
            raise ValueError("value must not be blank")
        return value.strip() if isinstance(value, str) else value


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000, description="The reason (not blank)")

    _note = field_validator("note", mode="before")(_trim)


class ReviewOption(BaseModel):
    value: str
    count: int = Field(description="Items of the document carrying it (not superseded)")


class ReviewOptions(BaseModel):
    """The wordings a document already uses for a text field (e.g. its land-use designations),
    most frequent first: what the reviewer picks from when correcting a value."""

    document_id: int
    field_key: str
    values: list[ReviewOption]


class ReviewCounters(BaseModel):
    document_id: int
    document_name: str
    pending: int
    approved: int
    amended: int
    rejected: int
    total: int
    can_publish: bool = Field(description="No pending items and at least one approved / amended")
    publish_blockers: list[str] = Field(default_factory=list)


class AuditEntry(BaseModel):
    id: int
    actor: str
    actor_user_id: int | None = None
    action: str
    entity_type: str | None = None
    entity_id: int | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    note: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEntry]
    total: int
    limit: int
    offset: int
