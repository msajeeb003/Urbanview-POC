"""Admin configuration API (``/v1/admin/assumptions``, ``/zone-parameters``, ``/users``).

Three sets of staff-maintained data, every write audited (``audit_log``) and role-gated (admin):

- **Financial assumptions** per zone (or the municipality-wide row, ``zone_id`` null): the
  four rates the feasibility engine needs, the range factors, optional absolute low / high
  bounds per rate, sources, an optional effective date and, once reviewed market inputs have set
  a rate, its provenance (``rate_sources``). Rows are immutable versions: a create or an update
  inserts version n+1 for the zone and flips the previous current row off (``supersedes_id``
  links them); a retire flips the current row off without a successor, so the panel reports no
  market data for the zone. The current row is what ``GET /v1/panel`` and ``POST
  /v1/feasibility`` read, and both state its id and version. A zone without its own row gets no
  figures: the municipality-wide row (``zone_id`` null) only supplies the range factors that
  single-figure market imports are widened with (``core.market``), never a zone's figures.
  Approved market inputs write versions through :func:`insert_assumptions_version` too.
- **Zone parameter sets**: the typical planning values of a zone (land use, FAR, coverage,
  height, floors) with a source document reference and a verification date; versioned the same
  way; the current row is the zone panel's ``typical_parameters``.
- **Staff users**: create, list, update role / name, deactivate (open sessions revoked) or
  reactivate. No passwords: the magic-link login (e-mail item) issues sessions.

Field validation lives in ``api.schemas.admin_config``; referential checks (zone, document) and
uniqueness (e-mail) here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.admin_config import (
    AssumptionsIn,
    AssumptionsList,
    AssumptionsOut,
    AssumptionsUpdate,
    RateIn,
    StaffUserIn,
    StaffUserList,
    StaffUserOut,
    StaffUserUpdate,
    ZoneParametersIn,
    ZoneParametersList,
    ZoneParameterSource,
    ZoneParametersOut,
    ZoneParametersUpdate,
)
from api.schemas.panel import RateRange
from api.services.audit import write_audit
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError
from core.municipality import MunicipalityProfile

RATES = ("land", "build", "design", "sale")


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


def _assumptions_sql(extra: str) -> str:
    return f"""
    SELECT a.id, a.zone_id, z.name AS zone_name, a.version, a.is_current, a.supersedes_id,
           {_ASSUMPTION_COLUMNS},
           a.range_low_factor, a.range_high_factor, a.source, a.source_date, a.notes,
           a.effective_from, a.rate_sources,
           a.created_by, a.created_at, a.retired_at, a.retired_by
    FROM financial_assumptions a
    LEFT JOIN zones z ON z.id = a.zone_id
    WHERE a.municipality_id = :m {extra}
    ORDER BY a.zone_id ASC NULLS FIRST, a.version DESC, a.id DESC
    LIMIT :limit OFFSET :offset
    """


ASSUMPTIONS_BY_ID_SQL = text(_assumptions_sql("AND a.id = :id"))
CURRENT_ASSUMPTIONS_SQL = text(
    """
    SELECT id, version, land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2,
           sale_rate_eur_m2, range_low_factor, range_high_factor, source, effective_from,
           rate_sources
    FROM financial_assumptions
    WHERE municipality_id = :m AND is_current AND zone_id IS NOT DISTINCT FROM :zone_id
    FOR UPDATE
    """
)
SUPERSEDE_ASSUMPTIONS_SQL = text(
    "UPDATE financial_assumptions SET is_current = false WHERE id = :id"
)
RETIRE_ASSUMPTIONS_SQL = text(
    """
    UPDATE financial_assumptions
    SET is_current = false, retired_at = :at, retired_by = :by WHERE id = :id
    """
)
INSERT_ASSUMPTIONS_SQL = text(
    """
    INSERT INTO financial_assumptions (
        municipality_id, zone_id, version, supersedes_id, is_current,
        land_rate_eur_m2, land_rate_low_eur_m2, land_rate_high_eur_m2,
        build_rate_eur_m2, build_rate_low_eur_m2, build_rate_high_eur_m2,
        design_rate_eur_m2, design_rate_low_eur_m2, design_rate_high_eur_m2,
        sale_rate_eur_m2, sale_rate_low_eur_m2, sale_rate_high_eur_m2,
        range_low_factor, range_high_factor, source, source_date, notes, created_by,
        dataset_version, effective_from, rate_sources)
    VALUES (
        :m, :zone_id, :version, :supersedes_id, true,
        :land_expected, :land_low, :land_high,
        :build_expected, :build_low, :build_high,
        :design_expected, :design_low, :design_high,
        :sale_expected, :sale_low, :sale_high,
        :range_low_factor, :range_high_factor, :source, :source_date, :notes, :created_by,
        NULL, :effective_from, CAST(:rate_sources AS jsonb))
    RETURNING id
    """
)


def _zone_parameters_sql(extra: str) -> str:
    return f"""
    SELECT p.id, p.zone_id, z.name AS zone_name, p.version, p.is_current, p.supersedes_id,
           p.land_use, p.max_far, p.max_site_coverage_pct, p.max_height_m, p.max_floors, p.notes,
           p.source_document_id, d.name AS source_document_name, d.source_url AS registry_url,
           p.source_page, p.source_note, p.verified_on, p.verified_by,
           p.created_by, p.created_at, p.retired_at, p.retired_by
    FROM zone_parameter_sets p
    LEFT JOIN zones z ON z.id = p.zone_id
    LEFT JOIN planning_documents d ON d.id = p.source_document_id
    WHERE p.municipality_id = :m {extra}
    ORDER BY p.zone_id ASC, p.version DESC, p.id DESC
    LIMIT :limit OFFSET :offset
    """


ZONE_PARAMETERS_BY_ID_SQL = text(_zone_parameters_sql("AND p.id = :id"))
CURRENT_ZONE_PARAMETERS_SQL = text(
    """
    SELECT id, version, land_use, max_far, max_site_coverage_pct, max_height_m, max_floors,
           notes, source_document_id, source_page, source_note, verified_on, verified_by
    FROM zone_parameter_sets
    WHERE municipality_id = :m AND is_current AND zone_id = :zone_id
    """
)
SUPERSEDE_ZONE_PARAMETERS_SQL = text(
    "UPDATE zone_parameter_sets SET is_current = false WHERE id = :id"
)
RETIRE_ZONE_PARAMETERS_SQL = text(
    """
    UPDATE zone_parameter_sets
    SET is_current = false, retired_at = :at, retired_by = :by WHERE id = :id
    """
)
INSERT_ZONE_PARAMETERS_SQL = text(
    """
    INSERT INTO zone_parameter_sets (
        municipality_id, zone_id, version, supersedes_id, is_current, land_use, max_far,
        max_site_coverage_pct, max_height_m, max_floors, notes, source_document_id, source_page,
        source_note, verified_on, verified_by, created_by, dataset_version)
    VALUES (
        :m, :zone_id, :version, :supersedes_id, true, :land_use, :max_far,
        :max_site_coverage_pct, :max_height_m, :max_floors, :notes, :source_document_id,
        :source_page, :source_note, :verified_on, :verified_by, :created_by, NULL)
    RETURNING id
    """
)

ZONE_EXISTS_SQL = text("SELECT 1 FROM zones WHERE id = :id AND municipality_id = :m")
DOCUMENT_EXISTS_SQL = text(
    "SELECT 1 FROM planning_documents WHERE id = :id AND municipality_id = :m"
)


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
        is_current=bool(row["is_current"]),
        supersedes_id=row["supersedes_id"],
        land_rate=rate("land"),
        build_rate=rate("build"),
        design_rate=rate("design"),
        sale_rate=rate("sale"),
        range_low_factor=low_factor,
        range_high_factor=high_factor,
        source=row["source"],
        source_date=row["source_date"],
        notes=row["notes"],
        effective_from=row["effective_from"],
        rate_sources=row["rate_sources"],
        created_by=row["created_by"],
        created_at=_utc(row["created_at"]),
        retired_at=_utc(row["retired_at"]),
        retired_by=row["retired_by"],
    )


def _zone_parameters_out(row: Mapping[str, Any]) -> ZoneParametersOut:
    source = None
    if row["source_document_id"] is not None:
        source = ZoneParameterSource(
            document_id=row["source_document_id"],
            document_name=row["source_document_name"],
            page=row["source_page"],
            note=row["source_note"],
            registry_url=row["registry_url"],
        )
    return ZoneParametersOut(
        id=row["id"],
        zone_id=row["zone_id"],
        zone_name=row["zone_name"],
        version=row["version"],
        is_current=bool(row["is_current"]),
        supersedes_id=row["supersedes_id"],
        land_use=row["land_use"],
        max_far=row["max_far"],
        max_site_coverage_pct=row["max_site_coverage_pct"],
        max_height_m=row["max_height_m"],
        max_floors=row["max_floors"],
        notes=row["notes"],
        source=source,
        verified_on=row["verified_on"],
        verified_by=row["verified_by"],
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


async def insert_assumptions_version(
    session: AsyncSession,
    *,
    municipality_id: str,
    principal: Principal,
    payload: AssumptionsIn,
    action: str,
    changed: list[str] | None = None,
    effective_from: date | None = None,
    rate_sources: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> int:
    """Insert version n+1 of the zone's assumptions (the current row, locked, is superseded)
    and audit it with the state before and after. The caller commits."""
    current = (
        (
            await session.execute(
                CURRENT_ASSUMPTIONS_SQL, {"m": municipality_id, "zone_id": payload.zone_id}
            )
        )
        .mappings()
        .first()
    )
    version = int(current["version"]) + 1 if current is not None else 1
    if current is not None:
        await session.execute(SUPERSEDE_ASSUMPTIONS_SQL, {"id": current["id"]})
    params: dict[str, Any] = {
        "m": municipality_id,
        "zone_id": payload.zone_id,
        "version": version,
        "supersedes_id": current["id"] if current is not None else None,
        "range_low_factor": payload.range_low_factor,
        "range_high_factor": payload.range_high_factor,
        "source": payload.source,
        "source_date": payload.source_date,
        "notes": payload.notes,
        "created_by": principal.subject,
        "effective_from": effective_from,
        "rate_sources": json.dumps(rate_sources, default=str) if rate_sources else None,
    }
    for prefix in RATES:
        params.update(_rate_params(prefix, getattr(payload, f"{prefix}_rate")))
    new_id = int((await session.execute(INSERT_ASSUMPTIONS_SQL, params)).scalar_one())
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
            "supersedes_id": current["id"] if current is not None else None,
            "changed": changed,
            "source": payload.source,
            **(details or {}),
        },
        before=dict(current) if current is not None else None,
        after={
            "id": new_id,
            "version": version,
            **payload.model_dump(mode="json"),
            "effective_from": effective_from.isoformat() if effective_from else None,
            "rate_sources": dict(rate_sources) if rate_sources else None,
        },
    )
    return new_id


# --- service --------------------------------------------------------------------------------------


class AdminConfigService:
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

    async def list_assumptions(
        self,
        *,
        zone_id: int | None = None,
        default_only: bool = False,
        include_history: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> AssumptionsList:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        if default_only:
            clauses.append("AND a.zone_id IS NULL")
        elif zone_id is not None:
            clauses.append("AND a.zone_id = :zone_id")
            params["zone_id"] = zone_id
        if not include_history:
            clauses.append("AND a.is_current")
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_assumptions_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
        return AssumptionsList(items=[_assumptions_out(r) for r in rows])

    async def get_assumptions(self, assumptions_id: int) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
        return _assumptions_out(row)

    async def _assumptions_row(self, session: AsyncSession, assumptions_id: int) -> Mapping:
        row = (
            (
                await session.execute(
                    ASSUMPTIONS_BY_ID_SQL,
                    {"m": self.municipality_id, "id": assumptions_id, "limit": 1, "offset": 0},
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
            if payload.zone_id is not None and not await self._exists(
                session, ZONE_EXISTS_SQL, payload.zone_id
            ):
                raise _validation_error([{"loc": ["body", "zone_id"], "msg": "no such zone"}])
            new_id = await self._insert_assumptions_version(
                session,
                principal,
                payload,
                action="assumptions.create",
                effective_from=payload.effective_from,
            )
            await session.commit()
        return await self.get_assumptions(new_id)

    async def update_assumptions(
        self, principal: Principal, assumptions_id: int, payload: AssumptionsUpdate
    ) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
            if not row["is_current"]:
                raise ConflictError(
                    "Only the current version can be updated; a new version is created from it",
                    details={"assumptions_id": assumptions_id, "reason": "not_current"},
                )
            changes = payload.model_dump(exclude_unset=True)
            merged = AssumptionsIn(
                zone_id=row["zone_id"],
                land_rate=payload.land_rate or rate_from_row(row, "land"),
                build_rate=payload.build_rate or rate_from_row(row, "build"),
                design_rate=payload.design_rate or rate_from_row(row, "design"),
                sale_rate=payload.sale_rate or rate_from_row(row, "sale"),
                range_low_factor=changes.get("range_low_factor", row["range_low_factor"]),
                range_high_factor=changes.get("range_high_factor", row["range_high_factor"]),
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
                effective_from=changes.get("effective_from", row["effective_from"]),
                rate_sources=rate_sources,
            )
            await session.commit()
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
    ) -> int:
        return await insert_assumptions_version(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            payload=payload,
            action=action,
            changed=changed,
            effective_from=effective_from,
            rate_sources=rate_sources,
        )

    async def retire_assumptions(self, principal: Principal, assumptions_id: int) -> AssumptionsOut:
        async with self.session_factory() as session:
            row = await self._assumptions_row(session, assumptions_id)
            if not row["is_current"]:
                raise ConflictError(
                    "Only the current version can be retired",
                    details={"assumptions_id": assumptions_id, "reason": "not_current"},
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
                before={"is_current": True},
                after={"is_current": False, "retired_by": principal.subject},
            )
            await session.commit()
        return await self.get_assumptions(assumptions_id)

    # --- zone parameter sets ----------------------------------------------------------------------

    async def list_zone_parameters(
        self,
        *,
        zone_id: int | None = None,
        include_history: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> ZoneParametersList:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        if zone_id is not None:
            clauses.append("AND p.zone_id = :zone_id")
            params["zone_id"] = zone_id
        if not include_history:
            clauses.append("AND p.is_current")
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_zone_parameters_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
        return ZoneParametersList(items=[_zone_parameters_out(r) for r in rows])

    async def get_zone_parameters(self, parameters_id: int) -> ZoneParametersOut:
        async with self.session_factory() as session:
            row = await self._zone_parameters_row(session, parameters_id)
        return _zone_parameters_out(row)

    async def _zone_parameters_row(self, session: AsyncSession, parameters_id: int) -> Mapping:
        row = (
            (
                await session.execute(
                    ZONE_PARAMETERS_BY_ID_SQL,
                    {"m": self.municipality_id, "id": parameters_id, "limit": 1, "offset": 0},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(
                f"No zone parameter set with id {parameters_id}",
                details={"parameters_id": parameters_id},
            )
        return row

    async def create_zone_parameters(
        self, principal: Principal, payload: ZoneParametersIn
    ) -> ZoneParametersOut:
        async with self.session_factory() as session:
            problems: list[dict[str, Any]] = []
            if not await self._exists(session, ZONE_EXISTS_SQL, payload.zone_id):
                problems.append({"loc": ["body", "zone_id"], "msg": "no such zone"})
            if payload.source_document_id is not None and not await self._exists(
                session, DOCUMENT_EXISTS_SQL, payload.source_document_id
            ):
                problems.append(
                    {"loc": ["body", "source_document_id"], "msg": "no such planning document"}
                )
            if problems:
                raise _validation_error(problems)
            new_id = await self._insert_zone_parameters_version(
                session, principal, payload, action="zone_parameters.create"
            )
            await session.commit()
        return await self.get_zone_parameters(new_id)

    async def update_zone_parameters(
        self, principal: Principal, parameters_id: int, payload: ZoneParametersUpdate
    ) -> ZoneParametersOut:
        async with self.session_factory() as session:
            row = await self._zone_parameters_row(session, parameters_id)
            if not row["is_current"]:
                raise ConflictError(
                    "Only the current version can be updated; a new version is created from it",
                    details={"parameters_id": parameters_id, "reason": "not_current"},
                )
            changes = payload.model_dump(exclude_unset=True)
            if "source_document_id" in changes and changes["source_document_id"] is not None:
                if not await self._exists(
                    session, DOCUMENT_EXISTS_SQL, changes["source_document_id"]
                ):
                    raise _validation_error(
                        [
                            {
                                "loc": ["body", "source_document_id"],
                                "msg": "no such planning document",
                            }
                        ]
                    )
            base = {
                name: row[name]
                for name in (
                    "land_use",
                    "max_far",
                    "max_site_coverage_pct",
                    "max_height_m",
                    "max_floors",
                    "notes",
                    "source_document_id",
                    "source_page",
                    "source_note",
                    "verified_on",
                    "verified_by",
                )
            }
            base.update(changes)
            merged = ZoneParametersIn(zone_id=row["zone_id"], **base)
            new_id = await self._insert_zone_parameters_version(
                session,
                principal,
                merged,
                action="zone_parameters.update",
                changed=sorted(changes),
            )
            await session.commit()
        return await self.get_zone_parameters(new_id)

    async def _insert_zone_parameters_version(
        self,
        session: AsyncSession,
        principal: Principal,
        payload: ZoneParametersIn,
        *,
        action: str,
        changed: list[str] | None = None,
    ) -> int:
        current = (
            (
                await session.execute(
                    CURRENT_ZONE_PARAMETERS_SQL,
                    {"m": self.municipality_id, "zone_id": payload.zone_id},
                )
            )
            .mappings()
            .first()
        )
        version = int(current["version"]) + 1 if current is not None else 1
        if current is not None:
            await session.execute(SUPERSEDE_ZONE_PARAMETERS_SQL, {"id": current["id"]})
        params = {
            "m": self.municipality_id,
            "zone_id": payload.zone_id,
            "version": version,
            "supersedes_id": current["id"] if current is not None else None,
            "land_use": payload.land_use,
            "max_far": payload.max_far,
            "max_site_coverage_pct": payload.max_site_coverage_pct,
            "max_height_m": payload.max_height_m,
            "max_floors": payload.max_floors,
            "notes": payload.notes,
            "source_document_id": payload.source_document_id,
            "source_page": payload.source_page,
            "source_note": payload.source_note,
            "verified_on": payload.verified_on,
            "verified_by": payload.verified_by,
            "created_by": principal.subject,
        }
        new_id = int((await session.execute(INSERT_ZONE_PARAMETERS_SQL, params)).scalar_one())
        await self._audit(
            session,
            principal,
            action,
            "zone_parameter_set",
            new_id,
            {
                "zone_id": payload.zone_id,
                "version": version,
                "supersedes_id": current["id"] if current is not None else None,
                "changed": changed,
                "source_document_id": payload.source_document_id,
                "verified_on": payload.verified_on,
            },
            before=dict(current) if current is not None else None,
            after={"id": new_id, "version": version, **payload.model_dump(mode="json")},
        )
        return new_id

    async def retire_zone_parameters(
        self, principal: Principal, parameters_id: int
    ) -> ZoneParametersOut:
        async with self.session_factory() as session:
            row = await self._zone_parameters_row(session, parameters_id)
            if not row["is_current"]:
                raise ConflictError(
                    "Only the current version can be retired",
                    details={"parameters_id": parameters_id, "reason": "not_current"},
                )
            await session.execute(
                RETIRE_ZONE_PARAMETERS_SQL,
                {"id": parameters_id, "at": self.clock(), "by": principal.subject},
            )
            await self._audit(
                session,
                principal,
                "zone_parameters.retire",
                "zone_parameter_set",
                parameters_id,
                {"zone_id": row["zone_id"], "version": row["version"]},
                before={"is_current": True},
                after={"is_current": False, "retired_by": principal.subject},
            )
            await session.commit()
        return await self.get_zone_parameters(parameters_id)

    # --- staff users ------------------------------------------------------------------------------

    async def list_users(self) -> StaffUserList:
        async with self.session_factory() as session:
            rows = (await session.execute(USERS_SQL, {"m": self.municipality_id})).mappings().all()
        return StaffUserList(items=[_user_out(r) for r in rows])

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
