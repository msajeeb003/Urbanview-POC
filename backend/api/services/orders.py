"""Expert-analysis orders: guest checkout by bank transfer, a snapshot of what the visitor saw,
a guarded status flow, expert assignment and report delivery.

- ``POST /v1/orders`` (public): the form is validated, the price comes from the configured tiers
  and the parcel's area basis, the reference is ``UV-{KO}-{parcel}-{yymmdd}-{seq}``, the panel
  the visitor saw (with their edited assumptions) is stored as the snapshot together with its
  ``data_version`` and market assumptions version, and the payment-instructions e-mail goes out.
- Status flow ``pending_payment → paid → in_progress → delivered``, ``refunded`` from paid or
  in_progress; anything else is 409. Payment receipt, assignment and the report upload drive
  it; every change is an ``audit_log`` row with before / after.
- Staff: admins and reviewers see and manage everything; an expert sees and delivers only the
  orders assigned to them. The public status page never returns personal data.
- Payments: the provider seam (``core.payments``) is a bank-transfer stub; a card provider plugs
  in there. Every e-mail attempt is an ``email_log`` row; a failed send never fails the order.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api.schemas.orders import (
    Assignee,
    AssignIn,
    OrderCreated,
    OrderIn,
    OrderList,
    OrderLocationOut,
    OrderOut,
    OrderStatusPublic,
    OrderSummary,
    PaymentIn,
    PaymentInstructionsOut,
    PricingOut,
    ReportFileOut,
    TurnaroundOut,
)
from api.schemas.panel import AssumptionOverrides
from api.services.admin import (
    FILE_BY_SHA_SQL,
    INSERT_FILE_SQL,
    MB,
    READ_CHUNK,
    safe_filename,
    validate_upload,
)
from api.services.audit import write_audit
from api.services.email import EMAIL_LOG_JSON, EmailService, email_log_out
from api.services.panel import PanelService
from core.auth import Principal, Role
from core.errors import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from core.municipality import MunicipalityProfile
from core.payments import PaymentInstructions, PaymentProvider
from core.pricing import PriceTier, add_business_days, price_for
from core.storage import ObjectStorage

log = logging.getLogger("urbanview.orders")

STATUSES: tuple[str, ...] = ("pending_payment", "paid", "in_progress", "delivered", "refunded")
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_payment": frozenset({"paid"}),
    "paid": frozenset({"in_progress", "refunded"}),
    "in_progress": frozenset({"delivered", "refunded"}),
    "delivered": frozenset(),
    "refunded": frozenset(),
}
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "pending_payment": ("awaiting payment", "čeka uplatu"),
    "paid": ("paid", "plaćeno"),
    "in_progress": ("in progress", "u izradi"),
    "delivered": ("delivered", "isporučeno"),
    "refunded": ("refunded", "refundirano"),
}
TURNAROUND_NOTE = (
    "Delivery within {n} business days after the payment is received.",
    "Isporuka u roku od {n} radnih dana od prijema uplate.",
)
# the public map's order page (``frontend/src/app/orders/[reference]``), which reads
# ``GET /v1/orders/{reference}/status``
STATUS_PATH = "/orders/{reference}"
MANAGER_ROLES = frozenset({Role.admin, Role.reviewer})
REFERENCE_ATTEMPTS = 25


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def ko_short(ko_name: str | None) -> str:
    """``"Podgorica I"`` -> ``"PODI"``: three letters of the first word plus numerals."""
    if not ko_name:
        return "UP"
    ascii_name = unicodedata.normalize("NFKD", ko_name).encode("ascii", "ignore").decode().upper()
    words = re.findall(r"[A-Z0-9]+", ascii_name)
    if not words:
        return "KO"
    tail = "".join(w for w in words[1:] if re.fullmatch(r"[IVXLC]+|\d+", w))
    return (words[0][:3] + tail)[:8]


def reference_token(label: str) -> str:
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", "-", ascii_label).strip("-")[:16] or "X"


def build_reference(ko: str, parcel: str, day: date, seq: int) -> str:
    return f"UV-{ko}-{parcel}-{day:%y%m%d}-{seq:02d}"


def _validation_error(problems: list[dict[str, Any]]) -> AppError:
    return AppError(
        "Request validation failed", code="validation_error", status_code=422, details=problems
    )


@dataclass(frozen=True, slots=True)
class OrderedLocation:
    parcel_type: str
    parcel_id: int
    cadastral_parcel_id: int | None
    urban_parcel_id: int | None
    parcel_label: str
    document_name: str | None
    zone_id: int | None
    zone_name: str | None
    ko_name: str | None
    parcel_token: str


def location_from_panel(panel: Any) -> OrderedLocation:
    """The identification the visitor saw, as stored on the order."""
    ident = panel.identification
    zone = ident.zone
    if panel.type == "cadastral":
        governing = ident.governing_document
        token = ident.parcel_number + (f"-{ident.sub_number}" if ident.sub_number else "")
        return OrderedLocation(
            parcel_type="cadastral",
            parcel_id=ident.parcel_id,
            cadastral_parcel_id=ident.parcel_id,
            urban_parcel_id=panel.urban_parcel.id if panel.urban_parcel else None,
            parcel_label=panel.header.ko_and_number or f"Parcel {ident.parcel_id}",
            document_name=governing.name if governing else None,
            zone_id=zone.id if zone else None,
            zone_name=zone.name if zone else None,
            ko_name=ident.ko_name,
            parcel_token=reference_token(token),
        )
    cadastral = ident.cadastral_parcel
    return OrderedLocation(
        parcel_type="urban",
        parcel_id=ident.urban_parcel_id,
        cadastral_parcel_id=cadastral.parcel_id if cadastral else None,
        urban_parcel_id=ident.urban_parcel_id,
        parcel_label=ident.urban_parcel_number,
        document_name=ident.governing_document.name,
        zone_id=zone.id if zone else None,
        zone_name=zone.name if zone else None,
        ko_name=cadastral.ko_name if cadastral else None,
        parcel_token=reference_token(ident.urban_parcel_number),
    )


# --- SQL ------------------------------------------------------------------------------------------

INSERT_ORDER_SQL = text(
    """
    INSERT INTO orders (
        municipality_id, reference, status, purchaser_type, first_name, last_name, email,
        telephone, company_name, tax_number, contact_person, registered_address, message,
        parcel_type, parcel_id, cadastral_parcel_id, urban_parcel_id, parcel_label, document_name,
        zone_id, zone_name, basis_area_m2, calculation_basis, price_eur, currency, pricing_tier,
        turnaround_business_days, expected_by, assumption_edits, snapshot, data_version,
        market_version_id, market_version, formula_version)
    VALUES (
        :m, :reference, 'pending_payment', :purchaser_type, :first_name, :last_name, :email,
        :telephone, :company_name, :tax_number, :contact_person, :registered_address, :message,
        :parcel_type, :parcel_id, :cadastral_parcel_id, :urban_parcel_id, :parcel_label,
        :document_name, :zone_id, :zone_name, :basis_area_m2, :calculation_basis, :price_eur,
        'EUR', CAST(:pricing_tier AS jsonb), :turnaround_business_days, :expected_by,
        CAST(:assumption_edits AS jsonb), CAST(:snapshot AS jsonb), :data_version,
        :market_version_id, :market_version, :formula_version)
    RETURNING id, placed_at, status_changed_at
    """
)
ORDERS_SINCE_SQL = text(
    "SELECT count(*) FROM orders WHERE municipality_id = :m AND placed_at >= :since"
)
EMAIL_SINCE_SQL = text(
    "SELECT count(*) FROM orders WHERE municipality_id = :m AND email = :email "
    "AND placed_at >= :since"
)
PUBLIC_STATUS_SQL = text(
    """
    SELECT reference, status, placed_at, status_changed_at, parcel_type, parcel_id, parcel_label,
           document_name, zone_name, turnaround_business_days, expected_by
    FROM orders WHERE municipality_id = :m AND reference = :reference
    """
)
_ORDER_COLUMNS = """
    SELECT o.id, o.reference, o.status, o.purchaser_type, o.first_name, o.last_name, o.email,
           o.telephone, o.company_name, o.tax_number, o.contact_person, o.registered_address,
           o.message, o.parcel_type, o.parcel_id, o.cadastral_parcel_id, o.urban_parcel_id,
           o.parcel_label, o.document_name, o.zone_id, o.zone_name, o.basis_area_m2,
           o.calculation_basis, o.price_eur, o.currency, o.pricing_tier,
           o.turnaround_business_days, o.expected_by, o.assumption_edits, o.data_version,
           o.market_version_id, o.market_version, o.formula_version, o.assignee_user_id,
           u.email AS assignee_email, u.display_name AS assignee_name, o.paid_at,
           o.payment_amount_eur, o.payment_reference, o.payment_received_on, o.delivered_at,
           o.refunded_at, o.report_file_id, f.original_filename AS report_filename,
           f.object_key AS report_key, f.uploaded_at AS report_uploaded_at, o.status_changed_at,
           o.placed_at, o.updated_at, o.notes,
           (SELECT count(*) FROM email_log e WHERE e.order_id = o.id
              AND e.status IN ('bounced', 'failed')) AS email_alerts,
           (SELECT COALESCE(jsonb_agg({email_log_json} ORDER BY e.id DESC), '[]'::jsonb)
            FROM email_log e WHERE e.order_id = o.id) AS emails{snapshot},
           count(*) OVER () AS total
    FROM orders o
    LEFT JOIN staff_users u ON u.id = o.assignee_user_id
    LEFT JOIN stored_files f ON f.id = o.report_file_id
    WHERE o.municipality_id = :m {extra}
    ORDER BY o.placed_at DESC, o.id DESC
    LIMIT :limit OFFSET :offset
