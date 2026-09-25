"""Schemas for the source viewer (``GET /v1/source/{document_id}/page/{page}``,
``GET /v1/source/value/{value_id}``): one signed, short-lived URL per cited page plus, for a value,
the box on that page. ``document_id`` and ``page`` are always present so the client can emit
``source_reference_opened``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceValue(BaseModel):
    value_id: int
    field_key: str
    label_en: str
    label_me: str
    value: float | str | None = Field(description="The published value (number or text)")
    unit: str | None = None
    urban_parcel_id: int | None = Field(description="null = document-level value")
    bbox: list[float] | None = Field(
        description="[x0, y0, x1, y1] of the cited value on the page, or null when unknown"
    )
    bbox_space: Literal["pdf-points-bottom-left"] = "pdf-points-bottom-left"
    note: str | None = Field(description="e.g. 'table 3 – UP 12'")


class SourcePage(BaseModel):
    document_id: int
    document_name: str
    document_status: Literal["adopted", "in_progress", "superseded"]
    page: int = Field(description="1-based page of the PDF")
    page_count: int | None = Field(description="Pages in the stored PDF; null when unknown")
    kind: Literal["page_image", "pdf_page"] = Field(
        description="page_image: a PNG of that page; pdf_page: the whole PDF, url ends with #page=N"
    )
    url: str = Field(
        description="Signed URL into the private bucket, valid until expires_at. The only thing "
        "about storage that leaves the API."
    )
    content_type: Literal["image/png", "application/pdf"]
    expires_at: datetime
    expires_in_seconds: int
    registry_url: str | None = Field(
        description="Public registry page of the document (eRegistri): the second way in"
    )
    value: SourceValue | None = Field(
        default=None, description="Present on /source/value/{value_id}: the cited value and its box"
    )
