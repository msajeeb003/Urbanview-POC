"""Geometry review (``/v1/admin/geometry``): staged geometry is reviewed like the extracted values
before the publish job applies it (the pilot technical scope's ``staging.geometry_draft`` with
origin, QA status and issues; A2 "100% of items reviewed").

A draft is one staged batch: one layer of one producing run (a georeferenced plan or GIS drawing,
a zone import, a cadastral import), with its topology QA (``core.geometry_qa``, computed when it
was staged; a batch staged before 0033 is checked when first approved). *Approve* marks it for the
next publish; a batch whose QA fails cannot be approved. *Reject* (a reason required) takes it out
of the staged pool for good: the geometry is fixed and staged again, which makes a new batch
(a rejected batch never returns, so the land-use layer's carry-forward can never pick it up).
A decision on a published or superseded batch is refused (409). Every decision writes one
append-only ``audit_log`` row (``geometry.approve`` / ``geometry.reject``, entity
``geometry_batch``) with the state before and after. Nothing here writes to the serving tables.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.geometry_review import (
    GeometryBlocker,
    GeometryBulkApproveIn,
    GeometryBulkResult,
    GeometryBulkSkipped,
    GeometryCounts,
    GeometryDatasetRef,
    GeometryDocumentRef,
    GeometryDraft,
    GeometryFeatures,
    GeometryPage,
    QaIssueOut,
)
from api.services.admin import _GEOREF_JSON, _georeference
from api.services.audit import write_audit
from core.auth import Principal
from core.errors import ConflictError, NotFoundError
from core.geometry_qa import LAYER_LABELS, run_batch_qa
from core.municipality import MunicipalityProfile
from jobs.publish_pipeline import GEOMETRY_PENDING_SQL

REVIEW_TO_DB = {"pending": "pending_review", "approved": "approved", "rejected": "rejected"}
DB_TO_REVIEW = {db: api for api, db in REVIEW_TO_DB.items()}
PREVIEW_MAX_FEATURES = 3000
PREVIEW_RESOLUTION = 1500  # simplify to about this many steps across the batch's extent

_DRAFT_SQL = f"""
    SELECT b.id, b.layer_id, b.origin, b.status, b.review_state, b.feature_count, b.produced_by,
           b.created_at, b.qa_status, b.qa_issues, b.reviewer, b.reviewed_at, b.review_note,
           b.published_version_id, b.published_at, b.document_id, b.dataset_version,
           d.name AS document_name, d.short_code AS document_short_code,
           d.type AS document_type, gr.georef,
           z.dataset_version AS zones_version, z.status AS zones_status,
           c.dataset_version AS cadastre_version, c.status AS cadastre_status,
           ext.west, ext.south, ext.east, ext.north,
           count(*) OVER () AS total
    FROM geometry_batches b
    LEFT JOIN planning_documents d ON d.id = b.document_id
    LEFT JOIN LATERAL (
        SELECT {_GEOREF_JSON} AS georef FROM georef_datasets g
        WHERE g.municipality_id = b.municipality_id
          AND g.batches @> jsonb_build_object(b.layer_id, b.id)
        ORDER BY g.id DESC LIMIT 1
    ) gr ON true
    LEFT JOIN zone_datasets z ON z.zones_batch_id = b.id
    LEFT JOIN cadastral_datasets c ON b.id IN (c.parcels_batch_id, c.ko_batch_id)
    LEFT JOIN LATERAL (
        SELECT ST_XMin(e) AS west, ST_YMin(e) AS south, ST_XMax(e) AS east, ST_YMax(e) AS north
        FROM (SELECT ST_Extent(geom) AS e FROM staging_geometry WHERE batch_id = b.id) x
    ) ext ON true
    WHERE b.municipality_id = :m {{extra}}
    ORDER BY (b.status = 'staged' AND b.review_state = 'pending_review') DESC,
             CASE b.qa_status WHEN 'fail' THEN 0 WHEN 'warn' THEN 1 WHEN 'pass' THEN 2 ELSE 3 END,
             (b.status = 'staged') DESC, b.id DESC
    LIMIT :limit OFFSET :offset
