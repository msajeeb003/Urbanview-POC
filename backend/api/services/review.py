"""Expert review of AI-extracted planning information (``/v1/admin/review``) and the audit trail
(``/v1/admin/audit``).

100% of extracted values are reviewed before publication; textual accuracy matters as much as
numerical. The queue reads STAGING (``planning_parameter_extractions``) only. A decision never
touches the AI value: *approve* accepts it, *amend* stores the reviewer's corrected value
alongside it (``amended_value_*``; ``effective`` is what would publish), *reject* keeps it out.
A correction is checked and normalised with the extraction contract's own rules
(``core.extraction.corrections``: numbers in the document's conventions and the field's unit,
impossible values refused, unusual ones only when confirmed, floors in the plan's notation, a
land use the document or the profile knows); notes are trimmed and a reason is required.
Approved and amended items are eligible for the publish job (a separate item) and are not served
until published; an item that has been published is closed (409). Every decision writes one
append-only ``audit_log`` row with the state before and after; so does every other admin change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.review import (
    AmendIn,
    AuditEntry,
    AuditPage,
    PageLinkOut,
    ReviewCounters,
    ReviewItem,
    ReviewOption,
    ReviewOptions,
    ReviewPage,
    ReviewPayload,
    ReviewPayloadFloors,
    ReviewPayloadTable,
    ReviewPrevious,
    ReviewRun,
    ReviewSource,
    ReviewTarget,
    ReviewValue,
)
from api.services.audit import write_audit
from api.services.source import signed_page_link
from core.auth import Principal
from core.errors import AppError, ConflictError, NotFoundError
from core.extraction.corrections import CorrectionRefused, check_correction, conventions_for
from core.extraction.schema import UnsupportedSchemaVersion, read_payload
from core.municipality import MunicipalityProfile

log = logging.getLogger("urbanview.review")

STATUS_TO_DB: dict[str, str] = {
    "pending": "pending_review",
    "approved": "approved",
    "amended": "amended",
    "rejected": "rejected",
}
DB_TO_STATUS = {db: api for api, db in STATUS_TO_DB.items()}


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
           e.document_id, d.name AS document_name, d.source_url AS registry_url,
           COALESCE(sf.object_key, d.file_key) AS file_key,
           CASE WHEN sf.id IS NULL THEN d.page_count ELSE sf.page_count END AS page_count,
           COALESCE(sf.id, d.file_id) AS source_file_id,
           COALESCE(sf.original_filename, df.original_filename) AS source_file_name,
           e.source_page, e.source_bbox, e.source_note,
           e.raw_text, e.confidence, e.extracted_by, e.extracted_at, e.reviewer, e.reviewed_at,
           e.review_note, e.published_value_id, e.flags, e.extraction_method, e.schema_version,
           e.prompt_version, e.run_id, e.target_label, e.target_key, e.previous_item_id,
           e.payload,
           er.job_id AS run_job_id, er.model_version AS run_model_version,
           er.estimated_cost_eur AS run_cost, er.items_written AS run_items,
           er.finished_at AS run_finished_at,
           e.change, e.superseded_at, e.superseded_by_run_id,
           p.review_state::text AS previous_state, p.value_text AS previous_value_text,
           p.value_number AS previous_value_number, p.unit AS previous_unit,
           p.amended_value_text AS previous_amended_text,
           p.amended_value_number AS previous_amended_number,
           p.amended_unit AS previous_amended_unit, p.run_id AS previous_run_id,
           count(*) OVER () AS total
    FROM planning_parameter_extractions e
    JOIN planning_documents d ON d.id = e.document_id
    LEFT JOIN extraction_runs er ON er.id = e.run_id
    LEFT JOIN stored_files sf ON sf.id = er.file_id
    LEFT JOIN stored_files df ON df.id = d.file_id
    LEFT JOIN planning_parameter_extractions p ON p.id = e.previous_item_id
    LEFT JOIN planning_fields f ON f.key = e.field_key
    LEFT JOIN urban_parcels u ON u.id = e.urban_parcel_id
    LEFT JOIN urban_blocks b ON b.id = COALESCE(e.block_id, u.block_id)
    LEFT JOIN zones z ON z.id = COALESCE(e.zone_id, b.zone_id, d.zone_id)
    WHERE e.municipality_id = :m
"""
# Within a document: its file, the page, the parcel (natural order: "UP 9" before "UP 12"), the
# field's dictionary order.
_PAGE_ORDER = """e.document_id ASC, COALESCE(er.file_id, d.file_id) ASC NULLS FIRST,
             e.source_page ASC NULLS LAST,
             CAST(substring(COALESCE(u.urban_parcel_number, e.target_label) from '[0-9]+')
                  AS integer) ASC NULLS LAST,
             COALESCE(u.urban_parcel_number, e.target_label, b.block_ref) ASC NULLS LAST,
             f.sort_order ASC NULLS LAST, e.id ASC"""
