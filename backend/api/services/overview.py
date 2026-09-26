"""The admin console's Overview: the four stat cards and the pipeline status per district.

A district is an UrbanView zone. Per zone: its current planning documents, how far their AI
extraction went (the latest ``extraction_runs`` row per document), how much of what was
extracted an expert has decided (items not superseded), and whether its adopted documents are
live on the map (a live coverage, the rule locate and the tiles use). Two statements; nothing
cached (staff traffic only).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.overview import DistrictStatus, OverviewOut, OverviewTotals
from core.municipality import MunicipalityProfile

TOTALS_SQL = text(
    """
    WITH docs AS (
        SELECT status FROM planning_documents
        WHERE municipality_id = :m AND is_current_version
    ), paid AS (
        SELECT price_eur, payment_amount_eur, paid_at FROM orders
        WHERE municipality_id = :m AND status IN ('paid', 'in_progress', 'delivered')
    )
    SELECT
        (SELECT count(*) FROM cadastral_parcels WHERE municipality_id = :m) AS parcels,
        (SELECT count(*) FROM docs) AS documents,
        (SELECT count(*) FROM docs WHERE status = 'adopted') AS documents_adopted,
        (SELECT count(*) FROM docs WHERE status = 'in_progress') AS documents_in_progress,
        (SELECT count(*) FROM docs WHERE status = 'superseded') AS documents_superseded,
        (SELECT count(*) FROM planning_parameter_extractions
         WHERE municipality_id = :m AND review_state = 'pending_review'
           AND superseded_at IS NULL) AS pending_review,
        (SELECT count(*) FROM paid) AS paid_orders,
        (SELECT count(*) FROM paid WHERE paid_at >= now() - interval '7 days')
            AS paid_orders_last_7_days,
        (SELECT COALESCE(sum(COALESCE(payment_amount_eur, price_eur)), 0) FROM paid)
            AS revenue_eur
    """
)
DISTRICTS_SQL = text(
    """
    WITH docs AS (
        SELECT id, zone_id, status, coverage_live AND coverage_geom IS NOT NULL AS live
        FROM planning_documents
        WHERE municipality_id = :m AND is_current_version
    ), runs AS (
        SELECT DISTINCT ON (document_id) document_id, status
        FROM extraction_runs WHERE municipality_id = :m
        ORDER BY document_id, id DESC
    ), items AS (
        SELECT document_id, count(*) AS total,
               count(*) FILTER (WHERE review_state <> 'pending_review') AS reviewed
        FROM planning_parameter_extractions
        WHERE municipality_id = :m AND superseded_at IS NULL
        GROUP BY document_id
    )
    SELECT z.id AS zone_id, z.name, z.zone_type,
           count(d.id) AS documents,
           count(d.id) FILTER (WHERE d.status = 'adopted') AS documents_adopted,
           count(d.id) FILTER (WHERE d.status = 'adopted' AND d.live) AS documents_live,
           count(r.document_id) FILTER (WHERE r.status = 'ready_for_review') AS extraction_done,
           count(r.document_id) FILTER (WHERE r.status IN ('queued', 'extracting'))
               AS extraction_running,
           count(r.document_id) FILTER (WHERE r.status = 'failed') AS extraction_failed,
           COALESCE(sum(i.total), 0) AS review_items,
           COALESCE(sum(i.reviewed), 0) AS reviewed
    FROM zones z
    LEFT JOIN docs d ON d.zone_id = z.id
    LEFT JOIN runs r ON r.document_id = d.id
    LEFT JOIN items i ON i.document_id = d.id
    WHERE z.municipality_id = :m
    GROUP BY z.id, z.name, z.zone_type
    ORDER BY z.name
    """
)


def district(row: Mapping[str, Any]) -> DistrictStatus:
    documents = int(row["documents"])
    done, running = int(row["extraction_done"]), int(row["extraction_running"])
    if documents == 0:
        extraction = "none"
    elif done >= documents:
        extraction = "done"
    elif running or done:
        extraction = "in_progress"
    else:
        extraction = "queued"
    items, reviewed = int(row["review_items"]), int(row["reviewed"])
    adopted, live = int(row["documents_adopted"]), int(row["documents_live"])
    return DistrictStatus(
        zone_id=int(row["zone_id"]),
        name=row["name"],
        zone_type=row["zone_type"],
        documents=documents,
        documents_adopted=adopted,
        extraction=extraction,
        extraction_done=done,
        extraction_running=running,
        extraction_failed=int(row["extraction_failed"]),
        review_items=items,
        review_pct=round(100 * reviewed / items, 1) if items else None,
        live="yes" if adopted and live >= adopted else "partial" if live else "no",
        documents_live=live,
    )


class OverviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        municipality: MunicipalityProfile,
    ) -> None:
        self.session_factory = session_factory
        self.municipality = municipality

    async def overview(self) -> OverviewOut:
        m = self.municipality.id
        async with self.session_factory() as session:
            totals = (await session.execute(TOTALS_SQL, {"m": m})).mappings().one()
            rows = (await session.execute(DISTRICTS_SQL, {"m": m})).mappings().all()
        return OverviewOut(
            municipality_id=m,
            municipality_name=self.municipality.name,
            totals=OverviewTotals(
                **{k: int(v) for k, v in totals.items() if k != "revenue_eur"},
                revenue_eur=float(totals["revenue_eur"]),
            ),
            districts=[district(row) for row in rows],
        )
