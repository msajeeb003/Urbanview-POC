"""Location resolvers.

``PostgisResolver`` answers both endpoints with one PostGIS statement per call (see
``api.services.locate_sql``). ``NoDataResolver`` is a no-database stand-in (unit tests,
``LOCATION_RESOLVER=nodata``). Whatever the implementation, a resolver returns a
``LocationResolution`` for every valid input and never raises for "no data here": an uncovered
point, or a parcel reference that matches nothing, is a normal 200 result (BRD S6).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.locate import (
    AreaComparison,
    CadastralParcelSummary,
    Coverage,
    CoverageReason,
    CoverageStatus,
    DocumentRef,
    LocateQuery,
    LocationResolution,
    PlanningDocumentSummary,
    UrbanBlockSummary,
    UrbanParcelSummary,
    ZoneSummary,
)
from api.services.locate_sql import LOCATE_PARCEL_SQL, LOCATE_POINT_SQL
from core.municipality import MunicipalityProfile

log = logging.getLogger("urbanview.locate")

NO_ADOPTED_PLAN_MESSAGE = "No adopted planning document covers this location."
AREA_DIFFERS_THRESHOLD_PCT = 0.5


class LocationResolver(Protocol):
    async def resolve_point(self, *, lat: float, lng: float) -> LocationResolution: ...

    async def resolve_parcel(
        self, *, ko: str, parcel_number: str, sub_number: str | None
    ) -> LocationResolution: ...


def uncovered(
    query: LocateQuery,
    reason: CoverageReason,
    message: str,
    *,
    cadastral_parcel: CadastralParcelSummary | None = None,
) -> LocationResolution:
    return LocationResolution(
        query=query,
        covered=False,
        coverage=Coverage(status=CoverageStatus.uncovered, reason=reason, message=message),
        cadastral_parcel=cadastral_parcel,
        centroid=cadastral_parcel.centroid if cadastral_parcel else None,
    )


def outside_message(profile: MunicipalityProfile) -> str:
    return f"This location is outside the {profile.name} coverage area."


def parcel_not_found_message(ko: str, parcel_number: str, sub_number: str | None) -> str:
    ref = f"{parcel_number}/{sub_number}" if sub_number else parcel_number
    return f"No cadastral parcel {ref} is recorded in cadastral municipality {ko}."


class NoDataResolver:
    """No planning geometry: every point is uncovered and every parcel lookup finds nothing."""

    def __init__(self, profile: MunicipalityProfile) -> None:
        self.profile = profile

    async def resolve_point(self, *, lat: float, lng: float) -> LocationResolution:
        query = LocateQuery(mode="point", municipality_id=self.profile.id, lat=lat, lng=lng)
        if not self.profile.contains(lng, lat):
            return uncovered(
                query, CoverageReason.outside_municipality, outside_message(self.profile)
            )
        return uncovered(query, CoverageReason.no_adopted_plan, NO_ADOPTED_PLAN_MESSAGE)

    async def resolve_parcel(
        self, *, ko: str, parcel_number: str, sub_number: str | None
    ) -> LocationResolution:
        query = LocateQuery(
            mode="parcel",
            municipality_id=self.profile.id,
            ko=ko,
            parcel_number=parcel_number,
            sub_number=sub_number,
        )
        return uncovered(
            query,
            CoverageReason.parcel_not_found,
            parcel_not_found_message(ko, parcel_number, sub_number),
        )


def _as_json(value: Any) -> Any:
    """asyncpg may hand jsonb back decoded or as text depending on codec setup."""
    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


class PostgisResolver:
    """One statement per call against the published location tables."""

    def __init__(
        self,
        profile: MunicipalityProfile,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        min_overlap_m2: float = 1.0,
        min_overlap_fraction: float = 0.02,
    ) -> None:
        self.profile = profile
        self.session_factory = session_factory
        self.min_overlap_m2 = float(min_overlap_m2)
        self.min_overlap_fraction = float(min_overlap_fraction)

    async def resolve_point(self, *, lat: float, lng: float) -> LocationResolution:
        query = LocateQuery(mode="point", municipality_id=self.profile.id, lat=lat, lng=lng)
        if not self.profile.contains(lng, lat):
            # Outside the municipality's declared extent: nothing to look up.
            return uncovered(
                query, CoverageReason.outside_municipality, outside_message(self.profile)
            )
        row = await self._execute(LOCATE_POINT_SQL, {"lat": lat, "lng": lng})
        return self._assemble(row, query)

    async def resolve_parcel(
        self, *, ko: str, parcel_number: str, sub_number: str | None
    ) -> LocationResolution:
        row = await self._execute(
            LOCATE_PARCEL_SQL,
            {"ko": ko, "parcel_number": parcel_number, "sub_number": sub_number},
        )
        query = LocateQuery(
            mode="parcel",
            municipality_id=self.profile.id,
            ko=ko,
            parcel_number=parcel_number,
            sub_number=sub_number,
            lat=row["anchor_lat"],
            lng=row["anchor_lng"],
        )
        if _as_json(row["cadastral_parcel"]) is None:
            return uncovered(
                query,
                CoverageReason.parcel_not_found,
                parcel_not_found_message(ko, parcel_number, sub_number),
            )
        resolution = self._assemble(row, query)
        # The parcel's own centroid is the reference point for a parcel lookup.
        if resolution.centroid is not None:
            resolution.query.lat = resolution.centroid.lat
            resolution.query.lng = resolution.centroid.lng
        return resolution

    async def _execute(self, sql: str, params: dict[str, Any]) -> Mapping[str, Any]:
        bound = {
            "municipality_id": self.profile.id,
            "min_overlap_m2": self.min_overlap_m2,
            "min_overlap_fraction": self.min_overlap_fraction,
            **params,
        }
        async with self.session_factory() as session:
            result = await session.execute(text(sql), bound)
            return result.mappings().one()

    def _assemble(self, row: Mapping[str, Any], query: LocateQuery) -> LocationResolution:
        cad_raw = _as_json(row["cadastral_parcel"])
        doc_raw = _as_json(row["planning_document"])
        ups_raw = _as_json(row["urban_parcels"]) or []
        blk_raw = _as_json(row["urban_block"])
        zone_raw = _as_json(row["zone"])
        zone_docs_raw = _as_json(row["zone_documents"]) or []

        cadastral = CadastralParcelSummary(**cad_raw) if cad_raw else None
        document = PlanningDocumentSummary(**doc_raw) if doc_raw else None
        centroid = cadastral.centroid if cadastral else None

        if document is None:
            return uncovered(
                query,
                CoverageReason.no_adopted_plan,
                NO_ADOPTED_PLAN_MESSAGE,
                cadastral_parcel=cadastral,
            )

        urban_parcels: list[UrbanParcelSummary] = []
        for raw in ups_raw:
            overlap_pct = None
            if raw.get("overlap_m2") is not None and cadastral and cadastral.area_m2 > 0:
                overlap_pct = round(raw["overlap_m2"] / cadastral.area_m2 * 100, 2)
            urban_parcels.append(UrbanParcelSummary(**raw, overlap_pct=overlap_pct))
        primary = urban_parcels[0] if urban_parcels else None

        comparison = None
        if cadastral and primary:
            delta = primary.area_m2 - cadastral.area_m2
            pct = (delta / cadastral.area_m2 * 100) if cadastral.area_m2 else 0.0
            comparison = AreaComparison(
                cadastral_area_m2=cadastral.area_m2,
                urban_parcel_area_m2=primary.area_m2,
                delta_m2=round(delta, 1),
                delta_pct=round(pct, 2),
                differs=abs(pct) > AREA_DIFFERS_THRESHOLD_PCT,
            )

        if primary is not None:
            basis: str | None = "urban"
        elif cadastral is not None:
            basis = "cadastral"
        else:
            basis = None

        zone = None
        if zone_raw:
            zone = ZoneSummary(
                **zone_raw, planning_documents=[DocumentRef(**d) for d in zone_docs_raw]
            )

        status_label = document.status.replace("_", " ")
        return LocationResolution(
            query=query,
            covered=True,
            coverage=Coverage(
                status=CoverageStatus.covered,
                reason=None,
                message=f"Covered by {document.name} ({status_label}).",
            ),
            cadastral_parcel=cadastral,
            urban_parcel=primary,
            urban_parcels=urban_parcels,
            urban_block=UrbanBlockSummary(**blk_raw) if blk_raw else None,
            zone=zone,
            planning_document=document,
            calculation_basis=basis,  # type: ignore[arg-type]
            area_comparison=comparison,
            centroid=centroid,
        )