_ITEM_ORDERS = {
    "pending": f"(e.review_state = 'pending_review') DESC, {_PAGE_ORDER}",
    "page": _PAGE_ORDER,
    "confidence": f"(e.review_state = 'pending_review') DESC, e.confidence ASC NULLS LAST, "
    f"{_PAGE_ORDER}",
}


def _queue_sql(extra: str, sort: str = "pending") -> str:
    order = _ITEM_ORDERS[sort]
    return f"{_ITEM_COLUMNS}{extra}\n    ORDER BY {order}\n    LIMIT :limit OFFSET :offset\n"


ITEM_SQL = text(_queue_sql("AND e.id = :id"))
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
# The wordings a document's items already carry for one field (effective value: amended if
# amended), most frequent first.
OPTIONS_SQL = text(
    """
    SELECT CASE WHEN e.review_state = 'amended' THEN e.amended_value_text
                ELSE e.value_text END AS value, count(*) AS n
    FROM planning_parameter_extractions e
    WHERE e.municipality_id = :m AND e.document_id = :document_id AND e.field_key = :field_key
      AND e.superseded_at IS NULL AND e.review_state <> 'rejected'
      AND CASE WHEN e.review_state = 'amended' THEN e.amended_value_text
               ELSE e.value_text END IS NOT NULL
    GROUP BY 1
    ORDER BY n DESC, value ASC
    LIMIT 200
    """
)
# The wordings a correction of a text field may take without the profile's term table: those
# of the document's items (OPTIONS_SQL) and of the values its published versions serve.
PUBLISHED_WORDINGS_SQL = text(
    """
    SELECT DISTINCT value_text FROM planning_parameter_values
    WHERE municipality_id = :m AND document_id = :document_id AND field_key = :field_key
      AND value_text IS NOT NULL
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
    return row["label_en"], row["label_me"], row["value_type"]


def _payload_out(row: Mapping[str, Any]) -> ReviewPayload | None:
    """The stored payload read with its schema version's reader; null for manual / seeded items
    or a payload no reader takes (logged, the item still lists)."""
    raw = row.get("payload")
    if not raw:
        return None
    try:
        staged = read_payload(row.get("schema_version") or raw.get("schema_version") or "", raw)
    except (UnsupportedSchemaVersion, ValueError) as exc:
        log.warning("review item %s: unreadable payload (%s)", row.get("id"), exc)
        return None
    leaf = staged.leaf
    ref = leaf.source.table_ref
    floors = leaf.derived
    return ReviewPayload(
        schema_version=staged.schema_version,
        task=str(staged.task),
        path=staged.path,
        field_key=staged.field_key,
        urban_parcel_number=staged.urban_parcel_number,
        block_ref=staged.block_ref,
        stated_value=leaf.stated.value,
        stated_unit=leaf.stated.unit,
        value=leaf.value,
        unit=leaf.unit,
        normalisation=list(leaf.normalisation),
        floors=ReviewPayloadFloors(
            notation=floors.notation,
            below_ground=floors.below_ground,
            above_ground=floors.above_ground,
            attic=floors.attic,
        )
        if floors is not None
        else None,
        land_use_class=leaf.category.value if leaf.category is not None else None,
        table=ReviewPayloadTable(**ref.model_dump()) if ref is not None else None,
        flags=[str(f) for f in leaf.flags],
    )


def _item_out(row: Mapping[str, Any], link: PageLinkOut | None) -> ReviewItem:
    label_en, label_me, value_type = _labels(row)
    unit = row["unit"] or row["field_unit"]
    run = None
    if row.get("run_id") is not None:
        cost = row.get("run_cost")
        run = ReviewRun(
            id=row["run_id"],
            job_id=row.get("run_job_id"),
            model_version=row.get("run_model_version"),
            estimated_cost_eur=float(cost) if cost is not None else None,
            items_written=row.get("run_items"),
            finished_at=row["run_finished_at"].astimezone(UTC)
            if row.get("run_finished_at")
            else None,
        )
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
        field_unit=row.get("field_unit"),
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
            file_id=row.get("source_file_id"),
            file_name=row.get("source_file_name"),
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
        run=run,
        payload=_payload_out(row),
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
        file_id: int | None = None,
        change: str | None = None,
        include_superseded: bool = False,
        sort: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> ReviewPage:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = [] if include_superseded else ["AND e.superseded_at IS NULL"]
        if run_id is not None:
            clauses.append("AND e.run_id = :run_id")
            params["run_id"] = run_id
        if file_id is not None:
            clauses.append("AND COALESCE(er.file_id, d.file_id) = :file_id")
            params["file_id"] = file_id
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
                (
                    await session.execute(
                        text(
                            _queue_sql(
                                " ".join(clauses), sort if sort in _ITEM_ORDERS else "pending"
                            )
                        ),
                        params,
                    )
                )
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
        """The item's signed page link; without it (storage or its credentials unavailable) the
        item still lists, with no link, so the queue never fails over a page link."""
        try:
            link = signed_page_link(
                self.storage,
                file_key=row["file_key"],
                page_count=row["page_count"],
                page=row["source_page"],
                expires_in_seconds=self.link_expires_in_seconds,
                now=now,
            )
        except (BotoCoreError, ClientError) as exc:
            log.warning("review page link unavailable (%s: %s)", type(exc).__name__, exc)
            return None
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
            wordings = await self._wordings(session, row)
            try:
                correction = check_correction(
                    row["field_key"],
                    payload.value,
                    payload.unit,
                    conventions=conventions_for(self.municipality_id),
                    known_wordings=wordings,
                    confirm_out_of_range=payload.confirm_out_of_range,
                )
            except CorrectionRefused as refused:
                raise _validation_error([refused.problem()]) from None
            checked = correction.details()
            await self._decide(
                session,
                principal,
                row,
                "amended",
                note=payload.note,
                amended_text=correction.text,
                amended_number=correction.number,
                amended_unit=correction.unit,
                extra_details={"correction": checked} if checked else None,
            )
            await session.commit()
        return await self.get_item(item_id)

    async def _wordings(self, session: AsyncSession, row: Mapping[str, Any]) -> list[str]:
        """For a land-use correction: the wordings the document already uses for the field."""
        if row["field_key"] != "land_use":
            return []
        params = {
            "m": self.municipality_id,
            "document_id": row["document_id"],
            "field_key": row["field_key"],
        }
        staged = [r[0] for r in (await session.execute(OPTIONS_SQL, params)).all()]
        served = [r[0] for r in (await session.execute(PUBLISHED_WORDINGS_SQL, params)).all()]
        return [w for w in dict.fromkeys([*staged, *served]) if w]

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
        extra_details: Mapping[str, Any] | None = None,
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
                **(extra_details or {}),
            },
            before=before,
            after=_snapshot(after_row),
            note=note,
        )

    # --- options ---------------------------------------------------------------------------------

    async def options(self, *, document_id: int, field_key: str) -> ReviewOptions:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    OPTIONS_SQL,
                    {"m": self.municipality_id, "document_id": document_id, "field_key": field_key},
                )
            ).all()
        return ReviewOptions(
            document_id=document_id,
            field_key=field_key,
            values=[ReviewOption(value=str(r[0]), count=int(r[1])) for r in rows],
        )

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
        action: str | None = None,
        actor: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AuditPage:
        """Newest first; ``action`` is a prefix (``review.``), ``actor`` exact."""
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        if actor is not None:
            clauses.append("AND actor = :actor")
            params["actor"] = actor
        if action is not None:
            clauses.append("AND action LIKE :action")
            params["action"] = action if "%" in action else action + "%"
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
