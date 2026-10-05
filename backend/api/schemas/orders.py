"""Schemas for expert-analysis orders: the public order form, confirmation and status page (no
personal data returned), and the staff views (queue, detail with the snapshot, status / payment /
assignment)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.email import EmailLogOut, MailLanguage
from api.schemas.feasibility import EditedAssumptions
from core.models.orders import ORDER_STATUSES
from core.payments import PAYMENT_ORIGINS

# the six statuses: one list (core.models.orders.ORDER_STATUSES, ck_orders_status)
OrderStatus = Literal[ORDER_STATUSES]
# where the customer pays from: one list (core.payments.PAYMENT_ORIGINS, ck_orders_payment_origin)
PaymentOrigin = Literal[PAYMENT_ORIGINS]
PurchaserType = Literal["individual", "legal_entity"]
ParcelType = Literal["cadastral", "urban"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()./-]{5,24}$")


# --- public ---------------------------------------------------------------------------------------


class OrderLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parcel_type: ParcelType = Field(description="Carried through from the panel that was shown")
    parcel_id: int = Field(gt=0, description="cadastral_parcels.id (Parcel ID) or urban_parcels.id")


class OrderIn(BaseModel):
    """The pilot scope's guest form: name, e-mail and telephone for everyone; a legal entity may
    add its company name and PIB (company id). No account, no verification."""

    model_config = ConfigDict(extra="forbid")

    location: OrderLocation
    purchaser_type: PurchaserType
    first_name: str = Field(max_length=100, description="Required: who the order e-mails address")
    last_name: str | None = Field(default=None, max_length=100, description="Optional")
    email: str = Field(max_length=254)
    telephone: str = Field(max_length=30)
    company_name: str | None = Field(
        default=None, max_length=200, description="Legal entity only, optional"
    )
    tax_number: str | None = Field(
        default=None, max_length=40, description="PIB (company id); legal entity only, optional"
    )
    assumptions: EditedAssumptions | None = Field(
        default=None, description="The assumptions the visitor edited on the panel, if any"
    )
    message: str | None = Field(default=None, max_length=1000)
    language: MailLanguage | None = Field(
        default=None,
        description="The language the map is in: the order's e-mails are written in it "
        "(default: MAIL_DEFAULT_LANGUAGE)",
    )
    payment_origin: PaymentOrigin | None = Field(
        default=None,
        description="Where the customer pays from (the order form asks): a bank in the "
        "municipality's country (domestic) or abroad (international). The order is shown that "
        "set of bank details (default: domestic)",
    )

    @field_validator("first_name")
    @classmethod
    def _named(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("enter a name")
        return value

    @field_validator("last_name", "company_name", "tax_number")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def _looks_like_an_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("not an e-mail address")
        return value

    @field_validator("telephone")
    @classmethod
    def _looks_like_a_phone_number(cls, value: str) -> str:
        value = value.strip()
        if not PHONE_RE.match(value) or sum(c.isdigit() for c in value) < 6:
            raise ValueError("not a telephone number")
        return value

    @model_validator(mode="after")
    def _company_only_for_a_legal_entity(self) -> OrderIn:
        """An individual's order carries no company; the last name is optional for everyone."""
        if self.purchaser_type == "individual":
            self.company_name = None
            self.tax_number = None
        self.last_name = self.last_name or ""
        return self


class PaymentInstructionsOut(BaseModel):
    """The bank details of the order's payment origin: the domestic account number for a customer
    paying from a bank in the country, the IBAN and SWIFT / BIC for one paying from abroad."""

    method: Literal["bank_transfer"]
    origin: PaymentOrigin = Field(description="The set these details are")
    title_en: str = Field(description='The set by name: "Domestic payment (Montenegro)"')
    title_me: str
    beneficiary: str
    beneficiary_address: str | None = None
    bank_name: str | None = None
    account_number: str | None = Field(
        default=None, description="Domestic: the account as the country's banks write it"
    )
    iban: str | None = Field(
        default=None,
        description="International; domestic too while no account number is configured",
    )
    swift: str | None = Field(default=None, description="International")
    amount_eur: float
    currency: str
    reference_to_quote: str
    note_en: str
    note_me: str


class OrderLocationOut(BaseModel):
    parcel_type: ParcelType
    parcel_id: int
    cadastral_parcel_id: int | None = Field(
        default=None, description="The Parcel ID the map opens (`/?parcel=`), also for urban orders"
    )
    parcel_label: str
    document_name: str | None = None
    zone_name: str | None = None


class PriceTierOut(BaseModel):
    up_to_m2: float | None = Field(
        description="Inclusive upper bound of the area basis; null = open"
    )
    price_eur: float


class OrderPricing(BaseModel):
    """The configured order prices (``ORDER_PRICE_TIERS``): the panel shows the price of a parcel
    by picking the first tier whose bound covers its ``basis_area_m2`` (bounds inclusive), exactly
    as ``POST /v1/orders`` prices it."""

    currency: str = "EUR"
    tiers: list[PriceTierOut]
    turnaround_business_days: int


class PricingOut(BaseModel):
    basis_area_m2: float | None
    calculation_basis: str | None
    tier_up_to_m2: float | None = Field(description="null = the open-ended top tier")
    price_eur: float
    currency: str


class TurnaroundOut(BaseModel):
    business_days: int
    expected_by: date
    note_en: str
    note_me: str


