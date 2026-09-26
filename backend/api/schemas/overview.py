"""The admin console's Overview tab (``GET /v1/admin/overview``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OverviewTotals(BaseModel):
    parcels: int = Field(description="Cadastral parcels ingested")
    documents: int = Field(description="Planning documents (current versions)")
    documents_adopted: int
    documents_in_progress: int
    documents_superseded: int
    pending_review: int = Field(description="AI-extracted items waiting for an expert")
    paid_orders: int = Field(description="Orders paid, in progress or delivered")
    paid_orders_last_7_days: int
    revenue_eur: float = Field(description="Received amounts (the price where none was recorded)")


class DistrictStatus(BaseModel):
    zone_id: int
    name: str
    zone_type: str | None
    documents: int
    documents_adopted: int
    extraction: Literal["none", "queued", "in_progress", "done"] = Field(
        description="none: the zone has no documents; queued: nothing extracted yet; "
        "in_progress: runs going or only part of the documents read; done: every document read"
    )
    extraction_done: int
    extraction_running: int
    extraction_failed: int
    review_items: int
    review_pct: float | None = Field(
        description="Reviewed / extracted items, 0-100; null when nothing was extracted"
    )
    live: Literal["yes", "partial", "no"] = Field(
        description="Adopted documents with a live coverage: all of them, some, none"
    )
    documents_live: int


class OverviewOut(BaseModel):
    municipality_id: str
    municipality_name: str
    totals: OverviewTotals
    districts: list[DistrictStatus]
