"""Bank-transfer payment instructions for expert-analysis orders.

The POC takes **bank transfers** (no hosted checkout, no card data): the order e-mail carries the
amount, the beneficiary's account and the order reference to quote, and staff mark the order paid
in the admin API once the transfer arrives.

The beneficiary has two sets of account details (the client, 2026-10-05): a customer paying from
a bank in the beneficiary's country gets the **domestic** account number, one paying from abroad
the **international** details (IBAN and SWIFT / BIC). The order says which
(``orders.payment_origin``, the order form's "Paying from" choice) and is shown that set only; an
order that does not say (placed before the form asked) is domestic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# where the customer pays from: one list (ck_orders_payment_origin, the API's enum)
PAYMENT_ORIGINS: tuple[str, ...] = ("domestic", "international")
DEFAULT_PAYMENT_ORIGIN = "domestic"
# the set's name, en / me (the country follows the domestic one in brackets when it is known)
TITLES: dict[str, tuple[str, str]] = {
    "domestic": ("Domestic payment", "Plaćanje u zemlji"),
    "international": ("International payment", "Plaćanje iz inostranstva"),
}


@dataclass(frozen=True, slots=True)
class PaymentInstructions:
    method: str
    origin: str  # domestic | international: the set these details are
    title_en: str
    title_me: str
    beneficiary: str
    beneficiary_address: str | None
    bank_name: str | None
    account_number: str | None  # domestic: the account as the country's banks write it
    iban: str | None  # international (and domestic when no account number is configured)
    swift: str | None  # international
    amount_eur: float
    currency: str
    reference_to_quote: str
    note_en: str
    note_me: str


class BankTransferProvider:
    """Instructions only; staff confirm receipt by hand."""

    name = "bank_transfer"

    def __init__(
        self,
        *,
        beneficiary: str,
        iban: str,
        bank_name: str | None = None,
        swift: str | None = None,
        account_number: str | None = None,
        beneficiary_address: str | None = None,
        country_name: str | None = None,
        country_name_local: str | None = None,
    ) -> None:
        self.beneficiary = beneficiary
        self.iban = iban
        self.bank_name = bank_name
        self.swift = swift
        self.account_number = account_number
        self.beneficiary_address = beneficiary_address
        self.country_name = country_name
        self.country_name_local = country_name_local

    @classmethod
    def from_settings(cls, settings: Any, municipality: Any = None) -> BankTransferProvider:
        """The configured account (``ORDER_BANK_*``); the municipality profile names the country
        a domestic payment is made in."""
        return cls(
            beneficiary=settings.order_bank_beneficiary,
            beneficiary_address=settings.order_bank_beneficiary_address,
            bank_name=settings.order_bank_name,
            account_number=settings.order_bank_account,
            iban=settings.order_bank_iban,
            swift=settings.order_bank_swift,
            country_name=getattr(municipality, "country_name", None),
            country_name_local=getattr(municipality, "country_name_local", None),
        )

    def instructions(
        self, *, reference: str, amount_eur: float, origin: str | None = None
    ) -> PaymentInstructions:
        """The details for a customer paying from ``origin``; none stated = domestic."""
        if origin not in PAYMENT_ORIGINS:
            origin = DEFAULT_PAYMENT_ORIGIN
        domestic = origin == "domestic"
        # a domestic transfer goes to the account number; without one configured, to the IBAN
        account_number = self.account_number if domestic else None
        title_en, title_me = TITLES[origin]
        if domestic and self.country_name:
            title_en = f"{title_en} ({self.country_name})"
        if domestic and (self.country_name_local or self.country_name):
            title_me = f"{title_me} ({self.country_name_local or self.country_name})"
        return PaymentInstructions(
            method=self.name,
            origin=origin,
            title_en=title_en,
            title_me=title_me,
            beneficiary=self.beneficiary,
            beneficiary_address=self.beneficiary_address,
            bank_name=self.bank_name,
            account_number=account_number,
            iban=None if account_number else self.iban,
            swift=None if domestic else self.swift,
            amount_eur=float(amount_eur),
            currency="EUR",
            reference_to_quote=reference,
            note_en=(
                f"Transfer {amount_eur:.2f} EUR to the account shown and quote {reference} as the "
                "payment reference. Work starts when the payment is received."
            ),
            note_me=(
                f"Uplatite {amount_eur:.2f} EUR na navedeni račun i navedite {reference} kao poziv "
                "na broj. Izrada počinje po prijemu uplate."
            ),
        )