class OrderCreated(BaseModel):
    reference: str
    status: OrderStatus
    status_label_en: str
    status_label_me: str
    placed_at: datetime
    location: OrderLocationOut
    pricing: PricingOut
    turnaround: TurnaroundOut
    payment_instructions: PaymentInstructionsOut
    status_url: str = Field(description="Public status page of this order (no personal data)")
    data_version: str | None = Field(
        default=None, description="Label of the published version the panel was served from"
    )
    email_status: Literal["queued", "sent", "suppressed", "failed"] = Field(
        description="queued: the send_email job will deliver it; final states when it already ran"
    )


class OrderPublic(BaseModel):
    """The confirmation page data (``GET /v1/orders/{reference}``): what the S5 confirmation and
    the public order page show, from the reference alone. Never personal data."""

    reference: str
    status: OrderStatus
    status_label_en: str
    status_label_me: str
    placed_at: datetime
    status_changed_at: datetime
    location: OrderLocationOut
    pricing: PricingOut
    turnaround: TurnaroundOut
    payment_due: bool = Field(description="pending_payment or payment_failed: the transfer is due")
    payment_instructions: PaymentInstructionsOut | None = Field(
        description="The bank-transfer instructions while the payment is due, else null"
    )
    data_version: str | None = Field(
        default=None, description="Label of the published version the order was placed on"
    )
    data_version_no: int | None = Field(
        default=None,
        description="Number of that published version (1, 2, 3 …): what the order page shows",
    )
    status_url: str


# --- staff ----------------------------------------------------------------------------------------


class Assignee(BaseModel):
    user_id: int
    email: str
    display_name: str | None = None


class ReportFileOut(BaseModel):
    file_id: int
    original_filename: str
    uploaded_at: datetime
    download_url: str | None = Field(default=None, description="Signed, short-lived")
    download_expires_at: datetime | None = None


class OrderSummary(BaseModel):
    id: int
    reference: str
    status: OrderStatus
    purchaser_type: PurchaserType
    customer_name: str
    email: str
    company_name: str | None = None
    location: OrderLocationOut
    planned_parcel: str | None = Field(
        default=None, description="The planned (urban) parcel the figures used, as shown (`UP 12`)"
    )
    ko_and_number: str | None = Field(
        default=None, description='The cadastral parcel as shown: "KO {ko}, {number}[/{sub}]"'
    )
    data_version: str | None = Field(
        default=None, description="Label of the published version the visitor saw"
    )
    data_version_no: int | None = Field(
        default=None,
        description="Number of that published version (1, 2, 3 …): what the queue shows (`v12`)",
    )
    price_eur: float
    currency: str
    placed_at: datetime
    status_changed_at: datetime
    turnaround_business_days: int
    expected_by: date
    delivered_at: datetime | None = None
    assignee: Assignee | None = None
    has_report: bool
    email_alerts: int = Field(
        default=0, description="E-mails that failed for this order (listed on its detail)"
    )


class OrderEvent(BaseModel):
    """One audit entry of the order: status changes, payments, assignment, report uploads."""

    id: int
    action: str = Field(description="order.status | order.payment | order.payment_check | ...")
    actor: str
    created_at: datetime
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    note: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ExpertOut(BaseModel):
    user_id: int
    email: str
    display_name: str | None = None
    open_orders: int = Field(description="Orders in progress assigned to them")


class OrderOut(OrderSummary):
    emails: list[EmailLogOut] = Field(
        default_factory=list, description="Every e-mail of this order, newest first"
    )
    first_name: str
    last_name: str
    telephone: str
    tax_number: str | None = None
    customer_id: int | None = Field(default=None, description="customers.id (the guest purchaser)")
    payment_origin: PaymentOrigin | None = Field(
        default=None,
        description="Where the customer pays from (null: placed before the form asked; the "
        "order is shown the domestic details)",
    )
    message: str | None = None
    assumption_edits: dict[str, Any]
    pricing: PricingOut
    turnaround: TurnaroundOut
    publish_version_id: int | None = Field(
        default=None, description="publish_versions.id of the data the visitor saw"
    )
    market_version_id: int | None = None
    market_version: int | None = None
    formula_version: str | None = None
    paid_at: datetime | None = None
    payment_amount_eur: float | None = None
    payment_reference: str | None = None
    payment_received_on: date | None = None
    refunded_at: datetime | None = None
    notes: str | None = None
    report: ReportFileOut | None = None
    report_versions: int = Field(
        default=0, description="Reports uploaded for the order (a replaced report is version 2 …)"
    )
    timeline: list[OrderEvent] = Field(
        default_factory=list, description="The order's audit entries, oldest first"
    )
    snapshot: dict[str, Any] = Field(description="The panel payload the visitor saw")


class OrderList(BaseModel):
    items: list[OrderSummary]
    total: int
    limit: int
    offset: int


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus
    note: str | None = Field(default=None, max_length=2000)


class PaymentIn(BaseModel):
    """``received`` pays the order (also after a failed payment), ``not_received`` records that the
    transfer did not arrive (pending_payment -> payment_failed), ``refunded`` refunds it."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["received", "not_received", "refunded"]
    amount_eur: float | None = Field(default=None, gt=0, le=1_000_000)
    received_on: date | None = None
    reference: str | None = Field(default=None, max_length=100, description="Bank reference")
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _received_needs_an_amount(self) -> PaymentIn:
        if self.status == "received" and self.amount_eur is None:
            raise ValueError("a received payment needs amount_eur")
        return self


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expert_user_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=2000)
