"""Schemas for location resolution (``GET /v1/locate``, ``GET /v1/locate/parcel``).

They encode the product rules from the BRD:
- the cadastral parcel and the planned urban parcel are separate objects, never merged;
- ``calculation_basis`` names the area that drives the feasibility calculation (planned urban
  parcel when one exists, cadastral only as fallback) and ``area_comparison`` surfaces the
  difference, or its absence, whenever both exist;
- an uncovered location is a normal 200 result with ``covered=false``, never an error.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

DocumentStatus = Literal["adopted", "in_progress", "superseded"]


class CoverageStatus(StrEnum):
    covered = "covered"
    uncovered = "uncovered"


class CoverageReason(StrEnum):
    no_adopted_plan = "no_adopted_plan"
    outside_municipality = "outside_municipality"
    parcel_not_found = "parcel_not_found"


class Coverage(BaseModel):
    status: CoverageStatus
    reason: CoverageReason | None = None
    message: str = Field(description="Neutral, user-facing sentence. Never an error message.")


class LatLng(BaseModel):
    lat: float
    lng: float


class LocateQuery(BaseModel):
    mode: Literal["point", "parcel"]
    municipality_id: str
    lat: float | None = Field(default=None, description="Query point, or the parcel centroid")
    lng: float | None = None
    ko: str | None = None
    parcel_number: str | None = None
    sub_number: str | None = None


class DocumentRef(BaseModel):
    id: int
    name: str
    type: str | None = Field(default=None, description="DUP / PUP / PGR (municipality profile)")
    status: DocumentStatus


class PlanningDocumentSummary(DocumentRef):
    source: str | None = None
    source_url: str | None = None
    zone_id: int | None = None


class ZoneSummary(BaseModel):
    """UrbanView's internal city division (~city quarter) grouping several planning documents."""

    id: int
    name: str
    general_planning_summary: str | None = None
    planning_documents: list[DocumentRef] = Field(
        default_factory=list, description="All documents grouped under the zone, any status"
    )


class UrbanBlockSummary(BaseModel):
    id: int
    block_ref: str
    zone_id: int | None = None


class CadastralParcelSummary(BaseModel):
    parcel_id: int = Field(description="UrbanView's own numeric Parcel ID (cadastral_parcels.id)")
    parcel_number: str = Field(description="Official cadastral parcel number")
    sub_number: str | None = None
    ko_name: str = Field(
        description="Cadastral municipality (KO). Mandatory: the same number recurs across KOs"
    )
    street_address: str | None = None
    area_m2: float
    public_ownership: bool = False
    restitution_or_legal_burden: bool = False
    centroid: LatLng
    geometry: dict[str, Any] = Field(description="GeoJSON geometry, EPSG:4326")


class UrbanParcelSummary(BaseModel):
    id: int
    urban_parcel_number: str
    area_m2: float
    block_id: int | None = None
    block_ref: str | None = None
    planning_document: DocumentRef
    match: Literal["point", "overlap"] = Field(
        description=(
            "'point': the parcel contains the query point. 'overlap': it overlaps the cadastral "
            "parcel (point lies in land the plan takes for roads/public space, or parcel lookup)"
        )
    )
    overlap_m2: float | None = None
    overlap_pct: float | None = Field(
        default=None, description="Share of the cadastral parcel area covered by this urban parcel"
    )
    geometry: dict[str, Any] = Field(description="GeoJSON geometry, EPSG:4326")


class AreaComparison(BaseModel):
    cadastral_area_m2: float
    urban_parcel_area_m2: float
    delta_m2: float
    delta_pct: float = Field(
        description="(urban − cadastral) / cadastral × 100; negative = land taken"
    )
    differs: bool = Field(description="True when the two areas differ by more than 0.5%")


class LocationResolution(BaseModel):
    query: LocateQuery
    covered: bool
    coverage: Coverage
    cadastral_parcel: CadastralParcelSummary | None = None
    urban_parcel: UrbanParcelSummary | None = Field(
        default=None, description="Primary planned urban parcel: the calculation basis when present"
    )
    urban_parcels: list[UrbanParcelSummary] = Field(
        default_factory=list,
        description="All planned urban parcels at the point / overlapping the cadastral parcel, "
        "primary first",
    )
    urban_block: UrbanBlockSummary | None = None
    zone: ZoneSummary | None = None
    planning_document: PlanningDocumentSummary | None = Field(
        default=None, description="Governing adopted document (most specific coverage wins)"
    )
    calculation_basis: Literal["urban", "cadastral"] | None = Field(
        default=None,
        description="Which area drives the feasibility calculation (same vocabulary as /v1/panel)",
    )
    area_comparison: AreaComparison | None = Field(
        default=None,
        description="Cadastral vs planned urban parcel area; present whenever both exist so the "
        "mismatch (or its absence) is always surfaced",
    )
    centroid: LatLng | None = Field(
        default=None, description="Cadastral parcel centroid: where the map should pan"
    )
