"""Admin configuration API (``/v1/admin/assumptions``, ``/formulas``, ``/users``).

Two sets of staff-maintained data, every write audited (``audit_log``) and role-gated (admin),
and the formula versions (read-only: a formula changes with an engine release, a migration adds
its row):

- **Financial assumptions** per zone (or the municipality-wide row, ``zone_id`` null): the
  four rates the feasibility engine needs, the range factors, optional absolute low / high
  bounds per rate, an optional default saleable share, sources, the date the set applies from
  and, once reviewed market inputs have set a rate, its provenance (``rate_sources``). Rows are
  immutable, effective-dated versions (``core.assumptions``): a create or an update inserts
  version n+1 for the zone (``supersedes_id`` = the previous version, ``is_current`` moves to the
  new head) applying from ``effective_from``: today by default, a later date schedules it. The
  version that applies today (``status = live``: the latest effective date on or before today,
  the newest version on a tie) is what ``GET /v1/panel``, the parcel panel and ``POST
  /v1/feasibility`` read, and each states its id and version. Nothing is deleted; retiring the
  live version leaves the zone without market figures until a later version takes effect. A
  zone without its own row gets no figures: the municipality-wide row (``zone_id`` null) only
  supplies the range factors that single-figure market imports are widened with
  (``core.market``), never a zone's figures. Approved market inputs write versions through
  :func:`insert_assumptions_version` too.
- **Staff users**: create, list, update role / name, deactivate (open sessions revoked) or
  reactivate. No passwords: the magic-link login (e-mail item) issues sessions.

Field validation lives in ``api.schemas.admin_config``; referential checks (zone) and uniqueness
(e-mail) here.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin_config import (
    AssumptionsBatchIn,
    AssumptionsBatchOut,
    AssumptionsIn,
    AssumptionsList,
    AssumptionsOut,
    AssumptionsUpdate,
    FormulaVersionOut,
    RateIn,
    StaffMeOut,
    StaffUserIn,
    StaffUserList,
    StaffUserOut,
    StaffUserUpdate,
)
from api.schemas.panel import RateRange
from api.services.audit import write_audit
from core.assumptions import LOCAL_TODAY, applies_from_sql, status_sql, top_versions_sql
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError
from core.municipality import MunicipalityProfile

RATES = ("land", "build", "design", "sale")
# How far ahead a set may be scheduled (a typo guard: 2062 instead of 2026).
MAX_SCHEDULE_DAYS = 5 * 366


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def _validation_error(problems: list[dict[str, Any]]) -> AppError:
    return AppError(
        "Request validation failed", code="validation_error", status_code=422, details=problems
    )


def rate_range(
    expected: float, low: float | None, high: float | None, low_factor: float, high_factor: float
) -> RateRange:
    """Absolute bounds when the row has them, else the factor-derived ones (2 decimals)."""
    if low is not None and high is not None:
        return RateRange(expected=expected, low=low, high=high, kind="absolute")
    return RateRange(
        expected=expected,
        low=round(expected * low_factor, 2),
        high=round(expected * high_factor, 2),
        kind="multiplier",
    )


# --- SQL ------------------------------------------------------------------------------------------

_ASSUMPTION_COLUMNS = ", ".join(
    f"a.{r}_rate_eur_m2, a.{r}_rate_low_eur_m2, a.{r}_rate_high_eur_m2" for r in RATES
)
_PLAIN_RATE_COLUMNS = ", ".join(
    f"{r}_rate_eur_m2, {r}_rate_low_eur_m2, {r}_rate_high_eur_m2" for r in RATES
)


def _assumptions_sql(extra: str) -> str:
    """Versions with their status today (``core.assumptions``); ``extra`` filters on the output
    columns (``id``, ``zone_id``, ``status`` ...). Parameters: ``m``, ``tz``, ``limit``,
    ``offset``."""
    return f"""
    WITH top AS ({top_versions_sql()}),
    versions AS (
        SELECT a.id, a.zone_id, z.name AS zone_name, a.version, a.is_current, a.supersedes_id,
               {_ASSUMPTION_COLUMNS},
               a.range_low_factor, a.range_high_factor, a.saleable_share, a.source,
               a.source_date, a.notes, a.effective_from, a.rate_sources,
               a.created_by, a.created_at, a.retired_at, a.retired_by,
               {applies_from_sql("a")} AS applies_from,
               LEAST(
                   LEAD({applies_from_sql("a")}) OVER (
                       PARTITION BY a.zone_id
                       ORDER BY {applies_from_sql("a")}, a.version, a.id
                   ),
                   (a.retired_at AT TIME ZONE CAST(:tz AS text))::date
               ) AS effective_to,
               {status_sql("a", "top")} AS status
        FROM financial_assumptions a
        LEFT JOIN zones z ON z.id = a.zone_id
        LEFT JOIN top ON top.zone_id IS NOT DISTINCT FROM a.zone_id
        WHERE a.municipality_id = :m
    )
    SELECT * FROM versions WHERE true {extra}
    ORDER BY zone_id ASC NULLS FIRST, version DESC, id DESC
    LIMIT :limit OFFSET :offset
    """


ASSUMPTIONS_BY_ID_SQL = text(_assumptions_sql("AND id = :id"))
FORMULAS_SQL = text(
    "SELECT id, label, effective_from, is_current, approval_note, engine_package "
    "FROM formula_versions ORDER BY effective_from DESC, id DESC"
)
TODAY_SQL = text(f"SELECT {LOCAL_TODAY} AS today")
# The zone's newest version, locked: the next version number, what it supersedes and the "before"
# of the audit row. Concurrent saves for a zone meet on the head's partial unique index.
LATEST_ASSUMPTIONS_SQL = text(
    f"""
    SELECT id, version, zone_id, {_PLAIN_RATE_COLUMNS},
           range_low_factor, range_high_factor, saleable_share, source, source_date, notes,
           effective_from, rate_sources, retired_at
    FROM financial_assumptions
    WHERE municipality_id = :m AND zone_id IS NOT DISTINCT FROM CAST(:zone_id AS bigint)
    ORDER BY version DESC, id DESC
    LIMIT 1
    FOR UPDATE
    """
)
SUPERSEDE_ASSUMPTIONS_SQL = text(
    """
    UPDATE financial_assumptions SET is_current = false
    WHERE municipality_id = :m AND zone_id IS NOT DISTINCT FROM CAST(:zone_id AS bigint)
      AND is_current
    """
)
RETIRE_ASSUMPTIONS_SQL = text(
    """
    UPDATE financial_assumptions
    SET is_current = false, retired_at = :at, retired_by = :by WHERE id = :id
    """
)
INSERT_ASSUMPTIONS_SQL = text(
    f"""
    INSERT INTO financial_assumptions (
        municipality_id, zone_id, version, supersedes_id, is_current,
        land_rate_eur_m2, land_rate_low_eur_m2, land_rate_high_eur_m2,
        build_rate_eur_m2, build_rate_low_eur_m2, build_rate_high_eur_m2,
        design_rate_eur_m2, design_rate_low_eur_m2, design_rate_high_eur_m2,
        sale_rate_eur_m2, sale_rate_low_eur_m2, sale_rate_high_eur_m2,
        range_low_factor, range_high_factor, saleable_share, source, source_date, notes,
        created_by, dataset_version, effective_from, rate_sources)
    VALUES (
        :m, :zone_id, :version, :supersedes_id, true,
        :land_expected, :land_low, :land_high,
        :build_expected, :build_low, :build_high,
        :design_expected, :design_low, :design_high,
        :sale_expected, :sale_low, :sale_high,
        :range_low_factor, :range_high_factor, :saleable_share, :source, :source_date, :notes,
        :created_by, NULL, COALESCE(CAST(:effective_from AS date), {LOCAL_TODAY}),
        CAST(:rate_sources AS jsonb))
    RETURNING id, effective_from
    """
)
ZONE_EXISTS_SQL = text("SELECT 1 FROM zones WHERE id = :id AND municipality_id = :m")


def _users_sql(extra: str) -> str:
    return f"""
    SELECT u.id, u.email, u.display_name, u.role, u.is_active, u.created_at, u.last_login_at,
           (SELECT count(*) FROM staff_sessions s
            WHERE s.user_id = u.id AND s.revoked_at IS NULL AND s.expires_at > now())
               AS open_sessions
    FROM staff_users u
    WHERE u.municipality_id = :m {extra}
    ORDER BY u.email ASC
    """


USERS_SQL = text(_users_sql(""))
USER_BY_ID_SQL = text(_users_sql("AND u.id = :id"))
INSERT_USER_SQL = text(
    """
    INSERT INTO staff_users (municipality_id, email, display_name, role, is_active)
    VALUES (:m, :email, :display_name, :role, true)
    RETURNING id
    """
)
REVOKE_USER_SESSIONS_SQL = text(
    "UPDATE staff_sessions SET revoked_at = now() WHERE user_id = :id AND revoked_at IS NULL"
)


# --- output builders ------------------------------------------------------------------------------


def _assumptions_out(row: Mapping[str, Any]) -> AssumptionsOut:
    low_factor, high_factor = float(row["range_low_factor"]), float(row["range_high_factor"])

    def rate(prefix: str) -> RateRange:
        return rate_range(
            float(row[f"{prefix}_rate_eur_m2"]),
            row[f"{prefix}_rate_low_eur_m2"],
            row[f"{prefix}_rate_high_eur_m2"],
            low_factor,
            high_factor,
        )

    return AssumptionsOut(
        id=row["id"],
        zone_id=row["zone_id"],
        zone_name=row["zone_name"],
        version=row["version"],
        status=row["status"],
        is_current=bool(row["is_current"]),
        supersedes_id=row["supersedes_id"],
        land_rate=rate("land"),
        build_rate=rate("build"),
        design_rate=rate("design"),
        sale_rate=rate("sale"),
        range_low_factor=low_factor,
        range_high_factor=high_factor,
        saleable_share=row["saleable_share"],
        source=row["source"],
        source_date=row["source_date"],
        notes=row["notes"],
        effective_from=row["effective_from"],
        applies_from=row["applies_from"],
        effective_to=row["effective_to"],
        rate_sources=row["rate_sources"],
        created_by=row["created_by"],
        created_at=_utc(row["created_at"]),
        retired_at=_utc(row["retired_at"]),
        retired_by=row["retired_by"],
    )


def _user_out(row: Mapping[str, Any]) -> StaffUserOut:
    return StaffUserOut(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        created_at=_utc(row["created_at"]),
        last_login_at=_utc(row["last_login_at"]),
        open_sessions=int(row["open_sessions"] or 0),
    )


def _rate_columns(payload: AssumptionsIn) -> dict[str, float | None]:
    """The four rates under their column names (the audit rows' shape)."""
    out: dict[str, float | None] = {}
    for prefix in RATES:
        rate: RateIn = getattr(payload, f"{prefix}_rate")
        out[f"{prefix}_rate_eur_m2"] = rate.expected
        out[f"{prefix}_rate_low_eur_m2"] = rate.low
        out[f"{prefix}_rate_high_eur_m2"] = rate.high
    return out


def _rate_params(prefix: str, rate: RateIn) -> dict[str, Any]:
    return {
        f"{prefix}_expected": rate.expected,
        f"{prefix}_low": rate.low,
        f"{prefix}_high": rate.high,
    }


def rate_from_row(row: Mapping[str, Any], prefix: str) -> RateIn:
    return RateIn(
        expected=float(row[f"{prefix}_rate_eur_m2"]),
        low=row[f"{prefix}_rate_low_eur_m2"],
        high=row[f"{prefix}_rate_high_eur_m2"],
    )


async def local_today(session: AsyncSession, timezone: str) -> date:
    """The municipality's local date, by the database's clock (what the panel's rule uses)."""
    return (await session.execute(TODAY_SQL, {"tz": timezone})).scalar_one()


async def insert_assumptions_version(
    session: AsyncSession,
    *,
    municipality_id: str,
    timezone: str,
    principal: Principal,
    payload: AssumptionsIn,
    action: str,
    changed: list[str] | None = None,
    effective_from: date | None = None,
    rate_sources: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> int:
    """Insert version n+1 of the zone's assumptions, applying from ``effective_from`` (default:
    the municipality's today), and audit it with the previous version as "before". The previous
    version keeps applying until the new one's date (``core.assumptions``). The caller commits."""
    latest = (
        (
            await session.execute(
                LATEST_ASSUMPTIONS_SQL, {"m": municipality_id, "zone_id": payload.zone_id}
            )
        )
        .mappings()
        .first()
    )
    version = int(latest["version"]) + 1 if latest is not None else 1
    await session.execute(
        SUPERSEDE_ASSUMPTIONS_SQL, {"m": municipality_id, "zone_id": payload.zone_id}
    )
    params: dict[str, Any] = {
        "m": municipality_id,
        "tz": timezone,
        "zone_id": payload.zone_id,
        "version": version,
        "supersedes_id": latest["id"] if latest is not None else None,
        "range_low_factor": payload.range_low_factor,
        "range_high_factor": payload.range_high_factor,
        "saleable_share": payload.saleable_share,
        "source": payload.source,
        "source_date": payload.source_date,
        "notes": payload.notes,
        "created_by": principal.subject,
        "effective_from": effective_from,
        "rate_sources": json.dumps(rate_sources, default=str) if rate_sources else None,
    }
    for prefix in RATES:
        params.update(_rate_params(prefix, getattr(payload, f"{prefix}_rate")))
    inserted = (await session.execute(INSERT_ASSUMPTIONS_SQL, params)).mappings().one()
    new_id = int(inserted["id"])
    applies_from: date = inserted["effective_from"]
    await write_audit(
        session,
        municipality_id=municipality_id,
        principal=principal,
        action=action,
        entity_type="financial_assumptions",
        entity_id=new_id,
        details={
            "zone_id": payload.zone_id,
            "version": version,
            "supersedes_id": latest["id"] if latest is not None else None,
            "effective_from": applies_from.isoformat(),
            "changed": changed,
            "source": payload.source,
            **(details or {}),
        },
        # before and after in the same flat shape (the table's columns), so the audit log lists
        # exactly the figures that changed, old and new
        before=dict(latest) if latest is not None else None,
        after={
            "id": new_id,
            "version": version,
            "zone_id": payload.zone_id,
            **_rate_columns(payload),
            "range_low_factor": payload.range_low_factor,
            "range_high_factor": payload.range_high_factor,
            "saleable_share": payload.saleable_share,
            "source": payload.source,
            "source_date": payload.source_date.isoformat() if payload.source_date else None,
            "notes": payload.notes,
            "effective_from": applies_from.isoformat(),
            "rate_sources": dict(rate_sources) if rate_sources else None,
            "retired_at": None,
        },
    )
    return new_id


def effective_problems(value: date | None, today: date, loc: list[Any]) -> list[dict[str, Any]]:
    """An admin set applies from today or a later date (never backdated: the panel would skip
    it), at most five years ahead."""
    if value is None:
        return []
    if value < today:
        return [
            {
                "loc": loc,
                "msg": f"a set applies from today ({today.isoformat()}) or a later date",
            }
        ]
    if value > today + timedelta(days=MAX_SCHEDULE_DAYS):
        return [{"loc": loc, "msg": "the date is more than five years ahead"}]
    return []


# --- service --------------------------------------------------------------------------------------


class AdminConfigService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        municipality: MunicipalityProfile,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_assumptions_changed: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.municipality = municipality
        self.clock = clock
        self.on_assumptions_changed = on_assumptions_changed

    async def _assumptions_changed(self) -> None:
        """After a committed market change: the sale-price heatmap follows the sets that apply
        today (``refresh_heatmaps``, queued only when they differ from the published cells)."""
        if self.on_assumptions_changed is not None:
            await self.on_assumptions_changed()

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
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            before=before,
            after=after,
            note=note,
        )

    async def _exists(self, session: AsyncSession, statement: Any, entity_id: int) -> bool:
        row = await session.execute(statement, {"m": self.municipality_id, "id": entity_id})
        return row.first() is not None

    # --- financial assumptions --------------------------------------------------------------------

    @property
    def timezone(self) -> str:
        return self.municipality.timezone

    async def list_assumptions(
        self,
        *,
        zone_id: int | None = None,
        default_only: bool = False,
        include_history: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> AssumptionsList:
        """The live and scheduled versions (what applies today and what is coming), or every
        version with ``include_history``; each with its status today."""
        params: dict[str, Any] = {
            "m": self.municipality_id,
            "tz": self.timezone,
            "limit": limit,
            "offset": offset,
        }
        clauses: list[str] = []
        if default_only:
            clauses.append("AND zone_id IS NULL")
        elif zone_id is not None:
            clauses.append("AND zone_id = :zone_id")
            params["zone_id"] = zone_id
        if not include_history:
            clauses.append("AND status IN ('live', 'scheduled')")
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_assumptions_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
            today = await local_today(session, self.timezone)
        return AssumptionsList(
            items=[_assumptions_out(r) for r in rows], today=today, timezone=self.timezone
        )

    async def list_formulas(self) -> list[FormulaVersionOut]:
        """The formula versions, newest first (product-wide, like the engine)."""
        async with self.session_factory() as session:
            rows = (await session.execute(FORMULAS_SQL)).mappings().all()
        return [FormulaVersionOut(**r) for r in rows]

    async def get_assumptions(self, assumptions_id: int) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
        return _assumptions_out(row)

    async def _assumptions_row(self, session: AsyncSession, assumptions_id: int) -> Mapping:
        row = (
            (
                await session.execute(
                    ASSUMPTIONS_BY_ID_SQL,
                    {
                        "m": self.municipality_id,
                        "tz": self.timezone,
                        "id": assumptions_id,
                        "limit": 1,
                        "offset": 0,
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(
                f"No financial assumptions with id {assumptions_id}",
                details={"assumptions_id": assumptions_id},
            )
        return row

    async def create_assumptions(
        self, principal: Principal, payload: AssumptionsIn
    ) -> AssumptionsOut:
        async with self.session_factory() as session:
            problems: list[dict[str, Any]] = []
            if payload.zone_id is not None and not await self._exists(
                session, ZONE_EXISTS_SQL, payload.zone_id
            ):
                problems.append({"loc": ["body", "zone_id"], "msg": "no such zone"})
            today = await local_today(session, self.timezone)
            problems += effective_problems(
                payload.effective_from, today, ["body", "effective_from"]
            )
            if problems:
                raise _validation_error(problems)
            new_id = await self._insert_assumptions_version(
                session,
                principal,
                payload,
                action="assumptions.create",
                effective_from=payload.effective_from or today,
            )
            await self._commit(session)
        await self._assumptions_changed()
        return await self.get_assumptions(new_id)

    async def create_assumptions_batch(
        self, principal: Principal, payload: AssumptionsBatchIn
    ) -> AssumptionsBatchOut:
        """Several zones' sets in one transaction (the console's "Save changes"): all or
        nothing, one version and one audit row per set."""
        async with self.session_factory() as session:
            today = await local_today(session, self.timezone)
            problems = effective_problems(payload.effective_from, today, ["body", "effective_from"])
            seen: set[int | None] = set()
            for index, item in enumerate(payload.sets):
                loc: list[Any] = ["body", "sets", index]
                if item.zone_id in seen:
                    problems.append({"loc": [*loc, "zone_id"], "msg": "the zone appears twice"})
                seen.add(item.zone_id)
                if item.zone_id is not None and not await self._exists(
                    session, ZONE_EXISTS_SQL, item.zone_id
                ):
                    problems.append({"loc": [*loc, "zone_id"], "msg": "no such zone"})
                problems += effective_problems(item.effective_from, today, [*loc, "effective_from"])
            if problems:
                raise _validation_error(problems)
            ids = [
                await self._insert_assumptions_version(
                    session,
                    principal,
                    item,
                    action="assumptions.create",
                    effective_from=item.effective_from or payload.effective_from or today,
                    details={"batch_size": len(payload.sets)},
                )
                for item in payload.sets
            ]
            await self._commit(session)
        await self._assumptions_changed()
        return AssumptionsBatchOut(items=[await self.get_assumptions(i) for i in ids])

    @staticmethod
    async def _commit(session: AsyncSession) -> None:
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ConflictError(
                "Another change to these assumptions was saved at the same time; reload and "
                "try again",
                details={"reason": "concurrent_change"},
            ) from None

    async def update_assumptions(
        self, principal: Principal, assumptions_id: int, payload: AssumptionsUpdate
    ) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
            if not row["is_current"]:
                raise ConflictError(
                    "Only the newest version can be updated; a new version is created from it",
                    details={"assumptions_id": assumptions_id, "reason": "not_current"},
                )
            changes = payload.model_dump(exclude_unset=True)
            today = await local_today(session, self.timezone)
            problems = effective_problems(
                changes.get("effective_from"), today, ["body", "effective_from"]
            )
            if problems:
                raise _validation_error(problems)
            merged = AssumptionsIn(
                zone_id=row["zone_id"],
                land_rate=payload.land_rate or rate_from_row(row, "land"),
                build_rate=payload.build_rate or rate_from_row(row, "build"),
                design_rate=payload.design_rate or rate_from_row(row, "design"),
                sale_rate=payload.sale_rate or rate_from_row(row, "sale"),
                range_low_factor=changes.get("range_low_factor", row["range_low_factor"]),
                range_high_factor=changes.get("range_high_factor", row["range_high_factor"]),
                saleable_share=changes.get("saleable_share", row["saleable_share"]),
                source=changes.get("source", row["source"]) or "",
                source_date=changes.get("source_date", row["source_date"]),
                notes=changes.get("notes", row["notes"]),
            )
            # provenance per rate: an edited rate is now the admin's, the others keep theirs
            rate_sources = dict(row["rate_sources"] or {}) or None
            if rate_sources is not None:
                for prefix in RATES:
                    if f"{prefix}_rate" in changes:
                        rate_sources[f"{prefix}_rate"] = {
                            "source": merged.source,
                            "source_date": merged.source_date,
                            "set_by": "admin",
                        }
            new_id = await self._insert_assumptions_version(
                session,
                principal,
                merged,
                action="assumptions.update",
                changed=sorted(changes),
                effective_from=changes.get("effective_from") or today,
                rate_sources=rate_sources,
            )
            await self._commit(session)
        await self._assumptions_changed()
        return await self.get_assumptions(new_id)

    async def _insert_assumptions_version(
        self,
        session: AsyncSession,
        principal: Principal,
        payload: AssumptionsIn,
        *,
        action: str,
        changed: list[str] | None = None,
        effective_from: date | None = None,
        rate_sources: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        return await insert_assumptions_version(
            session,
            municipality_id=self.municipality_id,
            timezone=self.timezone,
            principal=principal,
            payload=payload,
            action=action,
            changed=changed,
            effective_from=effective_from,
            rate_sources=rate_sources,
            details=details,
        )

    async def retire_assumptions(self, principal: Principal, assumptions_id: int) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
            if row["status"] != "live":
                raise ConflictError(
                    "Only the version that applies today can be retired; a scheduled set is "
                    "replaced by saving another set for its date",
                    details={
                        "assumptions_id": assumptions_id,
                        "reason": "not_live",
                        "status": row["status"],
                    },
                )
            await session.execute(
                RETIRE_ASSUMPTIONS_SQL,
                {"id": assumptions_id, "at": self.clock(), "by": principal.subject},
            )
            await self._audit(
                session,
                principal,
                "assumptions.retire",
                "financial_assumptions",
                assumptions_id,
                {"zone_id": row["zone_id"], "version": row["version"]},
                before={"status": "live", "is_current": bool(row["is_current"])},
                after={"status": "retired", "is_current": False, "retired_by": principal.subject},
            )
            await session.commit()
        await self._assumptions_changed()
        return await self.get_assumptions(assumptions_id)

    async def list_users(self) -> StaffUserList:
        async with self.session_factory() as session:
            rows = (await session.execute(USERS_SQL, {"m": self.municipality_id})).mappings().all()
        return StaffUserList(items=[_user_out(r) for r in rows])

    async def me(self, principal: Principal) -> StaffMeOut:
        """Who is calling: the staff user behind a session, else the service token."""
        if principal.user_id is None:
            return StaffMeOut(role=principal.role, subject=principal.subject, via="token")
        async with self.session_factory() as session:
            row = await self._user_row(session, principal.user_id)
        return StaffMeOut(
            id=int(row["id"]),
            email=row["email"],
            display_name=row["display_name"],
            role=row["role"],
            subject=principal.subject,
            via="session",
        )

    async def get_user(self, user_id: int) -> StaffUserOut:
        async with self.session_factory() as session:
            row = await self._user_row(session, user_id)
        return _user_out(row)

    async def _user_row(self, session: AsyncSession, user_id: int) -> Mapping:
        row = (
            (await session.execute(USER_BY_ID_SQL, {"m": self.municipality_id, "id": user_id}))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(f"No staff user with id {user_id}", details={"user_id": user_id})
        return row

    async def create_user(self, principal: Principal, payload: StaffUserIn) -> StaffUserOut:
        async with self.session_factory() as session:
            try:
                user_id = int(
                    (
                        await session.execute(
                            INSERT_USER_SQL,
                            {
                                "m": self.municipality_id,
                                "email": payload.email,
                                "display_name": payload.display_name,
                                "role": payload.role.value,
                            },
                        )
                    ).scalar_one()
                )
            except IntegrityError:
                await session.rollback()
                raise ConflictError(
                    "A staff user with this e-mail already exists",
                    details={"email": payload.email},
                ) from None
            await self._audit(
                session,
                principal,
                "user.create",
                "staff_user",
                user_id,
                {"email": payload.email, "role": payload.role.value},
                after={
                    "email": payload.email,
                    "role": payload.role.value,
                    "display_name": payload.display_name,
                    "is_active": True,
                },
            )
            await session.commit()
        return await self.get_user(user_id)

    async def update_user(
        self, principal: Principal, user_id: int, payload: StaffUserUpdate
    ) -> StaffUserOut:
        changes = payload.model_dump(exclude_unset=True)
        async with self.session_factory() as session:
            row = await self._user_row(session, user_id)
            if principal.user_id == user_id:
                if changes.get("is_active") is False:
                    raise ConflictError(
                        "You cannot deactivate your own account",
                        details={"user_id": user_id, "reason": "self"},
                    )
                if "role" in changes and changes["role"] != row["role"]:
                    raise ConflictError(
                        "You cannot change your own role",
                        details={"user_id": user_id, "reason": "self"},
                    )
            assignments: list[str] = []
            params: dict[str, Any] = {"id": user_id}
            if "role" in changes:
                assignments.append("role = :role")
                params["role"] = changes["role"].value
            if "display_name" in changes:
                assignments.append("display_name = :display_name")
                params["display_name"] = changes["display_name"]
            if "is_active" in changes:
                assignments.append("is_active = :is_active")
                params["is_active"] = bool(changes["is_active"])
            await session.execute(
                text(f"UPDATE staff_users SET {', '.join(assignments)} WHERE id = :id"), params
            )
            deactivated = changes.get("is_active") is False and bool(row["is_active"])
            revoked = 0
            if deactivated:
                result = await session.execute(REVOKE_USER_SESSIONS_SQL, {"id": user_id})
                revoked = int(result.rowcount or 0)
            action = (
                "user.deactivate"
                if deactivated
                else "user.reactivate"
                if changes.get("is_active") is True and not row["is_active"]
                else "user.update"
            )
            details: dict[str, Any] = {
                "changed": sorted(changes),
                "email": row["email"],
                "sessions_revoked": revoked,
            }
            if "role" in changes:
                details["role"] = changes["role"].value
            before = {
                "role": row["role"],
                "display_name": row["display_name"],
                "is_active": bool(row["is_active"]),
            }
            after = {
                **before,
                **{k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()},
            }
            await self._audit(
                session,
                principal,
                action,
                "staff_user",
                user_id,
                details,
                before=before,
                after=after,
            )
            await session.commit()
        return await self.get_user(user_id)
