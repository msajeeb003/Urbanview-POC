"""The KO list for the parcel search: every cadastral municipality with parcels served, its
code, parcel count, bounding box and a label point (``cadastral_municipalities``, filled by the
publish job from a cadastral dataset, ``core.cadastre``). The whole list is small (a few dozen
KOs per municipality) and served in one response."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.cadastral_municipalities import (
    CadastralMunicipalities,
    CadastralMunicipalityEntry,
)

KO_LIST_SQL = text(
    """
    SELECT k.ko_name, k.ko_code, k.parcel_count, k.boundary_source, k.dataset_version,
           ST_XMin(k.geom::box2d) AS min_lng, ST_YMin(k.geom::box2d) AS min_lat,
           ST_XMax(k.geom::box2d) AS max_lng, ST_YMax(k.geom::box2d) AS max_lat,
           ST_X(ST_PointOnSurface(k.geom)) AS lng, ST_Y(ST_PointOnSurface(k.geom)) AS lat
    FROM cadastral_municipalities k
    WHERE k.municipality_id = :m AND k.parcel_count > 0 AND NOT ST_IsEmpty(k.geom)
    ORDER BY lower(k.ko_name)
    """
)


def build_list(rows: list[dict[str, Any]]) -> CadastralMunicipalities:
    versions = sorted(r["dataset_version"] for r in rows if r["dataset_version"])
    return CadastralMunicipalities(
        items=[
            CadastralMunicipalityEntry(
                ko_name=r["ko_name"],
                ko_code=r["ko_code"],
                parcel_count=int(r["parcel_count"]),
                bbox=(
                    round(r["min_lng"], 6),
                    round(r["min_lat"], 6),
                    round(r["max_lng"], 6),
                    round(r["max_lat"], 6),
                ),
                centroid={"lng": round(r["lng"], 6), "lat": round(r["lat"], 6)},
                boundary_source=r["boundary_source"],
            )
            for r in rows
        ],
        dataset_version=versions[-1] if versions else None,
    )


class CadastralMunicipalityService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, municipality_id: str):
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def list(self) -> CadastralMunicipalities:
        async with self.session_factory() as session:
            rows = (await session.execute(KO_LIST_SQL, {"m": self.municipality_id})).mappings()
            return build_list([dict(r) for r in rows])