"""


def _order_sql(extra: str, *, with_snapshot: bool) -> str:
    return _ORDER_COLUMNS.format(
        snapshot=", o.snapshot" if with_snapshot else "", extra=extra, email_log_json=EMAIL_LOG_JSON
    )


ORDER_BY_ID_SQL = text(_order_sql("AND o.id = :id", with_snapshot=True))
SET_STATUS_SQL = text(
    """
    UPDATE orders
    SET status = :status, status_changed_at = :at, updated_at = :at,
        paid_at = CASE WHEN CAST(:status AS text) = 'paid' THEN :at ELSE paid_at END,
        delivered_at = CASE WHEN CAST(:status AS text) = 'delivered' THEN :at ELSE delivered_at END,
        refunded_at = CASE WHEN CAST(:status AS text) = 'refunded' THEN :at ELSE refunded_at END
    WHERE id = :id AND municipality_id = :m
    """
)
SET_PAID_SQL = text(
    """
    UPDATE orders
    SET status = 'paid', paid_at = :at, status_changed_at = :at, updated_at = :at,
        payment_amount_eur = :amount, payment_reference = :reference,
        payment_received_on = :received_on
    WHERE id = :id AND municipality_id = :m
    """
)
SET_ASSIGNEE_SQL = text(
    "UPDATE orders SET assignee_user_id = :assignee, updated_at = :at "
    "WHERE id = :id AND municipality_id = :m"
)
SET_REPORT_SQL = text(
    """
    UPDATE orders
    SET report_file_id = :file_id, status = 'delivered', delivered_at = :at,
        status_changed_at = :at, updated_at = :at
    WHERE id = :id AND municipality_id = :m
    """
)
APPEND_NOTE_SQL = text(
    "UPDATE orders SET notes = concat_ws(E'\\n', notes, CAST(:note AS text)), updated_at = :at "
    "WHERE id = :id AND municipality_id = :m"
)
INSERT_EMAIL_LOG_SQL = text(
    """
    INSERT INTO email_log (municipality_id, order_id, to_email, template, subject, status, error)
    VALUES (:m, :order_id, :to_email, :template, :subject, :status, :error)
    """
)
STAFF_USER_SQL = text(
    "SELECT id, email, display_name, role, is_active FROM staff_users "
    "WHERE id = :id AND municipality_id = :m"
)


# --- output builders ------------------------------------------------------------------------------


def _labels(status: str) -> tuple[str, str]:
    return STATUS_LABELS.get(status, (status, status))


def _turnaround(days: int, expected_by: date) -> TurnaroundOut:
    return TurnaroundOut(
        business_days=days,
        expected_by=expected_by,
        note_en=TURNAROUND_NOTE[0].format(n=days),
        note_me=TURNAROUND_NOTE[1].format(n=days),
    )


def _location_out(row: Mapping[str, Any]) -> OrderLocationOut:
    return OrderLocationOut(
        parcel_type=row["parcel_type"],
        parcel_id=row["parcel_id"],
        parcel_label=row["parcel_label"],
        document_name=row["document_name"],
        zone_name=row["zone_name"],
    )


def _pricing_out(row: Mapping[str, Any]) -> PricingOut:
    tier = row["pricing_tier"] or {}
    return PricingOut(
        basis_area_m2=row["basis_area_m2"],
        calculation_basis=row["calculation_basis"],
        tier_up_to_m2=tier.get("up_to_m2"),
        price_eur=float(row["price_eur"]),
        currency=row["currency"],
    )


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


def _summary_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    assignee = None
    if row["assignee_user_id"] is not None:
        assignee = Assignee(
            user_id=row["assignee_user_id"],
            email=row["assignee_email"] or "",
            display_name=row["assignee_name"],
        )
    return {
        "id": row["id"],
        "reference": row["reference"],
        "status": row["status"],
        "purchaser_type": row["purchaser_type"],
        "customer_name": f"{row['first_name']} {row['last_name']}".strip(),
        "email": row["email"],
        "company_name": row["company_name"],
        "location": _location_out(row),
        "price_eur": float(row["price_eur"]),
        "currency": row["currency"],
        "placed_at": _utc(row["placed_at"]),
        "status_changed_at": _utc(row["status_changed_at"]),
        "expected_by": row["expected_by"],
        "assignee": assignee,
        "has_report": row["report_file_id"] is not None,
        "email_alerts": int(row.get("email_alerts") or 0),
    }


def _summary(row: Mapping[str, Any]) -> OrderSummary:
    return OrderSummary(**_summary_fields(row))


class OrderService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        panel_service: PanelService,
        storage: Any,
        emails: EmailService,
        provider: PaymentProvider,
        municipality: MunicipalityProfile,
        tiers: list[PriceTier],
        turnaround_business_days: int,
        max_per_email_per_day: int,
        report_link_expires_seconds: int,
        support_email: str,
        public_base_url: str,
        upload_max_bytes: int = 100 * MB,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.panel_service = panel_service
        self.storage = storage
        self.emails = emails
        self.provider = provider
        self.municipality = municipality
        self.tiers = tiers
        self.turnaround_business_days = int(turnaround_business_days)
        self.max_per_email_per_day = int(max_per_email_per_day)
        self.report_link_expires_seconds = int(report_link_expires_seconds)
        self.support_email = support_email
        self.public_base_url = public_base_url.rstrip("/")
        self.upload_max_bytes = int(upload_max_bytes)
        self.clock = clock

    @property
    def municipality_id(self) -> str:
        return self.municipality.id

    def status_url(self, reference: str) -> str:
        return self.public_base_url + STATUS_PATH.format(reference=reference)

    # --- public ----------------------------------------------------------------------------------

    async def create(self, payload: OrderIn) -> OrderCreated:
        now = self.clock()
        async with self.session_factory() as session:
            recent = (
                await session.execute(
                    EMAIL_SINCE_SQL,
                    {
                        "m": self.municipality_id,
                        "email": payload.email,
                        "since": now - timedelta(days=1),
                    },
                )
            ).scalar_one()
        if int(recent) >= self.max_per_email_per_day:
            raise AppError(
                "Too many orders for this e-mail address today; please contact support",
                code="rate_limited",
                status_code=429,
                details={"limit_per_day": self.max_per_email_per_day},
                headers={"Retry-After": "3600"},
            )

        edits = payload.assumptions
        overrides = AssumptionOverrides(
            saleable_share=edits.saleable_share if edits else None,
            construction_cost_eur_m2=edits.construction_cost_per_m2 if edits else None,
            sale_price_eur_m2=edits.selling_price_per_m2 if edits else None,
        )
        panel = await self.panel_service.get_panel(
            payload.location.parcel_type, payload.location.parcel_id, overrides
        )
        area = getattr(panel, "basis_area_m2", None)
        if area is None:
            raise _validation_error(
                [{"loc": ["body", "location"], "msg": "the parcel has no area to price from"}]
            )
        tier = price_for(float(area), self.tiers)
        location = location_from_panel(panel)
        today = now.date()
        expected_by = add_business_days(today, self.turnaround_business_days)
        market_version = panel.assumptions.market_version if panel.assumptions else None
        snapshot = panel.model_dump(mode="json")
        params = {
            "m": self.municipality_id,
            "purchaser_type": payload.purchaser_type,
            "first_name": payload.first_name,
            "last_name": payload.last_name,
            "email": payload.email,
            "telephone": payload.telephone,
            "company_name": payload.company_name,
            "tax_number": payload.tax_number,
            "contact_person": payload.contact_person,
            "registered_address": payload.registered_address,
            "message": payload.message,
            "parcel_type": location.parcel_type,
            "parcel_id": location.parcel_id,
            "cadastral_parcel_id": location.cadastral_parcel_id,
            "urban_parcel_id": location.urban_parcel_id,
            "parcel_label": location.parcel_label,
            "document_name": location.document_name,
            "zone_id": location.zone_id,
            "zone_name": location.zone_name,
            "basis_area_m2": float(area),
            "calculation_basis": getattr(panel, "calculation_basis", None),
            "price_eur": tier.price_eur,
            "pricing_tier": json.dumps({"up_to_m2": tier.up_to_m2, "price_eur": tier.price_eur}),
            "turnaround_business_days": self.turnaround_business_days,
            "expected_by": expected_by,
            "assumption_edits": json.dumps(
                edits.model_dump(mode="json", exclude_none=True) if edits else {}
            ),
            "snapshot": json.dumps(snapshot, default=str),
            "data_version": panel.data_version,
            "market_version_id": market_version.id if market_version else None,
            "market_version": market_version.version if market_version else None,
            "formula_version": panel.formula_version,
        }

        async with self.session_factory() as session:
            base_seq = int(
                (
                    await session.execute(
                        ORDERS_SINCE_SQL,
                        {
                            "m": self.municipality_id,
                            "since": datetime.combine(today, datetime.min.time(), tzinfo=UTC),
                        },
                    )
                ).scalar_one()
            )
        order_id: int | None = None
        reference = ""
        placed_at = now
        for attempt in range(REFERENCE_ATTEMPTS):
            reference = build_reference(
                ko_short(location.ko_name), location.parcel_token, today, base_seq + 1 + attempt
            )
            async with self.session_factory() as session:
                try:
                    row = (
                        (
                            await session.execute(
                                INSERT_ORDER_SQL, {**params, "reference": reference}
                            )
                        )
                        .mappings()
                        .one()
                    )
                except IntegrityError:
                    await session.rollback()
                    continue
                order_id, placed_at = int(row["id"]), row["placed_at"]
                await write_audit(
                    session,
                    municipality_id=self.municipality_id,
                    actor="guest",
                    action="order.create",
                    entity_type="order",
                    entity_id=order_id,
                    details={
                        "reference": reference,
                        "parcel_type": location.parcel_type,
                        "parcel_id": location.parcel_id,
                        "price_eur": tier.price_eur,
                        "purchaser_type": payload.purchaser_type,
                    },
                    after={"status": "pending_payment"},
                )
                await session.commit()
                break
        if order_id is None:
            raise ConflictError("Could not allocate a unique order reference; please retry")

        instructions = self.provider.instructions(reference=reference, amount_eur=tier.price_eur)
        # The payment e-mail is a send_email job over an email_log row; the worker renders it
        # from the order at send time. A queue outage marks the row failed, the order stands.
        email_status = (
            await self.emails.queue(
                template="payment_instructions",
                to=payload.email,
                order_id=order_id,
                requested_by="guest",
            )
        ).status
        labels = _labels("pending_payment")
        return OrderCreated(
            reference=reference,
            status="pending_payment",
            status_label_en=labels[0],
            status_label_me=labels[1],
            placed_at=_utc(placed_at) or now,
            location=OrderLocationOut(
                parcel_type=location.parcel_type,
                parcel_id=location.parcel_id,
                parcel_label=location.parcel_label,
                document_name=location.document_name,
                zone_name=location.zone_name,
            ),
            pricing=PricingOut(
                basis_area_m2=float(area),
                calculation_basis=getattr(panel, "calculation_basis", None),
                tier_up_to_m2=tier.up_to_m2,
                price_eur=tier.price_eur,
                currency="EUR",
            ),
            turnaround=_turnaround(self.turnaround_business_days, expected_by),
            payment_instructions=_instructions_out(instructions),
            status_url=self.status_url(reference),
            email_status=email_status,  # type: ignore[arg-type]
        )

    async def public_status(self, reference: str) -> OrderStatusPublic:
        async with self.session_factory() as session:
            row = (
                (
                    await session.execute(
                        PUBLIC_STATUS_SQL,
                        {"m": self.municipality_id, "reference": reference.strip().upper()},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError(f"No order {reference}", details={"reference": reference})
        labels = _labels(row["status"])
        return OrderStatusPublic(
            reference=row["reference"],
            status=row["status"],
            status_label_en=labels[0],
            status_label_me=labels[1],
            placed_at=_utc(row["placed_at"]),
            status_changed_at=_utc(row["status_changed_at"]),
            location=_location_out(row),
            turnaround=_turnaround(row["turnaround_business_days"], row["expected_by"]),
        )

    # --- staff -----------------------------------------------------------------------------------

    async def list(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        assignee_user_id: int | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> OrderList:
        params: dict[str, Any] = {"m": self.municipality_id, "limit": limit, "offset": offset}
        clauses: list[str] = []
        if principal.role == Role.expert:
            assignee_user_id = principal.user_id  # experts see their own orders only
        if status is not None:
            clauses.append("AND o.status = :status")
            params["status"] = status
        if assignee_user_id is not None:
            clauses.append("AND o.assignee_user_id = :assignee")
            params["assignee"] = assignee_user_id
        if search:
            clauses.append(
                "AND (o.reference ILIKE :search OR o.email ILIKE :search OR o.last_name ILIKE "
                ":search OR o.company_name ILIKE :search OR o.parcel_label ILIKE :search)"
            )
            params["search"] = f"%{search.strip()}%"
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(_order_sql(" ".join(clauses), with_snapshot=False)), params
                    )
                )
                .mappings()
                .all()
            )
        total = int(rows[0]["total"]) if rows else 0
        return OrderList(items=[_summary(r) for r in rows], total=total, limit=limit, offset=offset)

    async def get(self, principal: Principal, order_id: int) -> OrderOut:
        async with self.session_factory() as session:
            row = await self._row(session, order_id)
        self._scope(principal, row)
        return self._detail(row)

    async def _row(self, session: AsyncSession, order_id: int) -> Mapping[str, Any]:
        row = (
            (
                await session.execute(
                    ORDER_BY_ID_SQL,
                    {"m": self.municipality_id, "id": order_id, "limit": 1, "offset": 0},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError(f"No order with id {order_id}", details={"order_id": order_id})
        return row

    def _scope(self, principal: Principal, row: Mapping[str, Any]) -> None:
        if principal.role == Role.expert and row["assignee_user_id"] != principal.user_id:
            raise ForbiddenError(
                "This order is not assigned to you", details={"order_id": row["id"]}
            )

    def _detail(self, row: Mapping[str, Any]) -> OrderOut:
        report = None
        if row["report_file_id"] is not None:
            url, expires_at = None, None
            if row["report_key"]:
                url, expires_at = self._report_link(row["report_key"])
            report = ReportFileOut(
                file_id=row["report_file_id"],
                original_filename=row["report_filename"] or "report.pdf",
                uploaded_at=_utc(row["report_uploaded_at"]) or self.clock(),
                download_url=url,
                download_expires_at=expires_at,
            )
        return OrderOut(
            **_summary_fields(row),
            first_name=row["first_name"],
            last_name=row["last_name"],
            telephone=row["telephone"],
            tax_number=row["tax_number"],
            contact_person=row["contact_person"],
            registered_address=row["registered_address"],
            message=row["message"],
            assumption_edits=row["assumption_edits"] or {},
            pricing=_pricing_out(row),
            turnaround=_turnaround(row["turnaround_business_days"], row["expected_by"]),
            data_version=row["data_version"],
            market_version_id=row["market_version_id"],
            market_version=row["market_version"],
            formula_version=row["formula_version"],
            paid_at=_utc(row["paid_at"]),
            payment_amount_eur=(
                float(row["payment_amount_eur"]) if row["payment_amount_eur"] is not None else None
            ),
            payment_reference=row["payment_reference"],
            payment_received_on=row["payment_received_on"],
            delivered_at=_utc(row["delivered_at"]),
            refunded_at=_utc(row["refunded_at"]),
            notes=row["notes"],
            report=report,
            emails=[email_log_out(e) for e in (row.get("emails") or [])],
            snapshot=row.get("snapshot") or {},
        )

    def _report_link(self, key: str) -> tuple[str, datetime]:
        url = self.storage.presigned_get_url(
            key, self.report_link_expires_seconds, content_type="application/pdf", inline=False
        )
        return url, self.clock() + timedelta(seconds=self.report_link_expires_seconds)

    async def set_status(
        self, principal: Principal, order_id: int, target: str, note: str | None
    ) -> OrderOut:
        self._manager(principal)
        async with self.session_factory() as session:
            row = await self._row(session, order_id)
            self._guard(row, target)
            if target == "delivered" and row["report_file_id"] is None:
                raise ConflictError(
                    "Upload the expert's report to deliver the order",
                    details={"order_id": order_id, "reason": "no_report"},
                )
            now = self.clock()
            await session.execute(
                SET_STATUS_SQL,
                {"id": order_id, "m": self.municipality_id, "status": target, "at": now},
            )
            await self._audit_status(session, principal, row, target, note)
            await session.commit()
        return await self.get(principal, order_id)

    async def record_payment(
        self, principal: Principal, order_id: int, payload: PaymentIn
    ) -> OrderOut:
        self._manager(principal)
        now = self.clock()
        async with self.session_factory() as session:
            row = await self._row(session, order_id)
            if payload.status == "received":
                self._guard(row, "paid")
                await session.execute(
                    SET_PAID_SQL,
                    {
                        "id": order_id,
                        "m": self.municipality_id,
                        "at": now,
                        "amount": payload.amount_eur,
                        "reference": payload.reference,
                        "received_on": payload.received_on or now.date(),
                    },
                )
                await write_audit(
                    session,
                    municipality_id=self.municipality_id,
                    principal=principal,
                    action="order.payment",
                    entity_type="order",
                    entity_id=order_id,
                    details={
                        "reference": row["reference"],
                        "amount_eur": payload.amount_eur,
                        "price_eur": float(row["price_eur"]),
                        "bank_reference": payload.reference,
                    },
                    before={"status": row["status"]},
                    after={"status": "paid", "payment_amount_eur": payload.amount_eur},
                    note=payload.note,
                )
            elif payload.status == "refunded":
                self._guard(row, "refunded")
                await session.execute(
                    SET_STATUS_SQL,
                    {"id": order_id, "m": self.municipality_id, "status": "refunded", "at": now},
                )
                await self._audit_status(session, principal, row, "refunded", payload.note)
            else:  # not_received: nothing changes but the check is on record
                if payload.note:
                    await session.execute(
                        APPEND_NOTE_SQL,
                        {
                            "id": order_id,
                            "m": self.municipality_id,
                            "note": f"{now:%Y-%m-%d} payment not received: {payload.note}",
                            "at": now,
                        },
                    )
                await write_audit(
                    session,
                    municipality_id=self.municipality_id,
                    principal=principal,
                    action="order.payment_check",
                    entity_type="order",
                    entity_id=order_id,
                    details={"reference": row["reference"], "result": "not_received"},
                    before={"status": row["status"]},
                    after={"status": row["status"]},
                    note=payload.note,
                )
            await session.commit()
        return await self.get(principal, order_id)

    async def assign(self, principal: Principal, order_id: int, payload: AssignIn) -> OrderOut:
        self._manager(principal)
        now = self.clock()
        async with self.session_factory() as session:
            row = await self._row(session, order_id)
            if row["status"] in ("delivered", "refunded"):
                raise ConflictError(
                    f"A {row['status']} order cannot be assigned",
                    details={"order_id": order_id, "status": row["status"]},
                )
            expert = (
                (
                    await session.execute(
                        STAFF_USER_SQL, {"m": self.municipality_id, "id": payload.expert_user_id}
                    )
                )
                .mappings()
                .first()
            )
            if expert is None or not expert["is_active"]:
                raise _validation_error(
                    [{"loc": ["body", "expert_user_id"], "msg": "no such active staff user"}]
                )
            if expert["role"] != Role.expert.value:
                raise _validation_error(
                    [{"loc": ["body", "expert_user_id"], "msg": "the user is not an expert"}]
                )
            await session.execute(
                SET_ASSIGNEE_SQL,
                {"id": order_id, "m": self.municipality_id, "assignee": expert["id"], "at": now},
            )
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="order.assign",
                entity_type="order",
                entity_id=order_id,
                details={"reference": row["reference"], "expert_user_id": expert["id"]},
                before={"assignee_user_id": row["assignee_user_id"]},
                after={"assignee_user_id": expert["id"], "assignee_email": expert["email"]},
                note=payload.note,
            )
            if row["status"] == "paid":  # work starts when an expert takes it
                await session.execute(
                    SET_STATUS_SQL,
                    {"id": order_id, "m": self.municipality_id, "status": "in_progress", "at": now},
                )
                await self._audit_status(session, principal, row, "in_progress", "assigned")
            await session.commit()
        return await self.get(principal, order_id)

    async def upload_report(
        self, principal: Principal, order_id: int, upload: UploadFile, note: str | None
    ) -> OrderOut:
        async with self.session_factory() as session:
            row = await self._row(session, order_id)
        self._scope(principal, row)
        if row["status"] != "in_progress":
            raise ConflictError(
                "A report can be uploaded on an order in progress only",
                details={
                    "order_id": order_id,
                    "status": row["status"],
                    "reason": "not_in_progress",
                },
            )
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while chunk := await upload.read(READ_CHUNK):
            total += len(chunk)
            if total > self.upload_max_bytes:
                raise AppError(
                    f"File larger than {self.upload_max_bytes // MB} MB",
                    code="payload_too_large",
                    status_code=413,
                )
            hasher.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise _validation_error([{"loc": ["body", "file"], "msg": "the file is empty"}])
        _, mime = validate_upload("expert_report", upload.filename, upload.content_type, data[:16])
        sha256 = hasher.hexdigest()
        filename = safe_filename(upload.filename, fallback=f"{row['reference']}.pdf")
        key = ObjectStorage.upload_key(self.municipality_id, "expert_report", sha256, filename)
        try:
            await run_in_threadpool(self.storage.put_bytes, key, data, mime)
        except (BotoCoreError, ClientError) as exc:
            log.warning("report upload failed (%s: %s)", type(exc).__name__, exc)
            raise ServiceUnavailableError("Object storage is not available") from exc

        now = self.clock()
        async with self.session_factory() as session:
            try:
                file_id = int(
                    (
                        await session.execute(
                            INSERT_FILE_SQL,
                            {
                                "m": self.municipality_id,
                                "kind": "expert_report",
                                "object_key": key,
                                "sha256": sha256,
                                "original_filename": filename,
                                "mime_type": mime,
                                "size_bytes": len(data),
                                "page_count": None,
                                "uploaded_by": principal.subject,
                                "uploaded_by_user_id": principal.user_id,
                            },
                        )
                    ).scalar_one()
                )
            except IntegrityError:  # the same PDF was stored before: reuse it
                await session.rollback()
                file_id = int(
                    (
                        await session.execute(
                            FILE_BY_SHA_SQL, {"m": self.municipality_id, "sha256": sha256}
                        )
                    ).scalar_one()
                )
            await session.execute(
                SET_REPORT_SQL,
                {"id": order_id, "m": self.municipality_id, "file_id": file_id, "at": now},
            )
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="order.report",
                entity_type="order",
                entity_id=order_id,
                details={"reference": row["reference"], "file_id": file_id, "filename": filename},
                before={"report_file_id": row["report_file_id"]},
                after={"report_file_id": file_id},
                note=note,
            )
            await self._audit_status(session, principal, row, "delivered", note)
            await session.commit()

        await self.emails.queue(
            template="order_delivered",
            to=row["email"],
            order_id=order_id,
            requested_by=principal.subject,
            requested_by_user_id=principal.user_id,
        )
        return await self.get(principal, order_id)

    # --- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _manager(principal: Principal) -> None:
        if principal.role not in MANAGER_ROLES:
            raise ForbiddenError("Only admins and reviewers manage orders")

    @staticmethod
    def _guard(row: Mapping[str, Any], target: str) -> None:
        if not can_transition(row["status"], target):
            raise ConflictError(
                f"An order cannot go from {row['status']} to {target}",
                details={
                    "order_id": row["id"],
                    "status": row["status"],
                    "target": target,
                    "allowed": sorted(TRANSITIONS.get(row["status"], ())),
                },
            )

    async def _audit_status(
        self,
        session: AsyncSession,
        principal: Principal,
        row: Mapping[str, Any],
        target: str,
        note: str | None,
    ) -> None:
        await write_audit(
            session,
            municipality_id=self.municipality_id,
            principal=principal,
            action="order.status",
            entity_type="order",
            entity_id=row["id"],
            details={"reference": row["reference"]},
            before={"status": row["status"]},
            after={"status": target},
            note=note,
        )


def _instructions_out(instructions: PaymentInstructions) -> PaymentInstructionsOut:
    return PaymentInstructionsOut(
        method=instructions.method,  # type: ignore[arg-type]
        beneficiary=instructions.beneficiary,
        iban=instructions.iban,
        bank_name=instructions.bank_name,
        swift=instructions.swift,
        amount_eur=instructions.amount_eur,
        currency=instructions.currency,
        reference_to_quote=instructions.reference_to_quote,
        note_en=instructions.note_en,
        note_me=instructions.note_me,
    )
