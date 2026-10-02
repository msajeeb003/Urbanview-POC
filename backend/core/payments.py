"""Bank-transfer payment instructions for expert-analysis orders.

The POC takes **bank transfers** (no hosted checkout, no card data): the order e-mail carries the
amount, the beneficiary's account and the order reference to quote, and staff mark the order paid
in the admin API once the transfer arrives.
"""

from __future__ import annotations

from dataclasses import dataclass


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
                f"Transfer {amount_eur:.2f} EUR to the account shown and quote {reference} as the "
                "payment reference. Work starts when the payment is received."
            ),
            note_me=(
                f"Uplatite {amount_eur:.2f} EUR na navedeni račun i navedite {reference} kao poziv "
                "na broj. Izrada počinje po prijemu uplate."
            ),
        )
