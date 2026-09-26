"""Schemas for the expert review queue (``/v1/admin/review``) and the audit trail
(``/v1/admin/audit``). A review item carries everything a reviewer needs to open the cited page
and check the value: the parameter with its labels, the AI value with unit, the reviewer's
corrected value when amended, the target (zone / block / urban parcel / market data), and the
source payload (document, page, bbox, raw text snippet, confidence, a signed link to the page)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReviewStatus = Literal["pending", "approved", "amended", "rejected"]
EntityType = Literal["urban_parcel", "zone", "block", "document", "market_data"]


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


class ReviewItem(BaseModel):
    id: int
    status: ReviewStatus
    parameter_key: str
    label_en: str
    label_me: str
    value_type: Literal["text", "number"]
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


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class AmendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | str = Field(description="The corrected value: a number or a text")
    unit: str | None = Field(default=None, max_length=30)
    note: str | None = Field(default=None, max_length=2000)

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

    note: str = Field(min_length=1, max_length=2000, description="The reason")


class BulkApproveIn(BaseModel):
    """Approve many pending items at once: by ids, by document page, or by urban parcel."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[int] | None = Field(default=None, max_length=500)
    document_id: int | None = Field(default=None, gt=0)
    source_page: int | None = Field(default=None, ge=1, description="needs document_id")
    urban_parcel_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _a_selector(self) -> BulkApproveIn:
        if not self.item_ids and self.document_id is None and self.urban_parcel_id is None:
            raise ValueError("give item_ids, a document_id (+ source_page) or an urban_parcel_id")
        if self.source_page is not None and self.document_id is None:
            raise ValueError("source_page needs a document_id")
        if self.item_ids is not None and any(i <= 0 for i in self.item_ids):
            raise ValueError("item ids are positive integers")
        return self


class BulkSkipped(BaseModel):
    id: int
    reason: Literal["not_found", "not_pending", "published", "superseded"]


class BulkResult(BaseModel):
    approved: list[int]
    skipped: list[BulkSkipped]


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
