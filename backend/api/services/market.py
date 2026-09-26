"""Market-data imports, the market-input review queue and coverage (``core.market``).

- **Imports** (role admin): ``POST /v1/admin/market/imports`` registers an uploaded table
  (``stored_files`` kind ``market_data``: official statistics or the client's range sheet) and
  ``POST /v1/admin/market/listings`` pasted portal listings; both record the content as read
  (``market_imports``: source, retrieval date, checksum; the same content twice is one import)
  and queue the ``import_market_data`` job, which normalises it into ``market_data`` rows
  (``pending_review``). Every write is audited.
- **Review** (roles admin, reviewer, expert; ``/v1/admin/review/market-inputs``): the same
  decisions as the planning review. Approve accepts the imported figure, amend stores a
  correction alongside (the imported figure stays), reject keeps it out with a reason. An
  approved or amended item **writes a new ``financial_assumptions`` version for its zone**
  (``api.services.admin_config.insert_assumptions_version``): the metric's expected value and
  absolute low / high bounds, the effective date, and the rate's provenance in ``rate_sources``;
  the other rates are carried over. A zone without assumptions gets its first version only when
  all four metrics have an approved item (until then the items wait, ``waiting_for``). An applied
  item is closed. Approving needs a complete, consistent range: a single figure no configured
  factors could widen must be amended with low and high first.
- **Coverage** (``GET /v1/admin/market/coverage``): per zone, the current sale price per m²
  (low / expected / high, source, date, reviewed or not) and every metric's state, i.e. the
  table the client confirms; zones without market data are listed (their panel says "no market
  data for this zone" and their heatmap cells are drawn not covered).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api.schemas.admin_config import AssumptionsIn, RateIn
from api.schemas.market import (
    CoverageRate,
    CurrentRate,
    ItemCounts,
    JobRef,
    ListingsImportIn,
    MarketAmendIn,
    MarketApproveIn,
    MarketCoverage,
    MarketImportAccepted,
    MarketImportIn,
    MarketImportList,
    MarketImportOut,
    MarketItemOut,
    MarketItemPage,
    MarketRange,
    ZoneCoverage,
)
from api.services.admin_config import (
    RATES,
    insert_assumptions_version,
    rate_from_row,
    rate_range,
)
from api.services.audit import write_audit
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError, ServiceUnavailableError
from core.market.model import METRICS
from core.market.pipeline import MarketImporter, MarketImportError
from core.municipality import MunicipalityProfile, load_market_profile
from jobs.enqueue import JobDispatcher, enqueue_job

STATUS_TO_DB = {
    "pending": "pending_review",
    "approved": "approved",
    "amended": "amended",
    "rejected": "rejected",
}
STATUS_FROM_DB = {v: k for k, v in STATUS_TO_DB.items()}
_RATE_COLUMNS = ", ".join(
    f"f.{r}_rate_eur_m2, f.{r}_rate_low_eur_m2, f.{r}_rate_high_eur_m2" for r in RATES
)


def _validation_error(loc: list[str], msg: str) -> AppError:
    return AppError(
        "Request validation failed",
        code="validation_error",
        status_code=422,
        details=[{"loc": loc, "msg": msg}],
    )


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --- SQL ------------------------------------------------------------------------------------------

FILE_SQL = text(
    """
    SELECT id, kind, object_key, original_filename, sha256
    FROM stored_files WHERE id = :id AND municipality_id = :m
    """
)
_IMPORT_COLUMNS = """
    i.id, i.kind, i.source, i.retrieved_on, i.file_id, i.filename, i.sha256, i.row_count,
    i.status, i.normaliser, i.error, i.notes, i.job_id, i.created_by, i.created_at,
    i.normalised_at,
    count(d.id) FILTER (WHERE d.review_status = 'pending_review') AS pending,
    count(d.id) FILTER (WHERE d.review_status = 'approved') AS approved,
    count(d.id) FILTER (WHERE d.review_status = 'amended') AS amended,
    count(d.id) FILTER (WHERE d.review_status = 'rejected') AS rejected,
    count(d.id) FILTER (WHERE d.applied_assumption_id IS NOT NULL) AS applied"""
IMPORTS_SQL = text(
    f"""
    SELECT {_IMPORT_COLUMNS}, count(*) OVER () AS total
    FROM market_imports i
    LEFT JOIN market_data d ON d.import_id = i.id
    WHERE i.municipality_id = :m
      AND (CAST(:kind AS text) IS NULL OR i.kind = CAST(:kind AS text))
      AND (CAST(:status AS text) IS NULL OR i.status = CAST(:status AS text))
    GROUP BY i.id
    ORDER BY i.created_at DESC, i.id DESC
    LIMIT :limit OFFSET :offset
    """
)
IMPORT_SQL = text(
    f"""
    SELECT {_IMPORT_COLUMNS}, i.report, i.raw
    FROM market_imports i
    LEFT JOIN market_data d ON d.import_id = i.id
    WHERE i.municipality_id = :m AND i.id = :id
    GROUP BY i.id
    """
)
SET_IMPORT_JOB_SQL = text("UPDATE market_imports SET job_id = :job_id WHERE id = :id")


def _items_sql(where: str, *, paged: bool = True) -> str:
    tail = (
        "ORDER BY (d.review_status = 'pending_review') DESC, z.name ASC, d.metric ASC, d.id ASC "
        "LIMIT :limit OFFSET :offset"
        if paged
        else ""
    )
    return f"""
    SELECT d.id, d.import_id, i.kind AS import_kind, d.zone_id, z.name AS zone_name, d.metric,
           d.currency, d.unit, d.low, d.expected, d.high, d.amended_low, d.amended_expected,
           d.amended_high, d.range_basis, d.source, d.source_date, d.effective_from,
           d.confidence, d.notes, d.flags, d.raw, d.mapping, d.normaliser,
           d.review_status::text AS review_status, d.reviewer, d.reviewed_at, d.review_note,
           d.applied_assumption_id, d.created_at,
           f.id AS current_id, f.version AS current_version, f.range_low_factor,
           f.range_high_factor, {_RATE_COLUMNS},
           count(*) OVER () AS total
    FROM market_data d
    JOIN market_imports i ON i.id = d.import_id
    JOIN zones z ON z.id = d.zone_id
    LEFT JOIN financial_assumptions f
           ON f.municipality_id = d.municipality_id AND f.zone_id = d.zone_id AND f.is_current
    WHERE d.municipality_id = :m {where}
    {tail}
    """


_ITEM_FILTERS = """
      AND (CAST(:zone_id AS bigint) IS NULL OR d.zone_id = CAST(:zone_id AS bigint))
      AND (CAST(:metric AS text) IS NULL OR d.metric = CAST(:metric AS text))
      AND (CAST(:import_id AS bigint) IS NULL OR d.import_id = CAST(:import_id AS bigint))
      AND (CAST(:flag AS text) IS NULL OR d.flags @> jsonb_build_array(CAST(:flag AS text)))"""
ITEMS_SQL = text(
    _items_sql(
        _ITEM_FILTERS
        + "\n      AND (CAST(:status AS text) IS NULL"
        + " OR d.review_status::text = CAST(:status AS text))"
    )
)
ITEM_SQL = text(_items_sql("AND d.id = :id", paged=False))
COUNTS_SQL = text(
    f"""
    SELECT d.review_status::text AS status, count(*) AS n,
           count(*) FILTER (WHERE d.applied_assumption_id IS NOT NULL) AS applied
    FROM market_data d
    WHERE d.municipality_id = :m {_ITEM_FILTERS}
    GROUP BY d.review_status
    """
)
WAITING_SQL = text(
    """
    SELECT zone_id, array_agg(DISTINCT metric) AS metrics
    FROM market_data
    WHERE municipality_id = :m AND review_status IN ('approved', 'amended')
      AND applied_assumption_id IS NULL
    GROUP BY zone_id
    """
)
LOCK_ITEM_SQL = text(
    """
    SELECT d.id, d.zone_id, d.metric, d.import_id, d.low, d.expected, d.high, d.amended_low,
           d.amended_expected, d.amended_high, d.range_basis, d.source, d.source_date,
           d.review_status::text AS review_status, d.review_note, d.effective_from,
           d.applied_assumption_id
    FROM market_data d WHERE d.municipality_id = :m AND d.id = :id
    FOR UPDATE
    """
)
OTHER_APPROVED_SQL = text(
    """
    SELECT id FROM market_data
    WHERE municipality_id = :m AND zone_id = :zone_id AND metric = :metric AND id <> :id
      AND review_status IN ('approved', 'amended') AND applied_assumption_id IS NULL
    ORDER BY id LIMIT 1
    """
)
DECIDE_SQL = text(
    """
    UPDATE market_data
    SET review_status = CAST(:state AS review_state), reviewer = :reviewer,
        reviewed_by_user_id = :reviewer_user_id, reviewed_at = :at, review_note = :note,
        amended_low = :amended_low, amended_expected = :amended_expected,
        amended_high = :amended_high, effective_from = :effective_from
    WHERE id = :id AND municipality_id = :m AND applied_assumption_id IS NULL
    RETURNING id
    """
)
READY_SQL = text(
    """
    SELECT id, import_id, metric, low, expected, high, amended_low, amended_expected,
           amended_high, review_status::text AS review_status, source, source_date, range_basis,
           effective_from, reviewer
    FROM market_data
    WHERE municipality_id = :m AND zone_id = :zone_id
      AND review_status IN ('approved', 'amended') AND applied_assumption_id IS NULL
    ORDER BY reviewed_at ASC, id ASC
    FOR UPDATE
    """
)
ZONE_ROW_SQL = text(
    f"""
    SELECT f.id, f.version, f.range_low_factor, f.range_high_factor, f.source, f.source_date,
           f.rate_sources, f.effective_from, {_RATE_COLUMNS}
    FROM financial_assumptions f
    WHERE f.municipality_id = :m AND f.is_current AND f.zone_id = :zone_id
    """
)
MUNICIPALITY_ROW_SQL = text(
    """
    SELECT range_low_factor, range_high_factor FROM financial_assumptions
    WHERE municipality_id = :m AND is_current AND zone_id IS NULL
    """
)
MARK_APPLIED_SQL = text(
    "UPDATE market_data SET applied_assumption_id = :assumption_id WHERE id = ANY(:ids)"
)
COVERAGE_ZONES_SQL = text(
    "SELECT id, name, zone_type FROM zones WHERE municipality_id = :m ORDER BY name ASC, id ASC"
)
COVERAGE_ROWS_SQL = text(
    f"""
    SELECT f.id, f.zone_id, f.version, f.range_low_factor, f.range_high_factor, f.source,
           f.source_date, f.rate_sources, f.effective_from, {_RATE_COLUMNS}
    FROM financial_assumptions f
    WHERE f.municipality_id = :m AND f.is_current AND f.zone_id IS NOT NULL
    """
)
OPEN_ITEMS_SQL = text(
    """
    SELECT id, zone_id, metric, review_status::text AS review_status
    FROM market_data
    WHERE municipality_id = :m AND applied_assumption_id IS NULL
      AND review_status IN ('pending_review', 'approved', 'amended')
    ORDER BY id
    """
)


# --- row → payload --------------------------------------------------------------------------------


def _effective(row: Mapping[str, Any]) -> tuple[float | None, float, float | None]:
    if row["review_status"] == "amended" and row["amended_expected"] is not None:
        return row["amended_low"], float(row["amended_expected"]), row["amended_high"]
    return row["low"], float(row["expected"]), row["high"]


def _import_out(row: Mapping[str, Any], *, detail: bool = False) -> MarketImportOut:
    return MarketImportOut(
        id=row["id"],
        kind=row["kind"],
        source=row["source"],
        retrieved_on=row["retrieved_on"],
        file_id=row["file_id"],
        filename=row["filename"],
        sha256=row["sha256"],
        row_count=row["row_count"],
        status=row["status"],
        normaliser=row["normaliser"],
        error=row["error"],
        notes=row["notes"],
        job_id=row["job_id"],
        created_by=row["created_by"],
        created_at=row["created_at"].astimezone(UTC),
        normalised_at=row["normalised_at"].astimezone(UTC) if row["normalised_at"] else None,
        items=ItemCounts(
            pending=row["pending"],
            approved=row["approved"],
            amended=row["amended"],
            rejected=row["rejected"],
            applied=row["applied"],
        ),
        report=row["report"] if detail else None,
        raw=row["raw"] if detail else None,
    )


def _item_out(row: Mapping[str, Any], waiting: Mapping[int, set[str]]) -> MarketItemOut:
    status = STATUS_FROM_DB[row["review_status"]]
    amended = None
    if row["amended_expected"] is not None:
        amended = MarketRange(
            low=row["amended_low"], expected=row["amended_expected"], high=row["amended_high"]
        )
    low, expected, high = _effective(row)
    current = None
    prefix = row["metric"].removesuffix("_rate")
    if row["current_id"] is not None:
        rate = rate_range(
            float(row[f"{prefix}_rate_eur_m2"]),
            row[f"{prefix}_rate_low_eur_m2"],
            row[f"{prefix}_rate_high_eur_m2"],
            float(row["range_low_factor"]),
            float(row["range_high_factor"]),
        )
        current = CurrentRate(
            assumptions_id=row["current_id"],
            version=row["current_version"],
            low=rate.low,
            expected=rate.expected,
            high=rate.high,
        )
    waiting_for = None
    if (
        status in ("approved", "amended")
        and row["applied_assumption_id"] is None
        and row["current_id"] is None
    ):
        waiting_for = [m for m in METRICS if m not in waiting.get(row["zone_id"], set())]
    return MarketItemOut(
        id=row["id"],
        import_id=row["import_id"],
        import_kind=row["import_kind"],
        zone_id=row["zone_id"],
        zone_name=row["zone_name"],
        metric=row["metric"],
        currency=row["currency"],
        unit=row["unit"],
        imported=MarketRange(low=row["low"], expected=row["expected"], high=row["high"]),
        amended=amended,
        effective=MarketRange(low=low, expected=expected, high=high),
        range_basis=row["range_basis"],
        source=row["source"],
        source_date=row["source_date"],
        effective_from=row["effective_from"],
        confidence=row["confidence"],
        notes=row["notes"],
        flags=list(row["flags"] or []),
        raw=row["raw"] or {},
        mapping=row["mapping"],
        normaliser=row["normaliser"],
        status=status,  # type: ignore[arg-type]
        reviewer=row["reviewer"],
        reviewed_at=row["reviewed_at"].astimezone(UTC) if row["reviewed_at"] else None,
        review_note=row["review_note"],
        applied_assumption_id=row["applied_assumption_id"],
        waiting_for=waiting_for,  # type: ignore[arg-type]
        current=current,
        created_at=row["created_at"].astimezone(UTC),
    )


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    low, expected, high = _effective(row)
    return {
        "status": STATUS_FROM_DB.get(row["review_status"], row["review_status"]),
        "zone_id": row["zone_id"],
        "metric": row["metric"],
        "low": low,
        "expected": expected,
        "high": high,
        "imported": {"low": row["low"], "expected": row["expected"], "high": row["high"]},
        "effective_from": _as_date(row["effective_from"]).isoformat()
        if row["effective_from"]
        else None,
        "note": row["review_note"],
        "applied_assumption_id": row["applied_assumption_id"],
    }


def _source_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": row["source"],
        "source_date": _as_date(row["source_date"]).isoformat() if row["source_date"] else None,
        "market_data_id": row["id"],
        "import_id": row["import_id"],
        "range_basis": row["range_basis"],
        "effective_from": _as_date(row["effective_from"]).isoformat()
        if row["effective_from"]
        else None,
        "reviewed_by": row["reviewer"],
    }


# --- service --------------------------------------------------------------------------------------


class MarketService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: Any,
        dispatcher: JobDispatcher,
        municipality: MunicipalityProfile,
        settings: Any,
        max_attempts: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.dispatcher = dispatcher
        self.municipality = municipality
        self.settings = settings
        self.max_attempts = max_attempts
        self.clock = clock
        self.importer = MarketImporter(
            session_factory,
            municipality_id=municipality.id,
            municipality_name=municipality.name,
            settings=settings,
            clock=clock,
        )

    @property
    def municipality_id(self) -> str:
        return self.municipality.id

    async def _audit(
        self,
        session: AsyncSession,
        principal: Principal,
        action: str,
        entity_type: str,
        entity_id: int | None,
        details: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            **kwargs,
        )

    # --- imports ----------------------------------------------------------------------------------

    async def create_import(
        self, principal: Principal, payload: MarketImportIn
    ) -> tuple[MarketImportAccepted, bool]:
        """Record an uploaded table and queue its normalisation. Returns the reply and whether
        a job was queued (202) or the content was already normalised (200)."""
        async with self.session_factory() as session:
            file_row = (
                (
                    await session.execute(
                        FILE_SQL, {"m": self.municipality_id, "id": payload.file_id}
                    )
                )
                .mappings()
                .first()
            )
        if file_row is None:
            raise _validation_error(["body", "file_id"], "no such file")
        if file_row["kind"] != "market_data":
            raise _validation_error(
                ["body", "file_id"],
                f"the file is a {file_row['kind']}; upload the table with kind market_data",
            )
        try:
            data = await run_in_threadpool(self.storage.get_bytes, file_row["object_key"])
        except (BotoCoreError, ClientError) as exc:
            raise ServiceUnavailableError(
                "The stored file could not be read", details={"file_id": payload.file_id}
            ) from exc
        source = payload.source or load_market_profile(self.municipality_id).statistics_source
        async with self.session_factory() as session:
            try:
                created = await self.importer.record_table(
                    session,
                    kind=payload.kind,
                    source=source,
                    retrieved_on=payload.retrieved_on,
                    filename=file_row["original_filename"],
                    data=data,
                    file_id=payload.file_id,
                    notes=payload.notes,
                    created_by=principal.subject,
                    created_by_user_id=principal.user_id,
                )
            except MarketImportError as exc:
                raise _validation_error(["body", "file_id"], str(exc)) from exc
            if created.created:
                await self._audit(
                    session,
                    principal,
                    "market.import",
                    "market_import",
                    created.id,
                    {
                        "kind": payload.kind,
                        "source": source,
                        "file_id": payload.file_id,
                        "sha256": created.sha256,
                        "rows": created.row_count,
                    },
                )
            await session.commit()
        return await self._queue(principal, created.id, created.created, created.sha256)

    async def create_listings(
        self, principal: Principal, payload: ListingsImportIn
    ) -> tuple[MarketImportAccepted, bool]:
        async with self.session_factory() as session:
            try:
                created = await self.importer.record_listings(
                    session,
                    source=payload.source,
                    retrieved_on=payload.retrieved_on,
                    metric=payload.metric,
                    pasted=payload.listings,
                    notes=payload.notes,
                    created_by=principal.subject,
                    created_by_user_id=principal.user_id,
                )
            except MarketImportError as exc:
                raise _validation_error(["body", "listings"], str(exc)) from exc
            if created.created:
                await self._audit(
                    session,
                    principal,
                    "market.import",
                    "market_import",
                    created.id,
                    {
                        "kind": "listings",
                        "source": payload.source,
                        "metric": payload.metric,
                        "sha256": created.sha256,
                        "lines": created.row_count,
                    },
                )
            await session.commit()
        return await self._queue(principal, created.id, created.created, created.sha256)

    async def _queue(
        self, principal: Principal, import_id: int, created: bool, sha: str
    ) -> tuple[MarketImportAccepted, bool]:
        current = await self.get_import(import_id)
        if current.status == "normalised":
            return MarketImportAccepted(market_import=current, created=created, job=None), False

        async def on_created(session: AsyncSession, job_id: int) -> None:
            await session.execute(SET_IMPORT_JOB_SQL, {"id": import_id, "job_id": job_id})
            await self._audit(
                session,
                principal,
                "job.enqueue",
                "pipeline_job",
                job_id,
                {
                    "type": "import_market_data",
                    "target_type": "market_import",
                    "target_id": import_id,
                },
            )

        async def on_dispatch_failed(session: AsyncSession, job_id: int, error: str) -> None:
            await self._audit(
                session,
                principal,
                "job.enqueue_failed",
                "pipeline_job",
                job_id,
                {"type": "import_market_data", "error": error},
            )

        outcome = await enqueue_job(
            self.session_factory,
            self.dispatcher,
            municipality_id=self.municipality_id,
            job_type="import_market_data",
            payload={"import_id": import_id},
            target_type="market_import",
            target_id=import_id,
            checksum=sha,
            max_attempts=self.max_attempts,
            requested_by=principal.subject,
            requested_by_user_id=principal.user_id,
            on_created=on_created,
            on_dispatch_failed=on_dispatch_failed,
        )
        job = JobRef(id=outcome.job_id, status_url=f"/v1/admin/jobs/{outcome.job_id}")
        return (
            MarketImportAccepted(
                market_import=await self.get_import(import_id), created=created, job=job
            ),
            True,
        )

    async def list_imports(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MarketImportList:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        IMPORTS_SQL,
                        {
                            "m": self.municipality_id,
                            "kind": kind,
                            "status": status,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return MarketImportList(
            items=[_import_out(r) for r in rows], total=int(rows[0]["total"]) if rows else 0
        )

    async def get_import(self, import_id: int, *, detail: bool = False) -> MarketImportOut:
        async with self.session_factory() as session:
            row = (
                (await session.execute(IMPORT_SQL, {"m": self.municipality_id, "id": import_id}))
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError(
                f"No market import with id {import_id}", details={"import_id": import_id}
            )
        return _import_out(row, detail=detail)

    # --- review queue -----------------------------------------------------------------------------

    async def _waiting(self, session: AsyncSession) -> dict[int, set[str]]:
        rows = (await session.execute(WAITING_SQL, {"m": self.municipality_id})).mappings()
        return {int(r["zone_id"]): set(r["metrics"]) for r in rows}

    async def list_items(
        self,
        *,
        status: str | None = None,
        zone_id: int | None = None,
        metric: str | None = None,
        import_id: int | None = None,
        flag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> MarketItemPage:
        filters = {
            "m": self.municipality_id,
            "zone_id": zone_id,
            "metric": metric,
            "import_id": import_id,
            "flag": flag,
        }
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        ITEMS_SQL,
                        {
                            **filters,
                            "status": STATUS_TO_DB[status] if status else None,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )
            counts_rows = (await session.execute(COUNTS_SQL, filters)).mappings().all()
            waiting = await self._waiting(session)
        counts = ItemCounts()
        for r in counts_rows:
            setattr(counts, STATUS_FROM_DB[r["status"]], int(r["n"]))
            counts.applied += int(r["applied"])
        return MarketItemPage(
            items=[_item_out(r, waiting) for r in rows],
            total=int(rows[0]["total"]) if rows else 0,
            counts=counts,
        )

    async def get_item(self, item_id: int) -> MarketItemOut:
        async with self.session_factory() as session:
            row = (
                (await session.execute(ITEM_SQL, {"m": self.municipality_id, "id": item_id}))
                .mappings()
                .first()
            )
            waiting = await self._waiting(session)
        if row is None:
            raise NotFoundError(f"No market input with id {item_id}", details={"item_id": item_id})
        return _item_out(row, waiting)

    async def approve(
        self, principal: Principal, item_id: int, payload: MarketApproveIn
    ) -> MarketItemOut:
        return await self._decide(
            principal, item_id, "approved", note=payload.note, effective_from=payload.effective_from
        )

    async def amend(
        self, principal: Principal, item_id: int, payload: MarketAmendIn
    ) -> MarketItemOut:
        return await self._decide(
            principal,
            item_id,
            "amended",
            note=payload.note,
            effective_from=payload.effective_from,
            amended=(payload.low, payload.expected, payload.high),
        )

    async def reject(self, principal: Principal, item_id: int, note: str) -> MarketItemOut:
        return await self._decide(principal, item_id, "rejected", note=note)

    async def _factors(self, session: AsyncSession, zone_id: int) -> tuple[float, float] | None:
        for statement, params in (
            (ZONE_ROW_SQL, {"m": self.municipality_id, "zone_id": zone_id}),
            (MUNICIPALITY_ROW_SQL, {"m": self.municipality_id}),
        ):
            row = (await session.execute(statement, params)).mappings().first()
            if row is not None:
                return float(row["range_low_factor"]), float(row["range_high_factor"])
        return None

    async def _decide(
        self,
        principal: Principal,
        item_id: int,
        state: str,
        *,
        note: str | None,
        effective_from: date | None = None,
        amended: tuple[float | None, float, float | None] | None = None,
    ) -> MarketItemOut:
        today = self.clock().date()
        async with self.session_factory() as session:
            row = (
                (await session.execute(LOCK_ITEM_SQL, {"m": self.municipality_id, "id": item_id}))
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError(
                    f"No market input with id {item_id}", details={"item_id": item_id}
                )
            if row["applied_assumption_id"] is not None:
                raise ConflictError(
                    "The item has written an assumptions version; its decision is closed",
                    details={
                        "item_id": item_id,
                        "reason": "applied",
                        "assumptions_id": row["applied_assumption_id"],
                    },
                )
            if amended is not None and amended[0] is None:
                factors = await self._factors(session, row["zone_id"])
                if factors is None:
                    raise _validation_error(
                        ["body", "low"],
                        "give low and high: no range factors are configured for this zone",
                    )
                expected = amended[1]
                amended = (
                    round(expected * factors[0], 2),
                    expected,
                    round(expected * factors[1], 2),
                )
            after = dict(row)
            after.update(
                review_status=state,
                review_note=note,
                amended_low=amended[0] if amended else None,
                amended_expected=amended[1] if amended else None,
                amended_high=amended[2] if amended else None,
                effective_from=(effective_from or today) if state != "rejected" else None,
            )
            if state in ("approved", "amended"):
                self._check_range(item_id, after)
                other = (
                    await session.execute(
                        OTHER_APPROVED_SQL,
                        {
                            "m": self.municipality_id,
                            "zone_id": row["zone_id"],
                            "metric": row["metric"],
                            "id": item_id,
                        },
                    )
                ).scalar_one_or_none()
                if other is not None:
                    raise ConflictError(
                        "Another approved input for this zone and metric is waiting to be "
                        "applied; reject or amend that one first",
                        details={
                            "item_id": item_id,
                            "reason": "already_approved",
                            "other_item_id": other,
                        },
                    )
            await session.execute(
                DECIDE_SQL,
                {
                    "id": item_id,
                    "m": self.municipality_id,
                    "state": state,
                    "reviewer": principal.subject,
                    "reviewer_user_id": principal.user_id,
                    "at": self.clock(),
                    "note": note,
                    "amended_low": after["amended_low"],
                    "amended_expected": after["amended_expected"],
                    "amended_high": after["amended_high"],
                    "effective_from": after["effective_from"],
                },
            )
            verb = {"approved": "approve", "amended": "amend", "rejected": "reject"}[state]
            applied_id = None
            if state in ("approved", "amended"):
                applied_id = await self._apply_zone(session, principal, row["zone_id"], item_id)
            after["applied_assumption_id"] = applied_id
            await self._audit(
                session,
                principal,
                f"market.{verb}",
                "market_data",
                item_id,
                {
                    "zone_id": row["zone_id"],
                    "metric": row["metric"],
                    "import_id": row["import_id"],
                    "source": row["source"],
                    "assumptions_id": applied_id,
                },
                before=_snapshot(row),
                after=_snapshot(after),
                note=note,
            )
            await session.commit()
        return await self.get_item(item_id)

    @staticmethod
    def _check_range(item_id: int, row: Mapping[str, Any]) -> None:
        low, expected, high = _effective(row)
        if low is None and high is None:
            raise ConflictError(
                "The figure has no range (no configured factors could widen it): amend it with "
                "low and high",
                details={"item_id": item_id, "reason": "range_required"},
            )
        if low is None or high is None:
            raise ConflictError(
                "The range has one bound only: amend it with low and high",
                details={"item_id": item_id, "reason": "range_incomplete"},
            )
        if not 0 < low <= expected <= high:
            raise ConflictError(
                "The range is inconsistent (low ≤ expected ≤ high, all above 0): amend it",
                details={"item_id": item_id, "reason": "bounds_inconsistent"},
            )

    async def _apply_zone(
        self, session: AsyncSession, principal: Principal, zone_id: int, trigger_id: int
    ) -> int | None:
        """Write the zone's next assumptions version from its approved, unapplied items (the
        latest per metric), carrying the other rates over; a zone without assumptions waits
        until all four metrics are approved. Returns the new version's id, or None."""
        ready = (
            (await session.execute(READY_SQL, {"m": self.municipality_id, "zone_id": zone_id}))
            .mappings()
            .all()
        )
        by_metric = {r["metric"]: r for r in ready}  # ordered by review time: the latest wins
        current = (
            (await session.execute(ZONE_ROW_SQL, {"m": self.municipality_id, "zone_id": zone_id}))
            .mappings()
            .first()
        )
        if current is None and set(by_metric) != set(METRICS):
            return None
        rates: dict[str, RateIn] = {}
        for prefix in RATES:
            item = by_metric.get(f"{prefix}_rate")
            if item is None:
                assert current is not None
                rates[prefix] = rate_from_row(current, prefix)
            else:
                low, expected, high = _effective(item)
                rates[prefix] = RateIn(expected=expected, low=low, high=high)
        sources: dict[str, Any] = {}
        if current is not None:
            sources = dict(current["rate_sources"] or {})
            for prefix in RATES:
                key = f"{prefix}_rate"
                if key not in sources and key not in by_metric:
                    sources[key] = {
                        "source": current["source"],
                        "source_date": _as_date(current["source_date"]).isoformat()
                        if current["source_date"]
                        else None,
                        "set_by": "admin",
                    }
        for key, item in by_metric.items():
            sources[key] = _source_entry(item)
        names = [sources[m]["source"] for m in METRICS if sources.get(m, {}).get("source")]
        dates = [
            d for m in METRICS if (d := _as_date(sources.get(m, {}).get("source_date"))) is not None
        ]
        factors = (
            (float(current["range_low_factor"]), float(current["range_high_factor"]))
            if current is not None
            else await self._factors(session, zone_id) or (0.86, 1.15)
        )
        ids = [int(item["id"]) for item in by_metric.values()]
        trigger = next(item for item in ready if item["id"] == trigger_id)
        payload = AssumptionsIn(
            zone_id=zone_id,
            land_rate=rates["land"],
            build_rate=rates["build"],
            design_rate=rates["design"],
            sale_rate=rates["sale"],
            range_low_factor=factors[0],
            range_high_factor=factors[1],
            source=("; ".join(dict.fromkeys(names)) or "market inputs")[:200],
            source_date=min(*dates, self.clock().date()) if dates else None,
            notes=(
                f"Reviewed market inputs {', '.join(map(str, sorted(ids)))} "
                f"({', '.join(sorted(by_metric))})"
            ),
        )
        new_id = await insert_assumptions_version(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            payload=payload,
            action="assumptions.market_input",
            changed=sorted(by_metric),
            effective_from=_as_date(trigger["effective_from"]),
            rate_sources=sources,
            details={"market_data_ids": sorted(ids)},
        )
        await session.execute(MARK_APPLIED_SQL, {"assumption_id": new_id, "ids": ids})
        return new_id

    # --- coverage ---------------------------------------------------------------------------------

    async def coverage(self) -> MarketCoverage:
        m = self.municipality_id
        async with self.session_factory() as session:
            zones = (await session.execute(COVERAGE_ZONES_SQL, {"m": m})).mappings().all()
            rows = {
                int(r["zone_id"]): r
                for r in (await session.execute(COVERAGE_ROWS_SQL, {"m": m})).mappings()
            }
            open_items = (await session.execute(OPEN_ITEMS_SQL, {"m": m})).mappings().all()
        pending: dict[tuple[int, str], list[int]] = {}
        approved: dict[tuple[int, str], list[int]] = {}
        for item in open_items:
            target = pending if item["review_status"] == "pending_review" else approved
            target.setdefault((int(item["zone_id"]), item["metric"]), []).append(int(item["id"]))
        out = []
        for zone in zones:
            zone_id = int(zone["id"])
            row = rows.get(zone_id)
            sources = dict(row["rate_sources"] or {}) if row is not None else {}
            rates = []
            for metric in METRICS:
                key = (zone_id, metric)
                entry = CoverageRate(
                    metric=metric,  # type: ignore[arg-type]
                    status="missing",
                    pending_item_ids=pending.get(key, []),
                    approved_item_ids=approved.get(key, []),
                )
                if row is not None:
                    prefix = metric.removesuffix("_rate")
                    rate = rate_range(
                        float(row[f"{prefix}_rate_eur_m2"]),
                        row[f"{prefix}_rate_low_eur_m2"],
                        row[f"{prefix}_rate_high_eur_m2"],
                        float(row["range_low_factor"]),
                        float(row["range_high_factor"]),
                    )
                    provenance = sources.get(metric) or {}
                    entry.status = "current"
                    entry.low, entry.expected, entry.high = rate.low, rate.expected, rate.high
                    entry.source = provenance.get("source") or row["source"]
                    entry.source_date = (
                        _as_date(provenance.get("source_date")) or row["source_date"]
                    )
                    entry.market_data_id = provenance.get("market_data_id")
                elif key in approved:
                    entry.status = "approved_waiting"
                elif key in pending:
                    entry.status = "pending"
                rates.append(entry)
            sale = next(r for r in rates if r.metric == "sale_rate")
            out.append(
                ZoneCoverage(
                    zone_id=zone_id,
                    zone_name=zone["name"],
                    zone_type=zone["zone_type"],
                    market_data=row is not None,
                    assumptions_id=row["id"] if row is not None else None,
                    assumptions_version=row["version"] if row is not None else None,
                    effective_from=row["effective_from"] if row is not None else None,
                    sale_price_eur_m2=MarketRange(
                        low=sale.low, expected=sale.expected, high=sale.high
                    )
                    if sale.status == "current" and sale.expected is not None
                    else None,
                    sale_price_source=sale.source if sale.status == "current" else None,
                    sale_price_source_date=sale.source_date if sale.status == "current" else None,
                    sale_price_reviewed=sale.status == "current"
                    and sale.market_data_id is not None,
                    rates=rates,
                )
            )
        return MarketCoverage(
            zones=out,
            zones_total=len(out),
            zones_with_market_data=sum(1 for z in out if z.market_data),
            zones_with_reviewed_sale_price=sum(1 for z in out if z.sale_price_reviewed),
            zones_without_market_data=[z.zone_name for z in out if not z.market_data],
            pending_items=sum(len(v) for v in pending.values()),
        )