"""
COUNTS_SQL = text(
    """
    SELECT count(*) FILTER (WHERE status = 'staged' AND review_state = 'pending_review')
               AS pending,
           count(*) FILTER (WHERE status = 'staged' AND review_state = 'approved') AS approved,
           count(*) FILTER (WHERE status = 'rejected') AS rejected,
           count(*) FILTER (WHERE status = 'staged' AND review_state = 'pending_review'
                            AND qa_status = 'fail') AS failing
    FROM geometry_batches WHERE municipality_id = :m
    """
)
LOCK_SQL = text(
    """
    SELECT id, layer_id, origin, status, review_state, qa_status, feature_count, review_note,
           document_id, dataset_version
    FROM geometry_batches WHERE id = :id AND municipality_id = :m FOR UPDATE
    """
)
APPROVE_SQL = text(
    """
    UPDATE geometry_batches
    SET review_state = 'approved', reviewer = :reviewer, reviewed_by_user_id = :user_id,
        reviewed_at = :at, review_note = :note
    WHERE id = :id AND status = 'staged'
    RETURNING id
    """
)
REJECT_SQL = text(
    """
    UPDATE geometry_batches
    SET status = 'rejected', review_state = 'rejected', reviewer = :reviewer,
        reviewed_by_user_id = :user_id, reviewed_at = :at, review_note = :note
    WHERE id = :id AND status = 'staged'
    RETURNING id
    """
)
BULK_CANDIDATES_SQL = """
    SELECT id FROM geometry_batches
    WHERE municipality_id = :m AND status = 'staged' AND review_state = 'pending_review'
      AND ({selectors})
    ORDER BY id
