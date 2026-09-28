"""What "covered" means in SQL: an adopted, live, current planning document with a coverage
geometry, the documents location resolution takes (``api/services/locate_sql.py``).

Outside coverage the public map is the base map alone (BRD S6: "zone geometry absent"; the POC
plan): the zones layer draws covered zones only, the heatmaps compute and carry covered zones and
blocks only, and cadastral parcels carry ``covered`` so the map draws the covered ones. Both
fragments are plain SQL for the publish catalogue, the heatmap job and the zone index.
"""

from __future__ import annotations

LIVE_DOCUMENT = (
    "d.status = 'adopted' AND d.coverage_live AND d.is_current_version "
    "AND d.coverage_geom IS NOT NULL"
)

# A zone (alias ``z``) is covered when one of its planning documents is live.
ZONE_COVERED = f"""EXISTS (
    SELECT 1 FROM planning_documents d
    WHERE d.zone_id = z.id AND d.municipality_id = z.municipality_id AND {LIVE_DOCUMENT})"""


def point_covered(geom: str, municipality: str = ":m") -> str:
    """The point on surface of ``geom`` lies in a live document's coverage (locate's rule for a
    parcel); ``municipality`` is the SQL expression of the municipality id."""
    return f"""EXISTS (
    SELECT 1 FROM planning_documents d
    WHERE d.municipality_id = {municipality} AND {LIVE_DOCUMENT}
      AND ST_Intersects(d.coverage_geom, ST_PointOnSurface({geom})))"""
