"""Publish API: the one button (``POST /v1/admin/publish``), its status screen
(``GET /v1/admin/publish``) and the public tiles pointer (``GET /v1/tiles/current``).

The heavy lifting is the job (``jobs.publish_pipeline``); this service refuses a publish while
items are pending review (naming the documents), enqueues one ``publish_approved`` job at a time
(idempotent) and reads ``publish_versions``. Earlier versions keep their values, links, cells and
archive (retention ``PUBLISH_KEEP_VERSIONS``) for a manual pointer flip by an operator; the
public API only ever reads the current version.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin import JobOut
from api.schemas.geometry_review import GeometryBlocker
from api.schemas.publish import (
    CellClasses,
    LayerInfo,
    PublishBlocker,
    PublishStatus,
    PublishVersionOut,
    TilesCurrent,
)
from api.services.admin import EnqueuedJob
from api.services.audit import write_audit
from api.services.jobs import JOB_JSON, job_out
from core.auth import Principal
from core.choropleth import sale_price_stale, stored_classes
from core.errors import ConflictError, NotFoundError
from jobs.enqueue import JobDispatcher, enqueue_job
from jobs.publish_pipeline import GEOMETRY_PENDING_SQL, PENDING_SQL

_VERSION_COLUMNS = """
    p.id, p.label, p.version_no, p.is_current, p.published_at, p.published_by, p.formula_version,
    p.notes, p.previous_version_id, p.job_id, p.archive_key, p.archive_size_bytes, p.archive_sha256,
    p.archive_pruned_at, p.layers, p.counts, p.duration_ms, p.min_zoom, p.max_zoom"""
VERSIONS_SQL = text(
    f"""
    SELECT {_VERSION_COLUMNS} FROM publish_versions p
    WHERE p.municipality_id = :m ORDER BY p.id DESC
    """
)
CURRENT_SQL = text(
    f"SELECT {_VERSION_COLUMNS} FROM publish_versions p "
    "WHERE p.municipality_id = :m AND p.is_current"
)
PUBLISH_JOBS_SQL = text(
    f"""
    SELECT {JOB_JSON} AS job FROM pipeline_jobs j
    WHERE j.municipality_id = :m AND j.type = 'publish_approved'
    ORDER BY j.requested_at DESC, j.id DESC LIMIT 5
    """
)


log = logging.getLogger("urbanview.publish")


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# a refresh of the version queued / running, or requested in the last minutes (a failing one is
# not retried on every map load)
RECENT_REFRESH_SQL = text(
    """
    SELECT 1 FROM pipeline_jobs
    WHERE municipality_id = :m AND type = 'refresh_heatmaps' AND target_id = :v
      AND (status IN ('queued', 'running', 'retrying')
           OR requested_at > now() - interval '10 minutes')
    LIMIT 1
    """
)


class PublishService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dispatcher: JobDispatcher,
        storage: Any,
        municipality_id: str,
        keep_versions: int = 3,
        tiles_url_expires_seconds: int = 3600,
        max_attempts: int = 1,
        timezone: str = "UTC",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.storage = storage
        self.municipality_id = municipality_id
        self.keep_versions = int(keep_versions)
        self.tiles_url_expires_seconds = int(tiles_url_expires_seconds)
        self.max_attempts = int(max_attempts)
        self.timezone = timezone
        self.clock = clock

    # --- output ----------------------------------------------------------------------------------

    def _version_out(self, row: Mapping[str, Any], *, signed: bool) -> PublishVersionOut:
        archive_url = None
        if signed and row["archive_key"]:
            archive_url = self.storage.presigned_get_url(
                row["archive_key"],
                self.tiles_url_expires_seconds,
                content_type="application/vnd.pmtiles",
            )
        return PublishVersionOut(
            id=int(row["id"]),
            label=row["label"],
            version_no=int(row["version_no"]),
            is_current=bool(row["is_current"]),
            published_at=_utc(row["published_at"]),
            published_by=row["published_by"],
            formula_version=row["formula_version"],
            notes=row["notes"],
            previous_version_id=row["previous_version_id"],
            job_id=row["job_id"],
            archive_key=row["archive_key"],
            archive_size_bytes=row["archive_size_bytes"],
            archive_sha256=row["archive_sha256"],
            archive_url=archive_url,
            archive_pruned_at=_utc(row["archive_pruned_at"]),
            layers=[LayerInfo(**layer) for layer in (row["layers"] or [])],
            counts=dict(row["counts"] or {}),
            duration_ms=row["duration_ms"],
            min_zoom=row["min_zoom"],
            max_zoom=row["max_zoom"],
        )

    # --- reads -----------------------------------------------------------------------------------

    async def blockers(self, session: AsyncSession) -> list[PublishBlocker]:
        rows = (await session.execute(PENDING_SQL, {"m": self.municipality_id})).mappings().all()
        return [
            PublishBlocker(
                document_id=int(r["document_id"]),
                document_name=r["document_name"],
                pending=int(r["pending"]),
            )
            for r in rows
        ]

    async def geometry_blockers(self, session: AsyncSession) -> list[GeometryBlocker]:
        from api.services.geometry_review import geometry_blocker

        rows = (
            (await session.execute(GEOMETRY_PENDING_SQL, {"m": self.municipality_id}))
            .mappings()
            .all()
        )
        return [geometry_blocker(r) for r in rows]

    async def status(self) -> PublishStatus:
        m = self.municipality_id
        async with self.session_factory() as session:
            versions = (await session.execute(VERSIONS_SQL, {"m": m})).mappings().all()
            jobs = [
                job_out(r["job"])
                for r in (await session.execute(PUBLISH_JOBS_SQL, {"m": m})).mappings()
            ]
            blockers = await self.blockers(session)
            geometry = await self.geometry_blockers(session)
        current = next((v for v in versions if v["is_current"]), None)
        active = next((j for j in jobs if j.status in ("queued", "running", "retrying")), None)
        return PublishStatus(
            current=self._version_out(current, signed=True) if current else None,
            versions=[self._version_out(v, signed=False) for v in versions],
            active_job=active,
            last_job=jobs[0] if jobs else None,
            can_publish=not blockers and not geometry and active is None,
            blockers=blockers,
            geometry_blockers=geometry,
            keep_versions=self.keep_versions,
        )

    async def current_tiles(self) -> TilesCurrent:
        async with self.session_factory() as session:
            row = (
                (await session.execute(CURRENT_SQL, {"m": self.municipality_id})).mappings().first()
            )
            classes = await stored_classes(session, row["id"]) if row is not None else None
        if row is None:
            return TilesCurrent(status="unpublished", data_version="unpublished")
        # a scheduled assumptions version took effect since the tiles were built: rebuild them
        refreshing = await self.refresh_heatmaps_if_stale(reason="tiles_pointer")
        version = self._version_out(row, signed=True)
        expires_at = (
            self.clock() + timedelta(seconds=self.tiles_url_expires_seconds)
            if version.archive_url
            else None
        )
        return TilesCurrent(
            status="published",
            version_id=version.id,
            version_no=version.version_no,
            data_version=version.label,
            published_at=version.published_at,
            archive_key=version.archive_key,
            archive_url=version.archive_url,
            expires_at=expires_at,
            layers=version.layers,
            min_zoom=version.min_zoom,
            max_zoom=version.max_zoom,
            cell_classes=CellClasses.model_validate(classes),
            heatmaps_refreshing=refreshing,
        )

    async def refresh_heatmaps_if_stale(self, *, reason: str) -> bool:
        """Queue ``refresh_heatmaps`` when the current version's sale-price cells come from other
        assumptions versions than the ones that apply today (saved, retired or scheduled since).
        Idempotent (one active job per version) and never raising: the heatmap stays as it was
        when the queue is down. True while a refresh is due."""
        m = self.municipality_id
        try:
            async with self.session_factory() as session:
                row = (await session.execute(CURRENT_SQL, {"m": m})).mappings().first()
                # only a version with tiles has a map heatmap to rebuild (not the seeded one)
                if (
                    row is None
                    or not row["archive_key"]
                    or not await sale_price_stale(
                        session, version_id=int(row["id"]), timezone=self.timezone, m=m
                    )
                ):
                    return False
                recent = (
                    await session.execute(RECENT_REFRESH_SQL, {"m": m, "v": int(row["id"])})
                ).first()
            if recent is not None:
                return True  # queued / running, or failed a moment ago: not once per request
            await enqueue_job(
                self.session_factory,
                self.dispatcher,
                municipality_id=m,
                job_type="refresh_heatmaps",
                payload={"reason": reason, "requested_by": f"system:{reason}"},
                target_type="publish_run",
                target_id=int(row["id"]),
                max_attempts=3,
                requested_by=f"system:{reason}",
            )
            return True
        except Exception:  # noqa: BLE001 - the pointer / the admin write must not fail on it
            log.warning("could not queue refresh_heatmaps", exc_info=True)
            return False

    # --- writes ----------------------------------------------------------------------------------

    async def enqueue(
        self, principal: Principal, *, label: str | None = None, notes: str | None = None
    ) -> EnqueuedJob:
        """Refuse while any document has items pending review or any staged geometry batch
        waits for a decision; otherwise one publish job at a time (an identical active job is
        returned as is)."""
        async with self.session_factory() as session:
            blockers = await self.blockers(session)
            geometry = await self.geometry_blockers(session)
        if blockers or geometry:
            parts = []
            if blockers:
                names = ", ".join(f"{b.document_name} ({b.pending})" for b in blockers)
                parts.append(f"items pending review in {names}")
            if geometry:
                parts.append(f"{len(geometry)} geometry batch(es) waiting for review")
            raise ConflictError(
                "Publishing waits for review: " + "; ".join(parts),
                details={
                    "reason": "pending_review",
                    "documents": [b.model_dump() for b in blockers],
                    "geometry": [g.model_dump() for g in geometry],
                },
            )

        async def on_created(session: AsyncSession, job_id: int) -> None:
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="publish.request",
                entity_type="pipeline_job",
                entity_id=job_id,
                details={"label": label, "notes": notes},
            )

        async def on_dispatch_failed(session: AsyncSession, job_id: int, error: str) -> None:
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="job.enqueue_failed",
                entity_type="pipeline_job",
                entity_id=job_id,
                details={"type": "publish_approved", "error": error},
            )

        outcome = await enqueue_job(
            self.session_factory,
            self.dispatcher,
            municipality_id=self.municipality_id,
            job_type="publish_approved",
            payload={
                "label": label,
                "notes": notes,
                "requested_by": principal.subject,
                "requested_by_user_id": principal.user_id,
            },
            target_type="publish_run",
            target_id=None,
            max_attempts=self.max_attempts,
            requested_by=principal.subject,
            requested_by_user_id=principal.user_id,
            on_created=on_created,
            on_dispatch_failed=on_dispatch_failed,
        )
        return EnqueuedJob(job=await self._job(outcome.job_id), created=outcome.created)

    async def _job(self, job_id: int) -> JobOut:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {JOB_JSON} AS job FROM pipeline_jobs j "
                        "WHERE j.id = :id AND j.municipality_id = :m"
                    ),
                    {"id": job_id, "m": self.municipality_id},
                )
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"No job with id {job_id}", details={"job_id": job_id})
        return job_out(row)


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
