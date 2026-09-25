"""Schemas for expert-analysis orders: the public order form and status page (no personal data
returned), and the staff views (queue, detail with the snapshot, status / payment / assignment)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.email import EmailLogOut
from api.schemas.feasibility import EditedAssumptions

OrderStatus = Literal["pending_payment", "paid", "in_progress", "delivered", "refunded"]
PurchaserType = Literal["individual", "legal_entity"]
ParcelType = Literal["cadastral", "urban"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()./-]{5,24}$")
LEGAL_ENTITY_FIELDS = ("company_name", "tax_number", "contact_person", "registered_address")


# --- public ---------------------------------------------------------------------------------------


class OrderLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parcel_type: ParcelType = Field(description="Carried through from the panel that was shown")
    parcel_id: int = Field(gt=0, description="cadastral_parcels.id (Parcel ID) or urban_parcels.id")


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: OrderLocation
    purchaser_type: PurchaserType
    first_name: str | None = Field(
        default=None,
        max_length=100,
        description="Required for an individual; a legal entity is addressed by its contact person",
    )
    last_name: str | None = Field(default=None, max_length=100, description="Optional")
    email: str = Field(max_length=254)
    telephone: str = Field(max_length=30)
    company_name: str | None = Field(default=None, max_length=200)
    tax_number: str | None = Field(default=None, max_length=40, description="PIB / VAT number")
    contact_person: str | None = Field(default=None, max_length=200)
    registered_address: str | None = Field(
        default=None, max_length=500, description="Invoice address of the legal entity"
    )
    assumptions: EditedAssumptions | None = Field(
        default=None, description="The assumptions the visitor edited on the panel, if any"
    )
    message: str | None = Field(default=None, max_length=1000)

    @field_validator(
        "first_name", "last_name", "company_name", "contact_person", "registered_address"
    )
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

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
    def _purchaser_needs_its_fields(self) -> OrderIn:
        """An individual gives a first name (last name optional); a legal entity gives company,
        PIB / VAT, contact person and invoice address, and the contact person is who the order
        e-mails address when no first name is given."""
        if self.purchaser_type == "legal_entity":
            missing = [name for name in LEGAL_ENTITY_FIELDS if not getattr(self, name)]
            if missing:
                raise ValueError(f"a legal entity needs {', '.join(missing)}")
            if not self.first_name:
                self.first_name = self.contact_person
        elif not self.first_name:
            raise ValueError("an individual needs first_name")
        self.last_name = self.last_name or ""
        return self


class PaymentInstructionsOut(BaseModel):
    method: Literal["bank_transfer"]
    beneficiary: str
    iban: str
    bank_name: str | None = None
    swift: str | None = None
    amount_eur: float
    currency: str
    reference_to_quote: str
    note_en: str
    note_me: str


class OrderLocationOut(BaseModel):
    parcel_type: ParcelType
    parcel_id: int
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
    email_status: Literal["queued", "sent", "suppressed", "failed"] = Field(
        description="queued: the send_email job will deliver it; final states when it already ran"
    )


class OrderStatusPublic(BaseModel):
    reference: str
    status: OrderStatus
    status_label_en: str
    status_label_me: str
    placed_at: datetime
    status_changed_at: datetime
    location: OrderLocationOut
    turnaround: TurnaroundOut


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
    price_eur: float
    currency: str
    placed_at: datetime
    status_changed_at: datetime
    expected_by: date
    assignee: Assignee | None = None
    has_report: bool
    email_alerts: int = Field(
        default=0, description="E-mails bounced or failed for this order (see /admin/email-log)"
    )


class OrderOut(OrderSummary):
    emails: list[EmailLogOut] = Field(
        default_factory=list, description="Every e-mail of this order, newest first"
    )
    first_name: str
    last_name: str
    telephone: str
    tax_number: str | None = None
    contact_person: str | None = None
    registered_address: str | None = None
    message: str | None = None
    assumption_edits: dict[str, Any]
    pricing: PricingOut
    turnaround: TurnaroundOut
    data_version: str | None = None
    market_version_id: int | None = None
    market_version: int | None = None
    formula_version: str | None = None
    paid_at: datetime | None = None
    payment_amount_eur: float | None = None
    payment_reference: str | None = None
    payment_received_on: date | None = None
    delivered_at: datetime | None = None
    refunded_at: datetime | None = None
    notes: str | None = None
    report: ReportFileOut | None = None
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
