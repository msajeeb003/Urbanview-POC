"""Payment provider seam for expert-analysis orders.

The BRD leaves the provider (Stripe vs Paddle) unverified, so the POC takes **bank transfers**:
the order e-mail carries the amount, the beneficiary's account and the order reference to quote,
and staff mark the order paid in the admin API once the transfer arrives. No card data anywhere.

A card provider later implements :class:`PaymentProvider` — a hosted checkout URL at order time
and a webhook that yields a :class:`PaymentEvent` — and plugs into ``OrderService`` without
touching the order flow, the status machine or the e-mails.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PaymentInstructions:
    method: str
    beneficiary: str
    iban: str
    bank_name: str | None
    swift: str | None
    amount_eur: float
    currency: str
    reference_to_quote: str
    note_en: str
    note_me: str


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    """A confirmed payment reported by a provider (webhook) — never trusted without verification."""

    provider: str
    order_reference: str
    amount_eur: float
    provider_reference: str
    received_at: datetime


class PaymentProvider(Protocol):
    name: str

    def instructions(self, *, reference: str, amount_eur: float) -> PaymentInstructions: ...

    def checkout_url(self, *, reference: str, amount_eur: float, return_url: str) -> str | None:
        """Hosted checkout for card providers; ``None`` when the provider has no online step."""
        ...

    def parse_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        """Verify and decode a provider callback; ``None`` when it is not a payment confirmation."""
        ...


class BankTransferProvider:
    """The POC's provider: instructions only; staff confirm receipt by hand."""

    name = "bank_transfer"

    def __init__(
        self,
        *,
        beneficiary: str,
        iban: str,
        bank_name: str | None = None,
        swift: str | None = None,
    ) -> None:
        self.beneficiary = beneficiary
        self.iban = iban
        self.bank_name = bank_name
        self.swift = swift

    def instructions(self, *, reference: str, amount_eur: float) -> PaymentInstructions:
        return PaymentInstructions(
            method=self.name,
            beneficiary=self.beneficiary,
            iban=self.iban,
            bank_name=self.bank_name,
            swift=self.swift,
            amount_eur=float(amount_eur),
            currency="EUR",
            reference_to_quote=reference,
            note_en=(
                f"Transfer {amount_eur:.2f} EUR to the account above and quote {reference} as the "
                "payment reference. Work starts when the payment is received."
            ),
            note_me=(
                f"Uplatite {amount_eur:.2f} EUR na navedeni račun i navedite {reference} kao poziv "
                "na broj. Izrada počinje po prijemu uplate."
            ),
        )

    def checkout_url(self, *, reference: str, amount_eur: float, return_url: str) -> str | None:
        return None

    def parse_webhook(self, payload: bytes, headers: Mapping[str, str]) -> PaymentEvent | None:
        return None
