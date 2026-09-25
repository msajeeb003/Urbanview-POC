"""The zone index for the search box: every zone of the municipality with its type, whether an
adopted plan covers it (the same rule as the map's tiles), its bounding box and a label point.

Zones are UrbanView's own city divisions (a handful to a few dozen per municipality), so the
whole list is served in one small response and matched in the browser as the visitor types.
Each zone carries a simplified outline (about 5 m) so the browser can say which zone an address
suggestion falls in ("Address · Centar") and flag suggestions outside coverage; `/v1/locate`
stays the authority once a suggestion is picked.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.zone_index import ZoneIndex, ZoneIndexEntry
from jobs.publish_layers import ZONE_COVERED

# ~5 m at Podgorica's latitude: enough to place an address in its zone, a few KB per zone.
SIMPLIFY_DEGREES = 0.00005

ZONE_INDEX_SQL = text(
    f"""
    SELECT z.id, z.name, z.zone_type, {ZONE_COVERED} AS covered,
           ST_XMin(z.geom::box2d) AS min_lng, ST_YMin(z.geom::box2d) AS min_lat,
           ST_XMax(z.geom::box2d) AS max_lng, ST_YMax(z.geom::box2d) AS max_lat,
           ST_X(ST_PointOnSurface(z.geom)) AS lng, ST_Y(ST_PointOnSurface(z.geom)) AS lat,
           ST_AsGeoJSON(ST_Multi(ST_SimplifyPreserveTopology(z.geom, :tolerance)), 6) AS geometry
    FROM zones z
    WHERE z.municipality_id = :m
    ORDER BY z.name, z.id
    """
)


def build_index(rows: list[dict[str, Any]]) -> ZoneIndex:
    return ZoneIndex(
        zones=[
            ZoneIndexEntry(
                id=r["id"],
                name=r["name"],
                zone_type=r["zone_type"],
                covered=bool(r["covered"]),
                bbox=(
                    round(r["min_lng"], 6),
                    round(r["min_lat"], 6),
                    round(r["max_lng"], 6),
                    round(r["max_lat"], 6),
                ),
                centroid={"lng": round(r["lng"], 6), "lat": round(r["lat"], 6)},
                geometry=_geometry(r["geometry"]),
            )
            for r in rows
        ]
    )


def _geometry(raw: Any) -> dict[str, Any]:
    geometry = json.loads(raw) if isinstance(raw, str) else raw
    return {"type": geometry["type"], "coordinates": geometry["coordinates"]}


class ZoneIndexService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, municipality_id: str):
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def index(self) -> ZoneIndex:
        async with self.session_factory() as session:
            params = {"m": self.municipality_id, "tolerance": SIMPLIFY_DEGREES}
            rows = (await session.execute(ZONE_INDEX_SQL, params)).mappings().all()
        return build_index([dict(r) for r in rows])
