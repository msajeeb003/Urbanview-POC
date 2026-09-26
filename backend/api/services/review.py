"""Expert review of AI-extracted planning information (``/v1/admin/review``) and the audit trail
(``/v1/admin/audit``).

100% of extracted values are reviewed before publication; textual accuracy matters as much as
numerical. The queue reads STAGING (``planning_parameter_extractions``) only. A decision never
touches the AI value: *approve* accepts it, *amend* stores the reviewer's corrected value
alongside it (``amended_value_*``; ``effective`` is what would publish), *reject* keeps it out.
Approved and amended items are eligible for the publish job (a separate item) and are not served
until published; an item that has been published is closed (409). Every decision writes one
append-only ``audit_log`` row with the state before and after; so does every other admin change.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.review import (
    AmendIn,
    AuditEntry,
    AuditPage,
    BulkApproveIn,
    BulkResult,
    BulkSkipped,
    PageLinkOut,
    ReviewCounters,
    ReviewItem,
    ReviewPage,
    ReviewPrevious,
    ReviewSource,
    ReviewTarget,
    ReviewValue,
)
from api.services import panel_text
from api.services.audit import write_audit
from api.services.source import signed_page_link
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError
from core.municipality import MunicipalityProfile

STATUS_TO_DB: dict[str, str] = {
    "pending": "pending_review",
    "approved": "approved",
    "amended": "amended",
    "rejected": "rejected",
}
DB_TO_STATUS = {db: api for api, db in STATUS_TO_DB.items()}
MARKET_KEYS = ("land_rate_eur_m2", "build_rate_eur_m2", "design_rate_eur_m2", "sale_rate_eur_m2")


def _validation_error(problems: list[dict[str, Any]]) -> AppError:
    return AppError(
        "Request validation failed", code="validation_error", status_code=422, details=problems
    )


def can_publish(pending: int, approved: int, amended: int) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if pending:
        blockers.append(f"{pending} item(s) pending review")
    if approved + amended == 0:
        blockers.append("nothing approved or amended")
    return not blockers, blockers


# --- SQL ------------------------------------------------------------------------------------------

_ITEM_COLUMNS = """
    SELECT e.id, e.review_state::text AS review_state, e.entity_type, e.parameter_key,
           e.field_key, f.label_en, f.label_me, f.value_type, f.unit AS field_unit, e.unit,
           e.value_text, e.value_number, e.amended_value_text, e.amended_value_number,
           e.amended_unit, e.urban_parcel_id, u.urban_parcel_number,
           COALESCE(e.block_id, u.block_id) AS block_id, b.block_ref,
           COALESCE(e.zone_id, b.zone_id, d.zone_id) AS zone_id, z.name AS zone_name,
           e.document_id, d.name AS document_name, d.source_url AS registry_url, d.file_key,
           d.page_count, d.page_images_rendered, e.source_page, e.source_bbox, e.source_note,
           e.raw_text, e.confidence, e.extracted_by, e.extracted_at, e.reviewer, e.reviewed_at,
           e.review_note, e.published_value_id, e.flags, e.extraction_method, e.schema_version,
           e.prompt_version, e.run_id, e.target_label, e.target_key, e.previous_item_id,
           e.change, e.superseded_at, e.superseded_by_run_id,
           p.review_state::text AS previous_state, p.value_text AS previous_value_text,
           p.value_number AS previous_value_number, p.unit AS previous_unit,
           p.amended_value_text AS previous_amended_text,
           p.amended_value_number AS previous_amended_number,
           p.amended_unit AS previous_amended_unit, p.run_id AS previous_run_id,
           count(*) OVER () AS total
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    LEFT JOIN planning_parameter_extractions p ON p.id = e.previous_item_id
    LEFT JOIN planning_fields f ON f.key = e.field_key
    LEFT JOIN urban_parcels u ON u.id = e.urban_parcel_id
    LEFT JOIN urban_blocks b ON b.id = COALESCE(e.block_id, u.block_id)
    LEFT JOIN zones z ON z.id = COALESCE(e.zone_id, b.zone_id, d.zone_id)
    WHERE e.municipality_id = :m
