"""Publish API: the one button (``POST /v1/admin/publish``), its status screen
(``GET /v1/admin/publish``), the manual rollback (``POST /v1/admin/publish/rollback``) and the
public tiles pointer (``GET /v1/tiles/current``).

The heavy lifting is the job (``jobs.publish_pipeline``); this service refuses a publish while
items are pending review (naming the documents), enqueues one ``publish_approved`` job at a time
(idempotent), reads ``publish_versions`` and flips ``is_current`` back on rollback. Rollback is
a pointer flip: the previous version's values, links, cells and archive are still there, nothing
is recomputed. The public API only ever reads the current version.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin import JobOut
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
from api.services.cell_classes import cell_classes
from api.services.jobs import JOB_JSON, job_out
from core.auth import Principal
from core.errors import ConflictError, NotFoundError
from jobs.enqueue import JobDispatcher, enqueue_job
from jobs.publish_pipeline import PENDING_SQL

_VERSION_COLUMNS = """
    p.id, p.label, p.is_current, p.published_at, p.published_by, p.formula_version, p.notes,
    p.previous_version_id, p.job_id, p.archive_key, p.archive_size_bytes, p.archive_sha256,
    p.archive_pruned_at, p.layers, p.counts, p.duration_ms, p.min_zoom, p.max_zoom,
    p.rolled_back_at, p.rolled_back_by"""
VERSIONS_SQL = text(
    f"""
    SELECT {_VERSION_COLUMNS} FROM publish_versions p
    WHERE p.municipality_id = :m ORDER BY p.id DESC LIMIT :limit
    """
)
CURRENT_SQL = text(
    f"SELECT {_VERSION_COLUMNS} FROM publish_versions p "
    "WHERE p.municipality_id = :m AND p.is_current"
)
CURRENT_FOR_UPDATE_SQL = text(
    f"SELECT {_VERSION_COLUMNS} FROM publish_versions p "
    "WHERE p.municipality_id = :m AND p.is_current FOR UPDATE"
)
VERSION_SQL = text(
    f"SELECT {_VERSION_COLUMNS} FROM publish_versions p WHERE p.municipality_id = :m AND p.id = :id"
)
PREVIOUS_SQL = text(
    f"""
    SELECT {_VERSION_COLUMNS} FROM publish_versions p
    WHERE p.municipality_id = :m AND p.id < :current_id AND NOT p.is_current
    ORDER BY p.id DESC LIMIT 1
    """
)
UNSET_CURRENT_SQL = text(
    "UPDATE publish_versions SET is_current = false WHERE municipality_id = :m AND is_current"
)
SET_CURRENT_SQL = text("UPDATE publish_versions SET is_current = true WHERE id = :id")
ROLLED_BACK_SQL = text(
    "UPDATE publish_versions SET rolled_back_at = now(), rolled_back_by = :by WHERE id = :id"
)
PUBLISH_JOBS_SQL = text(
    f"""
    SELECT {JOB_JSON} AS job FROM pipeline_jobs j
    WHERE j.municipality_id = :m AND j.type = 'publish_approved'
    ORDER BY j.requested_at DESC, j.id DESC LIMIT 5
    """
)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
        price_band_breaks: Sequence[float] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.storage = storage
        self.municipality_id = municipality_id
        self.keep_versions = int(keep_versions)
        self.tiles_url_expires_seconds = int(tiles_url_expires_seconds)
        self.max_attempts = int(max_attempts)
        self.price_band_breaks = tuple(float(b) for b in price_band_breaks)
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
            rolled_back_at=_utc(row["rolled_back_at"]),
            rolled_back_by=row["rolled_back_by"],
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

    async def status(self) -> PublishStatus:
        m = self.municipality_id
        async with self.session_factory() as session:
            versions = (
                (await session.execute(VERSIONS_SQL, {"m": m, "limit": self.keep_versions + 3}))
                .mappings()
                .all()
            )
            jobs = [
                job_out(r["job"])
                for r in (await session.execute(PUBLISH_JOBS_SQL, {"m": m})).mappings()
            ]
            blockers = await self.blockers(session)
        current = next((v for v in versions if v["is_current"]), None)
        active = next((j for j in jobs if j.status in ("queued", "running", "retrying")), None)
        return PublishStatus(
            current=self._version_out(current, signed=True) if current else None,
            versions=[self._version_out(v, signed=False) for v in versions],
            active_job=active,
            last_job=jobs[0] if jobs else None,
            can_publish=not blockers and active is None,
            blockers=blockers,
            keep_versions=self.keep_versions,
        )

    async def current_tiles(self) -> TilesCurrent:
        async with self.session_factory() as session:
            row = (
                (await session.execute(CURRENT_SQL, {"m": self.municipality_id})).mappings().first()
            )
            classes = (
                await cell_classes(session, row["id"], price_breaks=self.price_band_breaks)
                if row is not None
                else None
            )
        if row is None:
            return TilesCurrent(status="unpublished", data_version="unpublished")
        version = self._version_out(row, signed=True)
        expires_at = (
            self.clock() + timedelta(seconds=self.tiles_url_expires_seconds)
            if version.archive_url
            else None
        )
        return TilesCurrent(
            status="published",
            version_id=version.id,
            data_version=version.label,
            published_at=version.published_at,
            archive_url=version.archive_url,
            expires_at=expires_at,
            layers=version.layers,
            min_zoom=version.min_zoom,
            max_zoom=version.max_zoom,
            cell_classes=CellClasses.model_validate(classes),
        )

    # --- writes ----------------------------------------------------------------------------------

    async def enqueue(
        self, principal: Principal, *, label: str | None = None, notes: str | None = None
    ) -> EnqueuedJob:
        """Refuse while any document has items pending review; otherwise one publish job at a
        time (an identical active job is returned as is)."""
        async with self.session_factory() as session:
            blockers = await self.blockers(session)
        if blockers:
            names = ", ".join(f"{b.document_name} ({b.pending})" for b in blockers)
            raise ConflictError(
                f"Items are still pending review: {names}",
                details={
                    "reason": "pending_review",
                    "documents": [b.model_dump() for b in blockers],
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

    async def rollback(self, principal: Principal, version_id: int | None = None) -> PublishStatus:
        """Flip ``is_current`` to an earlier version (default: the one before the current)."""
        m = self.municipality_id
        async with self.session_factory() as session:
            current = (await session.execute(CURRENT_FOR_UPDATE_SQL, {"m": m})).mappings().first()
            if current is None:
                raise ConflictError(
                    "Nothing is published; there is no version to roll back from",
                    details={"reason": "unpublished"},
                )
            if version_id is None:
                target = (
                    (await session.execute(PREVIOUS_SQL, {"m": m, "current_id": current["id"]}))
                    .mappings()
                    .first()
                )
                if target is None:
                    raise ConflictError(
                        "There is no earlier version to roll back to",
                        details={
                            "reason": "no_previous_version",
                            "current_version_id": current["id"],
                        },
                    )
            else:
                target = (
                    (await session.execute(VERSION_SQL, {"m": m, "id": version_id}))
                    .mappings()
                    .first()
                )
                if target is None:
                    raise NotFoundError(
                        f"No publish version with id {version_id}",
                        details={"version_id": version_id},
                    )
                if target["is_current"]:
                    raise ConflictError(
                        f"Version {target['label']} is already current",
                        details={"reason": "already_current", "version_id": version_id},
                    )
            if target["archive_pruned_at"] is not None:
                raise ConflictError(
                    f"Version {target['label']} was pruned by retention; its archive is gone",
                    details={"reason": "pruned", "version_id": target["id"]},
                )
            await session.execute(UNSET_CURRENT_SQL, {"m": m})
            await session.execute(SET_CURRENT_SQL, {"id": target["id"]})
            await session.execute(ROLLED_BACK_SQL, {"id": current["id"], "by": principal.subject})
            await write_audit(
                session,
                municipality_id=m,
                principal=principal,
                action="publish.rollback",
                entity_type="publish_version",
                entity_id=int(target["id"]),
                details={"from_version_id": int(current["id"]), "from_label": current["label"]},
                before={
                    "current_version_id": int(current["id"]),
                    "current_label": current["label"],
                },
                after={"current_version_id": int(target["id"]), "current_label": target["label"]},
            )
            await session.commit()
        return await self.status()


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