"""
FEATURES_SQL = text(
    """
    WITH ext AS (SELECT ST_Extent(geom) AS e FROM staging_geometry WHERE batch_id = :batch),
    tol AS (
        SELECT GREATEST(ST_XMax(e) - ST_XMin(e), ST_YMax(e) - ST_YMin(e))
               / CAST(:resolution AS float8) AS t
        FROM ext
    )
    SELECT s.feature_key,
           COALESCE(s.properties->>'urban_parcel_number', s.properties->>'block_ref',
                    s.properties->>'name',
                    CASE WHEN s.properties ? 'parcel_number'
                         THEN (s.properties->>'parcel_number')
                              || COALESCE('/' || NULLIF(s.properties->>'sub_number', ''), '')
                    END,
                    s.properties->>'ko_name', s.feature_key) AS label,
           round(CAST(ST_Area(ST_Transform(s.geom, CAST(:srid AS integer))) AS numeric), 1)
               AS area_m2,
           ST_AsGeoJSON(CASE WHEN tol.t > 0 THEN ST_SimplifyPreserveTopology(s.geom, tol.t)
                             ELSE s.geom END, 7) AS geometry,
           count(*) OVER () AS total
    FROM staging_geometry s CROSS JOIN tol
    WHERE s.batch_id = :batch
    ORDER BY s.feature_key
    LIMIT :limit
    """
)


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def _dataset(row: Mapping[str, Any]) -> GeometryDatasetRef | None:
    georef = row.get("georef")
    if georef:
        return GeometryDatasetRef(
            kind="georef", version=georef["dataset_version"], status=georef["status"]
        )
    if row.get("zones_version"):
        return GeometryDatasetRef(
            kind="zones", version=row["zones_version"], status=row["zones_status"]
        )
    if row.get("cadastre_version"):
        return GeometryDatasetRef(
            kind="cadastre", version=row["cadastre_version"], status=row["cadastre_status"]
        )
    return None


def approve_blocker(status: str, qa_status: str | None) -> str | None:
    if status != "staged":
        return status  # published | superseded | rejected
    return "qa_failed" if qa_status == "fail" else None


def _draft_out(row: Mapping[str, Any]) -> GeometryDraft:
    bbox = None
    if row.get("west") is not None:
        bbox = [float(row[k]) for k in ("west", "south", "east", "north")]
    georef = row.get("georef")
    blocker = approve_blocker(row["status"], row["qa_status"])
    return GeometryDraft(
        id=row["id"],
        layer_id=row["layer_id"],
        layer_label=LAYER_LABELS.get(row["layer_id"], row["layer_id"]),
        origin=row["origin"],
        status=row["status"],
        review_status=DB_TO_REVIEW.get(row["review_state"]) if row["review_state"] else None,
        feature_count=int(row["feature_count"] or 0),
        document=GeometryDocumentRef(
            id=row["document_id"],
            name=row["document_name"],
            short_code=row["document_short_code"],
            type=row["document_type"],
        )
        if row.get("document_id") is not None and row.get("document_name") is not None
        else None,
        dataset=_dataset(row),
        dataset_version=row["dataset_version"],
        produced_by=row["produced_by"],
        created_at=_utc(row["created_at"]),
        qa_status=row["qa_status"],
        qa_issues=[QaIssueOut(**issue) for issue in row.get("qa_issues") or []],
        bbox=bbox,
        reviewed_by=row["reviewer"],
        reviewed_at=_utc(row["reviewed_at"]),
        review_note=row["review_note"],
        published_version_id=row["published_version_id"],
        published_at=_utc(row["published_at"]),
        georeference=_georeference(georef) if georef else None,
        can_approve=blocker is None,
        approve_blocker=blocker,
        can_reject=row["status"] == "staged",
    )


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": row["status"],
        "review_status": DB_TO_REVIEW.get(row["review_state"]) if row["review_state"] else None,
        "qa_status": row["qa_status"],
        "note": row["review_note"],
    }


class GeometryReviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        municipality: MunicipalityProfile,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.municipality = municipality
        self.clock = clock

    @property
    def municipality_id(self) -> str:
        return self.municipality.id

    # --- reads -----------------------------------------------------------------------------------

    async def list_drafts(
        self,
        *,
        status: str | None = None,
        origin: str | None = None,
        layer_id: str | None = None,
        document_id: int | None = None,
        dataset_version: str | None = None,
        qa_status: str | None = None,
        include_history: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> GeometryPage:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        if status == "pending":
            clauses.append("AND b.status = 'staged' AND b.review_state = 'pending_review'")
        elif status is not None:
            clauses.append("AND b.review_state = :review_state")
            params["review_state"] = REVIEW_TO_DB[status]
        if not include_history:
            clauses.append("AND b.status IN ('staged', 'rejected')")
        for column, value in (
            ("origin", origin),
            ("layer_id", layer_id),
            ("document_id", document_id),
            ("dataset_version", dataset_version),
            ("qa_status", qa_status),
        ):
            if value is not None:
                clauses.append(f"AND b.{column} = :{column}")
                params[column] = value
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_DRAFT_SQL.format(extra=" ".join(clauses))), params))
                .mappings()
                .all()
            )
            counts = (
                (await session.execute(COUNTS_SQL, {"m": self.municipality_id})).mappings().one()
            )
        return GeometryPage(
            items=[_draft_out(r) for r in rows],
            total=int(rows[0]["total"]) if rows else 0,
            limit=limit,
            offset=offset,
            counts=GeometryCounts(**{k: int(v or 0) for k, v in counts.items()}),
        )

    async def get_draft(self, batch_id: int) -> GeometryDraft:
        async with self.session_factory() as session:
            return _draft_out(await self._draft_row(session, batch_id))

    async def _draft_row(self, session: AsyncSession, batch_id: int) -> Mapping[str, Any]:
        row = (
            (
                await session.execute(
                    text(_DRAFT_SQL.format(extra="AND b.id = :id")),
                    {"m": self.municipality_id, "id": batch_id, "limit": 1, "offset": 0},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(
                f"No geometry batch with id {batch_id}", details={"batch_id": batch_id}
            )
        return row

    async def counts(self) -> GeometryCounts:
        async with self.session_factory() as session:
            row = (await session.execute(COUNTS_SQL, {"m": self.municipality_id})).mappings().one()
        return GeometryCounts(**{k: int(v or 0) for k, v in row.items()})

    async def blockers(self, session: AsyncSession) -> list[GeometryBlocker]:
        rows = (
            (await session.execute(GEOMETRY_PENDING_SQL, {"m": self.municipality_id}))
            .mappings()
            .all()
        )
        return [geometry_blocker(r) for r in rows]

    async def features(self, batch_id: int) -> GeometryFeatures:
        from core.parcel_links import metric_srid

        async with self.session_factory() as session:
            draft = await self._draft_row(session, batch_id)
            rows = (
                (
                    await session.execute(
                        FEATURES_SQL,
                        {
                            "batch": batch_id,
                            "resolution": PREVIEW_RESOLUTION,
                            "srid": metric_srid(self.municipality_id),
                            "limit": PREVIEW_MAX_FEATURES,
                        },
                    )
                )
                .mappings()
                .all()
            )
        marked: dict[str, list[str]] = {}
        gaps: list[list[float]] = []
        for issue in draft.get("qa_issues") or []:
            for key in issue.get("keys") or []:
                marked.setdefault(key, []).append(issue["code"])
            gaps += [list(p) for p in issue.get("locations") or []]
        features = [
            {
                "type": "Feature",
                "id": r["feature_key"],
                "geometry": json.loads(r["geometry"]) if r["geometry"] else None,
                "properties": {
                    "key": r["feature_key"],
                    "label": r["label"],
                    "area_m2": float(r["area_m2"]) if r["area_m2"] is not None else None,
                    "issues": marked.get(r["feature_key"], []),
                },
            }
            for r in rows
        ]
        total = int(rows[0]["total"]) if rows else 0
        bbox = None
        if draft.get("west") is not None:
            bbox = [float(draft[k]) for k in ("west", "south", "east", "north")]
        return GeometryFeatures(
            batch_id=batch_id,
            layer_id=draft["layer_id"],
            bbox=bbox,
            total=total,
            truncated=total > len(features),
            features={"type": "FeatureCollection", "features": features},
            gaps=gaps,
        )

    # --- decisions -------------------------------------------------------------------------------

    async def approve(self, principal: Principal, batch_id: int, note: str | None) -> GeometryDraft:
        async with self.session_factory() as session:
            try:
                await self._approve(session, principal, batch_id, note)
            except ConflictError:
                await session.commit()  # a QA outcome computed just now is kept
                raise
            await session.commit()
        return await self.get_draft(batch_id)

    async def _approve(
        self, session: AsyncSession, principal: Principal, batch_id: int, note: str | None
    ) -> None:
        row = await self._lock(session, batch_id)
        self._open_or_raise(row)
        before = _snapshot(row)
        qa_status = row["qa_status"]
        if qa_status is None:  # staged before geometry review: checked now
            qa_status = (
                await run_batch_qa(session, batch_id, municipality_id=self.municipality_id)
            ).status
        if qa_status == "fail":
            raise ConflictError(
                "The geometry fails its checks (invalid or empty features): fix it and stage it "
                "again, or reject it",
                details={"batch_id": batch_id, "reason": "qa_failed"},
            )
        await session.execute(
            APPROVE_SQL,
            {
                "id": batch_id,
                "reviewer": principal.subject,
                "user_id": principal.user_id,
                "at": self.clock(),
                "note": note,
            },
        )
        after = {**before, "review_status": "approved", "qa_status": qa_status, "note": note}
        await self._audit(session, principal, row, "approve", before, after, note)

    async def reject(self, principal: Principal, batch_id: int, note: str) -> GeometryDraft:
        async with self.session_factory() as session:
            row = await self._lock(session, batch_id)
            self._open_or_raise(row)
            before = _snapshot(row)
            await session.execute(
                REJECT_SQL,
                {
                    "id": batch_id,
                    "reviewer": principal.subject,
                    "user_id": principal.user_id,
                    "at": self.clock(),
                    "note": note,
                },
            )
            after = {**before, "status": "rejected", "review_status": "rejected", "note": note}
            await self._audit(session, principal, row, "reject", before, after, note)
            await session.commit()
        return await self.get_draft(batch_id)

    async def bulk_approve(
        self, principal: Principal, payload: GeometryBulkApproveIn
    ) -> GeometryBulkResult:
        approved: list[int] = []
        skipped: list[GeometryBulkSkipped] = []
        async with self.session_factory() as session:
            candidates: list[int] = list(dict.fromkeys(payload.batch_ids or []))
            selectors: list[str] = []
            params: dict[str, Any] = {"m": self.municipality_id}
            if payload.dataset_version is not None:
                selectors.append("dataset_version = :dataset_version")
                params["dataset_version"] = payload.dataset_version
            if payload.document_id is not None:
                selectors.append("document_id = :document_id")
                params["document_id"] = payload.document_id
            if selectors:
                rows = await session.execute(
                    text(BULK_CANDIDATES_SQL.format(selectors=" OR ".join(selectors))), params
                )
                candidates += [int(r[0]) for r in rows.all() if int(r[0]) not in candidates]
            for batch_id in candidates:
                row = (
                    (await session.execute(LOCK_SQL, {"id": batch_id, "m": self.municipality_id}))
                    .mappings()
                    .first()
                )
                if row is None:
                    skipped.append(GeometryBulkSkipped(id=batch_id, reason="not_found"))
                elif row["status"] != "staged":
                    skipped.append(GeometryBulkSkipped(id=batch_id, reason="not_open"))
                elif row["review_state"] != "pending_review":
                    skipped.append(GeometryBulkSkipped(id=batch_id, reason="not_pending"))
                else:
                    try:  # refused before anything but its QA outcome is written
                        await self._approve(session, principal, batch_id, payload.note)
                    except ConflictError:
                        skipped.append(GeometryBulkSkipped(id=batch_id, reason="qa_failed"))
                        continue
                    approved.append(batch_id)
            await session.commit()
        return GeometryBulkResult(approved=approved, skipped=skipped)

    # --- helpers ---------------------------------------------------------------------------------

    async def _lock(self, session: AsyncSession, batch_id: int) -> Mapping[str, Any]:
        row = (
            (await session.execute(LOCK_SQL, {"id": batch_id, "m": self.municipality_id}))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(
                f"No geometry batch with id {batch_id}", details={"batch_id": batch_id}
            )
        return row

    @staticmethod
    def _open_or_raise(row: Mapping[str, Any]) -> None:
        status = row["status"]
        if status == "staged":
            return
        message = {
            "published": "The geometry has been published; changes go through a new import",
            "superseded": "A newer run of the same geometry replaced this batch; review that one",
            "rejected": "The geometry was rejected; fix it and stage it again (a new batch)",
        }.get(status, f"The batch is {status}")
        raise ConflictError(message, details={"batch_id": row["id"], "reason": status})

    async def _audit(
        self,
        session: AsyncSession,
        principal: Principal,
        row: Mapping[str, Any],
        verb: str,
        before: dict[str, Any],
        after: dict[str, Any],
        note: str | None,
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action=f"geometry.{verb}",
            entity_type="geometry_batch",
            entity_id=row["id"],
            details={
                "layer_id": row["layer_id"],
                "origin": row["origin"],
                "document_id": row["document_id"],
                "dataset_version": row["dataset_version"],
                "feature_count": row["feature_count"],
            },
            before=before,
            after=after,
            note=note,
        )


def geometry_blocker(row: Mapping[str, Any]) -> GeometryBlocker:
    return GeometryBlocker(
        batch_id=row["batch_id"],
        layer_id=row["layer_id"],
        layer_label=LAYER_LABELS.get(row["layer_id"], row["layer_id"]),
        document_id=row["document_id"],
        document_name=row["document_name"],
        dataset_version=row["dataset_version"],
        qa_status=row["qa_status"],
    )