"""
_ITEM_ORDER = """
    ORDER BY (e.review_state = 'pending_review') DESC, e.document_id ASC,
             e.source_page ASC NULLS LAST, e.id ASC
    LIMIT :limit OFFSET :offset
"""


def _queue_sql(extra: str) -> str:
    return _ITEM_COLUMNS + extra + _ITEM_ORDER


ITEM_SQL = text(_queue_sql("AND e.id = :id"))
ITEM_STATE_SQL = text(
    """
    SELECT id, review_state::text AS review_state, published_value_id, superseded_at
    FROM planning_parameter_extractions
    WHERE municipality_id = :m AND id = ANY(:ids)
    """
)
DECIDE_SQL = text(
    """
    UPDATE planning_parameter_extractions
    SET review_state = CAST(:state AS review_state),
        reviewer = :reviewer, reviewed_by_user_id = :reviewer_user_id, reviewed_at = :at,
        review_note = :note,
        amended_value_text = :amended_text, amended_value_number = :amended_number,
        amended_unit = :amended_unit
    WHERE id = :id AND municipality_id = :m AND published_value_id IS NULL
      AND superseded_at IS NULL
    RETURNING id
    """
)
# Approving a newer reading of a target retires the older approved item it replaces (the
# previous run's), so the older value can never publish after the newer one.
RETIRE_PREVIOUS_SQL = text(
    """
    UPDATE planning_parameter_extractions
    SET superseded_by_run_id = :run_id, superseded_at = :at
    WHERE id = :id AND municipality_id = :m AND published_value_id IS NULL
      AND superseded_at IS NULL AND review_state IN ('approved', 'amended')
    RETURNING id
    """
)
COUNTERS_SQL = """
    SELECT d.id AS document_id, d.name AS document_name,
           count(*) FILTER (WHERE e.review_state = 'pending_review') AS pending,
           count(*) FILTER (WHERE e.review_state = 'approved') AS approved,
           count(*) FILTER (WHERE e.review_state = 'amended') AS amended,
           count(*) FILTER (WHERE e.review_state = 'rejected') AS rejected,
           count(*) AS total
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    WHERE e.municipality_id = :m AND e.superseded_at IS NULL {extra}
    GROUP BY d.id, d.name
    ORDER BY pending DESC, d.name ASC, d.id ASC
"""
AUDIT_SQL = """
    SELECT id, actor, actor_user_id, action, entity_type, entity_id, before, after, note, details,
           request_id, created_at, count(*) OVER () AS total
    FROM audit_log
    WHERE municipality_id = :m {extra}
    ORDER BY created_at DESC, id DESC
    LIMIT :limit OFFSET :offset
