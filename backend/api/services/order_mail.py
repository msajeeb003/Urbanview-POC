"""Template contexts of the order flow's e-mails (``payment_instructions``, ``order_delivered``).

The ``send_email`` job builds these from the order row at send time (``core.mail.repository``),
so nothing personal travels through the job queue; the same builders feed the unit tests that
render every template against fixture data. Wording lives in ``core/mail/templates``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from core.payments import PaymentInstructions


@dataclass(frozen=True, slots=True)
class OrderFacts:
    reference: str
    first_name: str
    parcel_label: str
    document_name: str | None
    price_eur: float
    turnaround_business_days: int
    expected_by: date
    status_url: str
    support_email: str
    currency: str = "EUR"


def order_status_url(public_base_url: str, reference: str) -> str:
    """The public order page on the map site (status, location, turnaround; no personal data)."""
    return f"{public_base_url.rstrip('/')}/orders/{reference}"


def order_location(facts: OrderFacts) -> str:
    return (
        f"{facts.parcel_label} ({facts.document_name})"
        if facts.document_name
        else facts.parcel_label
    )


def payment_instructions_context(
    facts: OrderFacts, instructions: PaymentInstructions
) -> dict[str, Any]:
    return {
        "reference": facts.reference,
        "first_name": facts.first_name,
        "location": order_location(facts),
        "price_eur": facts.price_eur,
        "currency": facts.currency,
        "beneficiary": instructions.beneficiary,
        "iban": instructions.iban,
        "bank_name": instructions.bank_name,
        "swift": instructions.swift,
        "amount_eur": instructions.amount_eur,
        "reference_to_quote": instructions.reference_to_quote,
        "note_en": instructions.note_en,
        "note_me": instructions.note_me,
        "turnaround_business_days": facts.turnaround_business_days,
        "expected_by": facts.expected_by,
        "status_url": facts.status_url,
        "support_email": facts.support_email,
    }


def order_delivered_context(
    facts: OrderFacts, download_url: str, expires_at: datetime
) -> dict[str, Any]:
    return {
        "reference": facts.reference,
        "first_name": facts.first_name,
        "location": order_location(facts),
        "download_url": download_url,
        "expires_at": expires_at,
        "status_url": facts.status_url,
        "support_email": facts.support_email,
    }


def magic_link_context(
    *, email: str, login_url: str, expires_minutes: int, support_email: str
) -> dict[str, Any]:
    return {
        "email": email,
        "login_url": login_url,
        "expires_minutes": expires_minutes,
        "support_email": support_email,
    }
