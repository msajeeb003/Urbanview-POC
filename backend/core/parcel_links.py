"""Cadastral ↔ planned urban parcel links of a publish version (``parcel_links``).

One statement pairs every cadastral parcel with the planned urban parcels of adopted, live,
current-version documents that it overlaps, keeps the pairs over the thresholds of location
resolution (``LOCATE_MIN_OVERLAP_M2`` and ``LOCATE_MIN_OVERLAP_FRACTION`` of the cadastral area)
and ranks them per cadastral parcel exactly like the panel's primary link: largest overlap, then
smallest planned area, then lowest id (rank 1 = the calculation basis). Used by the publish job
for every new version and by the seed loader for the seeded one; the parcel panel reads them.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LINKS_SQL = text(
    """
    WITH pairs AS (
        SELECT c.id AS cadastral_parcel_id, u.id AS urban_parcel_id,
               c.area_m2 AS cad_area, u.area_m2 AS up_area,
               ST_Area(CAST(ST_Intersection(u.geom, c.geom) AS geography)) AS overlap_m2
        FROM cadastral_parcels c
        JOIN urban_parcels u ON u.municipality_id = c.municipality_id
             AND ST_Intersects(u.geom, c.geom)
        JOIN planning_documents d ON d.id = u.document_id AND d.status = 'adopted'
             AND d.coverage_live AND d.is_current_version
        WHERE c.municipality_id = :m
    ),
    kept AS (
        SELECT *, row_number() OVER (PARTITION BY cadastral_parcel_id
                                     ORDER BY overlap_m2 DESC, up_area ASC, urban_parcel_id ASC)
                  AS rank
        FROM pairs
        WHERE overlap_m2 >= :min_m2 AND overlap_m2 >= :min_fraction * cad_area
    )
    INSERT INTO parcel_links (municipality_id, publish_version_id, cadastral_parcel_id,
                              urban_parcel_id, overlap_m2, overlap_fraction, area_delta_m2, rank)
    SELECT :m, :v, cadastral_parcel_id, urban_parcel_id, overlap_m2,
           CASE WHEN cad_area > 0 THEN overlap_m2 / cad_area ELSE 0 END,
           up_area - cad_area, rank
    FROM kept
    """
)
UNMATCHED_SQL = text(
    """
    SELECT count(*) FROM cadastral_parcels c
    WHERE c.municipality_id = :m AND NOT EXISTS (
        SELECT 1 FROM parcel_links l
        WHERE l.publish_version_id = :v AND l.cadastral_parcel_id = c.id)
    """
)
DELETE_SQL = text("DELETE FROM parcel_links WHERE publish_version_id = :v")
CURRENT_VERSION_SQL = text(
    "SELECT id FROM publish_versions WHERE municipality_id = :m AND is_current"
)


async def recompute_parcel_links(
    session: AsyncSession,
    *,
    municipality_id: str,
    version_id: int,
    min_overlap_m2: float = 1.0,
    min_overlap_fraction: float = 0.02,
) -> dict[str, int]:
    """Replace the links of ``version_id`` inside the caller's transaction (the caller commits);
    returns the number of links and of cadastral parcels left without one."""
    await session.execute(DELETE_SQL, {"v": version_id})
    result = await session.execute(
        LINKS_SQL,
        {
            "m": municipality_id,
            "v": version_id,
            "min_m2": float(min_overlap_m2),
            "min_fraction": float(min_overlap_fraction),
        },
    )
    unmatched = (
        await session.execute(UNMATCHED_SQL, {"m": municipality_id, "v": version_id})
    ).scalar_one()
    return {"parcel_links": result.rowcount or 0, "cadastral_unmatched": int(unmatched)}


async def recompute_current_links(
    session: AsyncSession,
    *,
    municipality_id: str,
    min_overlap_m2: float = 1.0,
    min_overlap_fraction: float = 0.02,
) -> dict[str, int] | None:
    """The same for the current version; ``None`` when nothing is published."""
    version_id = (
        await session.execute(CURRENT_VERSION_SQL, {"m": municipality_id})
    ).scalar_one_or_none()
    if version_id is None:
        return None
    return await recompute_parcel_links(
        session,
        municipality_id=municipality_id,
        version_id=int(version_id),
        min_overlap_m2=min_overlap_m2,
        min_overlap_fraction=min_overlap_fraction,
    )