"""


# --- output builders ------------------------------------------------------------------------------


def _value(text_value: str | None, number: float | None, unit: str | None) -> ReviewValue:
    return ReviewValue(text=text_value, number=number, unit=unit)


def _labels(row: Mapping[str, Any]) -> tuple[str, str, str]:
    if row["field_key"] is not None:
        return row["label_en"], row["label_me"], row["value_type"]
    label = panel_text.MARKET_PARAMETER_LABELS.get(row["parameter_key"])
    if label is None:
        return row["parameter_key"], row["parameter_key"], "number"
    return label.en, label.me, "number"


def _item_out(row: Mapping[str, Any], link: PageLinkOut | None) -> ReviewItem:
    label_en, label_me, value_type = _labels(row)
    unit = row["unit"] or row["field_unit"]
    extracted = _value(row["value_text"], row["value_number"], unit)
    amended = None
    status = DB_TO_STATUS[row["review_state"]]
    if status == "amended":
        amended = _value(
            row["amended_value_text"], row["amended_value_number"], row["amended_unit"] or unit
        )
    previous = None
    if row.get("previous_item_id") is not None and row.get("previous_state") is not None:
        previous_amended = row["previous_state"] == "amended"
        previous = ReviewPrevious(
            id=row["previous_item_id"],
            status=DB_TO_STATUS[row["previous_state"]],  # type: ignore[arg-type]
            value=_value(
                row["previous_amended_text"] if previous_amended else row["previous_value_text"],
                row["previous_amended_number"]
                if previous_amended
                else row["previous_value_number"],
                (row["previous_amended_unit"] if previous_amended else None)
                or row["previous_unit"],
            ),
            run_id=row["previous_run_id"],
        )
    matched = not (
        (row["entity_type"] == "urban_parcel" and row["urban_parcel_id"] is None)
        or (row["entity_type"] == "block" and row["block_id"] is None)
    )
    return ReviewItem(
        id=row["id"],
        status=status,  # type: ignore[arg-type]
        parameter_key=row["parameter_key"],
        label_en=label_en,
        label_me=label_me,
        value_type=value_type,  # type: ignore[arg-type]
        extracted=extracted,
        amended=amended,
        effective=amended or extracted,
        target=ReviewTarget(
            entity_type=row["entity_type"],
            urban_parcel_id=row["urban_parcel_id"],
            urban_parcel_number=row["urban_parcel_number"],
            block_id=row["block_id"],
            block_ref=row["block_ref"],
            zone_id=row["zone_id"],
            zone_name=row["zone_name"],
            label=row.get("target_label"),
            matched=matched,
        ),
        source=ReviewSource(
            document_id=row["document_id"],
            document_name=row["document_name"],
            registry_url=row["registry_url"],
            page=row["source_page"],
            bbox=row["source_bbox"],
            note=row["source_note"],
            raw_text=row["raw_text"],
            confidence=row["confidence"],
            extraction_method=row.get("extraction_method"),
            link=link,
        ),
        flags=list(row.get("flags") or []),
        schema_version=row.get("schema_version"),
        prompt_version=row.get("prompt_version"),
        extracted_by=row["extracted_by"],
        extracted_at=row["extracted_at"].astimezone(UTC),
        reviewed_by=row["reviewer"],
        reviewed_at=row["reviewed_at"].astimezone(UTC) if row["reviewed_at"] else None,
        review_note=row["review_note"],
        published=row["published_value_id"] is not None,
        run_id=row.get("run_id"),
        change=row.get("change"),
        previous=previous,
        superseded=row.get("superseded_at") is not None,
        superseded_at=row["superseded_at"].astimezone(UTC) if row.get("superseded_at") else None,
        superseded_by_run_id=row.get("superseded_by_run_id"),
    )


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """The reviewer-relevant state of an item, for audit before / after."""
    status = DB_TO_STATUS[row["review_state"]]
    unit = row["unit"] or row["field_unit"]
    if status == "amended":
        value = {
            "text": row["amended_value_text"],
            "number": row["amended_value_number"],
            "unit": row["amended_unit"] or unit,
        }
    else:
        value = {"text": row["value_text"], "number": row["value_number"], "unit": unit}
    return {"status": status, "value": value, "note": row["review_note"]}


# --- service --------------------------------------------------------------------------------------


class ReviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: Any,
        municipality: MunicipalityProfile,
        link_expires_in_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.municipality = municipality
        self.link_expires_in_seconds = int(link_expires_in_seconds)
        self.clock = clock

    @property
    def municipality_id(self) -> str:
        return self.municipality.id

    # --- queue -----------------------------------------------------------------------------------

    async def list_items(
        self,
        *,
        document_id: int | None = None,
        zone_id: int | None = None,
        status: str | None = None,
        entity_type: str | None = None,
        urban_parcel_id: int | None = None,
        source_page: int | None = None,
        flag: str | None = None,
        run_id: int | None = None,
        change: str | None = None,
        include_superseded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> ReviewPage:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = [] if include_superseded else ["AND e.superseded_at IS NULL"]
        if run_id is not None:
            clauses.append("AND e.run_id = :run_id")
            params["run_id"] = run_id
        if change is not None:
            clauses.append("AND e.change = :change")
            params["change"] = change
        if document_id is not None:
            clauses.append("AND e.document_id = :document_id")
            params["document_id"] = document_id
        if zone_id is not None:
            clauses.append("AND COALESCE(e.zone_id, b.zone_id, d.zone_id) = :zone_id")
            params["zone_id"] = zone_id
        if status is not None:
            clauses.append("AND e.review_state = CAST(:state AS review_state)")
            params["state"] = STATUS_TO_DB[status]
        if entity_type is not None:
            clauses.append("AND e.entity_type = :entity_type")
            params["entity_type"] = entity_type
        if urban_parcel_id is not None:
            clauses.append("AND e.urban_parcel_id = :urban_parcel_id")
            params["urban_parcel_id"] = urban_parcel_id
        if source_page is not None:
            clauses.append("AND e.source_page = :source_page")
            params["source_page"] = source_page
        if flag is not None:
            clauses.append("AND e.flags @> CAST(:flag AS jsonb)")
            params["flag"] = json.dumps([flag])
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(_queue_sql(" ".join(clauses))), params))
                .mappings()
                .all()
            )
        now = self.clock()
        items = [_item_out(row, self._link(row, now)) for row in rows]
        total = int(rows[0]["total"]) if rows else 0
        return ReviewPage(items=items, total=total, limit=limit, offset=offset)

    async def get_item(self, item_id: int) -> ReviewItem:
        async with self.session_factory() as session:
            row = await self._item_row(session, item_id)
        return _item_out(row, self._link(row, self.clock()))

    async def _item_row(self, session: AsyncSession, item_id: int) -> Mapping[str, Any]:
        row = (
            (
                await session.execute(
                    ITEM_SQL, {"m": self.municipality_id, "id": item_id, "limit": 1, "offset": 0}
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(f"No review item with id {item_id}", details={"item_id": item_id})
        return row

    def _link(self, row: Mapping[str, Any], now: datetime) -> PageLinkOut | None:
        link = signed_page_link(
            self.storage,
            self.municipality_id,
            document_id=row["document_id"],
            file_key=row["file_key"],
            page_count=row["page_count"],
            page_images_rendered=bool(row["page_images_rendered"]),
            page=row["source_page"],
            expires_in_seconds=self.link_expires_in_seconds,
            now=now,
        )
        return PageLinkOut(**link) if link else None

    # --- decisions -------------------------------------------------------------------------------

    async def approve(self, principal: Principal, item_id: int, note: str | None) -> ReviewItem:
        async with self.session_factory() as session:
            row = await self._item_row(session, item_id)
            await self._decide(session, principal, row, "approved", note=note)
            await session.commit()
        return await self.get_item(item_id)

    async def amend(self, principal: Principal, item_id: int, payload: AmendIn) -> ReviewItem:
        async with self.session_factory() as session:
            row = await self._item_row(session, item_id)
            _, _, value_type = _labels(row)
            amended = _typed_correction(value_type, payload.value)
            await self._decide(
                session,
                principal,
                row,
                "amended",
                note=payload.note,
                amended_text=amended[0],
                amended_number=amended[1],
                amended_unit=payload.unit,
            )
            await session.commit()
        return await self.get_item(item_id)

    async def reject(self, principal: Principal, item_id: int, note: str) -> ReviewItem:
        async with self.session_factory() as session:
            row = await self._item_row(session, item_id)
            await self._decide(session, principal, row, "rejected", note=note)
            await session.commit()
        return await self.get_item(item_id)

    async def _decide(
        self,
        session: AsyncSession,
        principal: Principal,
        row: Mapping[str, Any],
        state: str,
        *,
        note: str | None,
        amended_text: str | None = None,
        amended_number: float | None = None,
        amended_unit: str | None = None,
    ) -> None:
        if row["published_value_id"] is not None:
            raise ConflictError(
                "The item has been published; changes go through a new extraction and publish",
                details={"item_id": row["id"], "reason": "published"},
            )
        if row.get("superseded_at") is not None:
            raise ConflictError(
                "The item was superseded by a later extraction run or decision; review the "
                "current item instead",
                details={
                    "item_id": row["id"],
                    "reason": "superseded",
                    "superseded_by_run_id": row.get("superseded_by_run_id"),
                },
            )
        before = _snapshot(row)
        verb = {"approved": "approve", "amended": "amend", "rejected": "reject"}[state]
        updated = (
            await session.execute(
                DECIDE_SQL,
                {
                    "id": row["id"],
                    "m": self.municipality_id,
                    "state": state,
                    "reviewer": principal.subject,
                    "reviewer_user_id": principal.user_id,
                    "at": self.clock(),
                    "note": note,
                    "amended_text": amended_text,
                    "amended_number": amended_number,
                    "amended_unit": amended_unit,
                },
            )
        ).scalar_one_or_none()
        if updated is None:  # raced with a publish or a superseding run
            raise ConflictError(
                "The item has just been published or superseded", details={"item_id": row["id"]}
            )
        retired = None
        if state in ("approved", "amended") and row.get("previous_item_id") is not None:
            retired = (
                await session.execute(
                    RETIRE_PREVIOUS_SQL,
                    {
                        "id": row["previous_item_id"],
                        "m": self.municipality_id,
                        "run_id": row.get("run_id"),
                        "at": self.clock(),
                    },
                )
            ).scalar_one_or_none()
        after_row = dict(row)
        after_row.update(
            review_state=state,
            review_note=note,
            amended_value_text=amended_text,
            amended_value_number=amended_number,
            amended_unit=amended_unit,
        )
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action=f"review.{verb}",
            entity_type="extraction_item",
            entity_id=row["id"],
            details={
                "document_id": row["document_id"],
                "parameter_key": row["parameter_key"],
                "entity_type": row["entity_type"],
                "urban_parcel_id": row["urban_parcel_id"],
                "source_page": row["source_page"],
                "run_id": row.get("run_id"),
                "superseded_previous_item_id": retired,
            },
            before=before,
            after=_snapshot(after_row),
            note=note,
        )

    async def bulk_approve(self, principal: Principal, payload: BulkApproveIn) -> BulkResult:
        approved: list[int] = []
        skipped: list[BulkSkipped] = []
        async with self.session_factory() as session:
            candidate_ids: list[int] = []
            if payload.item_ids:
                states = (
                    (
                        await session.execute(
                            ITEM_STATE_SQL,
                            {"m": self.municipality_id, "ids": list(payload.item_ids)},
                        )
                    )
                    .mappings()
                    .all()
                )
                by_id = {int(s["id"]): s for s in states}
                for item_id in dict.fromkeys(payload.item_ids):
                    state = by_id.get(item_id)
                    if state is None:
                        skipped.append(BulkSkipped(id=item_id, reason="not_found"))
                    elif state["published_value_id"] is not None:
                        skipped.append(BulkSkipped(id=item_id, reason="published"))
                    elif state["superseded_at"] is not None:
                        skipped.append(BulkSkipped(id=item_id, reason="superseded"))
                    elif state["review_state"] != "pending_review":
                        skipped.append(BulkSkipped(id=item_id, reason="not_pending"))
                    else:
                        candidate_ids.append(item_id)
            selectors: list[str] = []
            params: dict[str, Any] = {"m": self.municipality_id}
            if payload.document_id is not None:
                if payload.source_page is not None:
                    selectors.append("(document_id = :document_id AND source_page = :source_page)")
                    params["source_page"] = payload.source_page
                else:
                    selectors.append("document_id = :document_id")
                params["document_id"] = payload.document_id
            if payload.urban_parcel_id is not None:
                selectors.append("urban_parcel_id = :urban_parcel_id")
                params["urban_parcel_id"] = payload.urban_parcel_id
            if selectors:
                rows = await session.execute(
                    text(
                        "SELECT id FROM planning_parameter_extractions WHERE municipality_id = :m "
                        "AND review_state = 'pending_review' AND published_value_id IS NULL "
                        "AND superseded_at IS NULL "
                        f"AND ({' OR '.join(selectors)}) ORDER BY id"
                    ),
                    params,
                )
                for (item_id,) in rows.all():
                    if item_id not in candidate_ids:
                        candidate_ids.append(int(item_id))
            for item_id in candidate_ids:
                row = await self._item_row(session, item_id)
                await self._decide(session, principal, row, "approved", note=payload.note)
                approved.append(item_id)
            await session.commit()
        return BulkResult(approved=approved, skipped=skipped)

    # --- counters --------------------------------------------------------------------------------

    async def counters(self, *, document_id: int | None = None) -> list[ReviewCounters]:
        params: dict[str, Any] = {"m": self.municipality_id}
        extra = ""
        if document_id is not None:
            extra, params["document_id"] = "AND e.document_id = :document_id", document_id
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(COUNTERS_SQL.format(extra=extra)), params))
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            ok, blockers = can_publish(
                int(row["pending"]), int(row["approved"]), int(row["amended"])
            )
            result.append(
                ReviewCounters(
                    document_id=row["document_id"],
                    document_name=row["document_name"],
                    pending=int(row["pending"]),
                    approved=int(row["approved"]),
                    amended=int(row["amended"]),
                    rejected=int(row["rejected"]),
                    total=int(row["total"]),
                    can_publish=ok,
                    publish_blockers=blockers,
                )
            )
        return result

    # --- audit trail -----------------------------------------------------------------------------

    async def list_audit(
        self,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        actor: str | None = None,
        actor_user_id: int | None = None,
        action: str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AuditPage:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        for column, value in (
            ("entity_type", entity_type),
            ("entity_id", entity_id),
            ("actor", actor),
            ("actor_user_id", actor_user_id),
        ):
            if value is not None:
                clauses.append(f"AND {column} = :{column}")
                params[column] = value
        if action is not None:
            clauses.append("AND action LIKE :action")
            params["action"] = action if "%" in action else action + "%"
        if from_ is not None:
            clauses.append("AND created_at >= :from_at")
            params["from_at"] = _utc(from_)
        if to is not None:
            clauses.append("AND created_at < :to_at")
            params["to_at"] = _utc(to)
        async with self.session_factory() as session:
            rows = (
                (await session.execute(text(AUDIT_SQL.format(extra=" ".join(clauses))), params))
                .mappings()
                .all()
            )
        items = [
            AuditEntry(
                id=r["id"],
                actor=r["actor"],
                actor_user_id=r["actor_user_id"],
                action=r["action"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                before=r["before"],
                after=r["after"],
                note=r["note"],
                details=r["details"] or {},
                request_id=r["request_id"],
                created_at=r["created_at"].astimezone(UTC),
            )
            for r in rows
        ]
        total = int(rows[0]["total"]) if rows else 0
        return AuditPage(items=items, total=total, limit=limit, offset=offset)


def _typed_correction(value_type: str, value: float | str) -> tuple[str | None, float | None]:
    """The corrected value must match the parameter's type (numbers stay numbers, texts texts)."""
    if value_type == "number":
        if isinstance(value, str):
            try:
                value = float(value.replace(",", "."))
            except ValueError:
                raise _validation_error(
                    [{"loc": ["body", "value"], "msg": "this parameter takes a number"}]
                ) from None
        return None, float(value)
    if not isinstance(value, str):
        raise _validation_error([{"loc": ["body", "value"], "msg": "this parameter takes a text"}])
    return value, None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
