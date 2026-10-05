"""Order pieces that need no database: price tiers from configuration, turnaround in business
days, references, the transition table, form validation, the payment seam and the e-mails."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.schemas.orders import OrderIn, PaymentIn
from api.services.order_mail import (
    OrderFacts,
    order_delivered_context,
    payment_instructions_context,
)
from api.services.orders import (
    STATUSES,
    TRANSITIONS,
    build_reference,
    can_transition,
    ko_short,
    reference_token,
)
from core.mail import render
from core.payments import BankTransferProvider
from core.pricing import PriceTier, add_business_days, parse_price_tiers, price_for
from tests.helpers import make_settings

FORM = {
    "location": {"parcel_type": "cadastral", "parcel_id": 1001},
    "purchaser_type": "individual",
    "first_name": " Ana ",
    "last_name": "Novak",
    "email": " Ana.Novak@Example.com ",
    "telephone": "+382 67 123 456",
}


def test_price_tiers_are_parsed_and_validated():
    assert parse_price_tiers("500:100,inf:200") == [
        PriceTier(up_to_m2=500.0, price_eur=100.0),
        PriceTier(up_to_m2=None, price_eur=200.0),
    ]
    assert parse_price_tiers(" 250 : 50 , 1000:120, * : 200 ")[2] == PriceTier(None, 200.0)
    for bad in (
        "",
        "500:100",  # no open-ended tier
        "inf:200,500:100",  # open-ended not last
        "700:100,500:150,inf:200",  # not ascending
        "500:abc,inf:1",
        "500:-1,inf:2",
        "0:10,inf:20",
        "500",
    ):
        with pytest.raises(ValueError):
            parse_price_tiers(bad)


def test_price_follows_the_configured_boundaries():
    tiers = parse_price_tiers("500:100,inf:200")
    assert price_for(0, tiers).price_eur == 100
    assert price_for(500, tiers).price_eur == 100  # inclusive bound
    assert price_for(500.01, tiers).price_eur == 200
    assert price_for(10_000, tiers).up_to_m2 is None
    three = parse_price_tiers("300:50,1000:120,inf:200")
    assert [price_for(a, three).price_eur for a in (299, 300, 301, 1000, 1001)] == [
        50,
        50,
        120,
        120,
        200,
    ]


def test_settings_refuse_bad_tiers():
    assert make_settings().order_price_tiers == "500:100,inf:200"
    with pytest.raises(ValidationError):
        make_settings(order_price_tiers="500:100")


def test_turnaround_counts_business_days_only():
    friday = date(2026, 9, 25)
    assert add_business_days(friday, 1) == date(2026, 9, 28)  # Monday
    assert add_business_days(friday, 5) == date(2026, 10, 2)
    assert add_business_days(date(2026, 9, 23), 5) == date(2026, 9, 30)
    assert add_business_days(friday, 0) == friday


def test_ko_short_and_references():
    assert ko_short("Podgorica I") == "PODI"
    assert ko_short("Podgorica III") == "PODIII"
    assert ko_short("Donja Gorica 2") == "DON2"
    assert ko_short("Čevo") == "CEV"
    assert ko_short(None) == ""
    # a planned parcel without a cadastral parcel: its own number once, no doubled "UP"
    assert build_reference("", "UP-C2962", date(2026, 10, 1), 1) == "UV-UP-C2962-261001-01"
    assert reference_token("1042/3") == "1042-3"
    assert reference_token("UP 12") == "UP-12"
    assert reference_token("Čćžšđ") == "CCZS"
    assert build_reference("PODI", "1042-3", date(2026, 9, 24), 7) == "UV-PODI-1042-3-260924-07"


def test_transition_table():
    assert set(TRANSITIONS) == set(STATUSES)
    assert can_transition("pending_payment", "paid")
    # the transfer did not arrive; it can still be paid, nothing else
    assert can_transition("pending_payment", "payment_failed")
    assert can_transition("payment_failed", "paid")
    assert TRANSITIONS["payment_failed"] == frozenset({"paid"})
    assert not can_transition("paid", "payment_failed")
    assert not can_transition("pending_payment", "in_progress")
    assert not can_transition("pending_payment", "refunded")
    assert can_transition("paid", "in_progress") and can_transition("paid", "refunded")
    assert not can_transition("paid", "delivered")
    assert can_transition("in_progress", "delivered") and can_transition("in_progress", "refunded")
    # a delivered order never goes back to work; money can still go back (a refund)
    assert [s for s in STATUSES if can_transition("delivered", s)] == ["refunded"]
    assert not any(can_transition("refunded", s) for s in STATUSES)


def test_order_form_validation():
    order = OrderIn(**FORM)
    assert order.first_name == "Ana" and order.email == "ana.novak@example.com"
    assert OrderIn(**{**FORM, "telephone": "067/123-456"}).telephone == "067/123-456"
    for bad in (
        {"email": "not-an-email"},
        {"telephone": "12"},
        {"telephone": "call me"},
        {"first_name": ""},
        {"first_name": "   "},
        {"first_name": None},
        {"location": {"parcel_type": "zone", "parcel_id": 1}},
        {"location": {"parcel_type": "cadastral", "parcel_id": 0}},
        {"assumptions": {"saleable_share": 1.5}},
        {"password": "x"},
    ):
        with pytest.raises(ValidationError):
            OrderIn(**{**FORM, **bad})
    # the pilot scope's form: name, e-mail and telephone for everyone; the company name and PIB
    # of a legal entity are optional; contact person and invoice address are not asked any more
    legal = OrderIn(
        **{
            **FORM,
            "purchaser_type": "legal_entity",
            "company_name": " Gradnja d.o.o. ",
            "tax_number": "02123456",
        }
    )
    assert legal.company_name == "Gradnja d.o.o." and legal.tax_number == "02123456"
    bare = OrderIn(**{**FORM, "purchaser_type": "legal_entity", "company_name": "  "})
    assert bare.company_name is None and bare.tax_number is None
    with pytest.raises(ValidationError):  # a legal entity is still named
        OrderIn(
            **{
                k: v
                for k, v in {**FORM, "purchaser_type": "legal_entity"}.items()
                if k != "first_name"
            }
        )
    for gone in ({"contact_person": "Marko M."}, {"registered_address": "Bulevar 1, Podgorica"}):
        with pytest.raises(ValidationError):
            OrderIn(**{**FORM, "purchaser_type": "legal_entity", **gone})
    # an individual's order carries no company
    individual = OrderIn(**{**FORM, "company_name": "Gradnja d.o.o.", "tax_number": "02123456"})
    assert individual.company_name is None and individual.tax_number is None
    # the last name is optional
    assert OrderIn(**{**FORM, "last_name": None}).last_name == ""
    assert OrderIn(**{k: v for k, v in FORM.items() if k != "last_name"}).last_name == ""
    assert OrderIn(**{**FORM, "first_name": " Ana "}).first_name == "Ana"
    # the language the map was in: one of the app's two, or none (the default applies)
    assert order.language is None and OrderIn(**{**FORM, "language": "me"}).language == "me"
    with pytest.raises(ValidationError):
        OrderIn(**{**FORM, "language": "de"})
    # where the customer pays from: one of the two origins, or none (domestic applies)
    assert order.payment_origin is None
    for origin in ("domestic", "international"):
        assert OrderIn(**{**FORM, "payment_origin": origin}).payment_origin == origin
    with pytest.raises(ValidationError):
        OrderIn(**{**FORM, "payment_origin": "abroad"})


def test_payment_payload():
    with pytest.raises(ValidationError):
        PaymentIn(status="received")
    assert PaymentIn(status="received", amount_eur=200).amount_eur == 200
    assert PaymentIn(status="not_received", note="nothing yet").status == "not_received"
    assert PaymentIn(status="refunded").amount_eur is None


def test_bank_transfer_provider_and_emails():
    provider = BankTransferProvider(
        beneficiary="UrbanView d.o.o.", iban="ME12 3456", bank_name="CKB", swift="CKBCMEPG"
    )
    instructions = provider.instructions(reference="UV-PODI-1042-260924-01", amount_eur=200)
    assert instructions.method == "bank_transfer"
    assert instructions.amount_eur == 200.0 and instructions.currency == "EUR"
    assert "UV-PODI-1042-260924-01" in instructions.note_en
    # only an IBAN configured: a domestic transfer goes to it; the SWIFT code is for abroad
    assert instructions.origin == "domestic" and instructions.title_en == "Domestic payment"
    assert (instructions.account_number, instructions.iban, instructions.swift) == (
        None,
        "ME12 3456",
        None,
    )
    abroad = provider.instructions(reference="R", amount_eur=1, origin="international")
    assert (abroad.account_number, abroad.iban, abroad.swift) == (None, "ME12 3456", "CKBCMEPG")

    facts = OrderFacts(
        reference="UV-PODI-1042-260924-01",
        first_name="Ana",
        parcel_label="KO Podgorica I, 1042",
        document_name="DUP Centar – Zona C2",
        price_eur=200.0,
        turnaround_business_days=5,
        expected_by=date(2026, 10, 1),
        status_url="http://localhost:3000/orders/UV-PODI-1042-260924-01",
        support_email="support@urbanview.io",
    )
    mail = render("payment_instructions", payment_instructions_context(facts, instructions))
    assert "UV-PODI-1042-260924-01" in mail.subject
    for needle in (
        "200.00 EUR",
        "ME12 3456",
        "01.10.2026",
        facts.status_url,
        "Dear Ana",
        "DUP Centar – Zona C2",
    ):
        assert needle in mail.text, needle
        assert needle in mail.html, needle
    assert "CKBCMEPG" not in mail.text  # a domestic transfer needs no SWIFT code
    context = payment_instructions_context(facts, instructions)
    in_montenegrin = render("payment_instructions", context, language="me")
    assert "Poštovani/a Ana" in in_montenegrin.text and "Dear Ana" not in in_montenegrin.text
    assert instructions.note_me in in_montenegrin.text and instructions.note_en in mail.text
    delivered = render(
        "order_delivered",
        order_delivered_context(
            facts,
            "https://minio.test/report.pdf?sig",
            datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
        ),
    )
    assert "ready" in delivered.subject
    assert "https://minio.test/report.pdf?sig" in delivered.text
    assert "08.10.2026 09:00 UTC" in delivered.text and "support@urbanview.io" in delivered.html


def test_domestic_and_international_bank_details():
    """Two sets of details for one beneficiary (the client, 2026-10-05): the domestic account
    number for a customer paying from a bank in the country, the IBAN and SWIFT / BIC for one
    paying from abroad; an order shows the set of its payment origin, never both."""
    settings = make_settings(
        order_bank_beneficiary="Primjer d.o.o.",
        order_bank_beneficiary_address="Ulica 1, 81000 Podgorica, Montenegro",
        order_bank_name="Primjer Banka AD",
        order_bank_account="550-12345-67",
        order_bank_iban="ME25505000012345678951",
        order_bank_swift="PRIMMEPG",
    )
    profile = SimpleNamespace(country_name="Montenegro", country_name_local="Crna Gora")
    provider = BankTransferProvider.from_settings(settings, profile)

    domestic = provider.instructions(reference="UV-UP-40-261005-01", amount_eur=200)
    assert domestic.origin == "domestic"
    assert domestic.title_en == "Domestic payment (Montenegro)"
    assert domestic.title_me == "Plaćanje u zemlji (Crna Gora)"
    assert (domestic.account_number, domestic.iban, domestic.swift) == ("550-12345-67", None, None)
    # an order that never said (placed before the form asked) or says nonsense is domestic
    for unstated in (None, "", "abroad"):
        assert (
            provider.instructions(reference="R", amount_eur=1, origin=unstated).origin == "domestic"
        )

    abroad = provider.instructions(
        reference="UV-UP-40-261005-01", amount_eur=200, origin="international"
    )
    assert abroad.origin == "international"
    assert (abroad.title_en, abroad.title_me) == (
        "International payment",
        "Plaćanje iz inostranstva",
    )
    assert (abroad.account_number, abroad.iban, abroad.swift) == (
        None,
        "ME25505000012345678951",
        "PRIMMEPG",
    )
    # both name the same beneficiary, bank and address, and the order's reference to quote
    for one in (domestic, abroad):
        assert (one.beneficiary, one.bank_name) == ("Primjer d.o.o.", "Primjer Banka AD")
        assert one.beneficiary_address == "Ulica 1, 81000 Podgorica, Montenegro"
        assert one.reference_to_quote == "UV-UP-40-261005-01" and one.currency == "EUR"

    # no country named in the profile: the titles stand alone
    bare = BankTransferProvider.from_settings(settings)
    assert bare.instructions(reference="R", amount_eur=1).title_en == "Domestic payment"
    # a blank setting in an env file is no setting
    blank = make_settings(order_bank_account=" ", order_bank_swift="", order_bank_name="")
    assert (blank.order_bank_account, blank.order_bank_swift, blank.order_bank_name) == (
        None,
        None,
        None,
    )

    facts = OrderFacts(
        reference="UV-UP-40-261005-01",
        first_name="Ana",
        parcel_label="UP 40",
        document_name="DUP Novi Grad 1 i 2",
        price_eur=200.0,
        turnaround_business_days=5,
        expected_by=date(2026, 10, 12),
        status_url="http://localhost:3000/orders/UV-UP-40-261005-01",
        support_email="support@urbanview.io",
    )
    shown = {
        "domestic": ("550-12345-67",),
        "international": ("ME25505000012345678951", "PRIMMEPG"),
    }
    labels = {
        "en": {
            "domestic": ("Domestic payment (Montenegro)", "Account number:"),
            "international": ("International payment", "IBAN:", "SWIFT/BIC:"),
        },
        "me": {
            "domestic": ("Plaćanje u zemlji (Crna Gora)", "Broj računa:"),
            "international": ("Plaćanje iz inostranstva", "IBAN:", "SWIFT/BIC:"),
        },
    }
    for origin, instructions in (("domestic", domestic), ("international", abroad)):
        other = "international" if origin == "domestic" else "domestic"
        context = payment_instructions_context(facts, instructions)
        for language in ("en", "me"):
            mail = render("payment_instructions", context, language=language)
            for needle in (
                *shown[origin],
                "Primjer d.o.o.",
                "Primjer Banka AD",
                "Ulica 1, 81000 Podgorica, Montenegro",
                "200.00 EUR",
                "UV-UP-40-261005-01",
            ):
                assert needle in mail.text and needle in mail.html, (origin, language, needle)
            for label in labels[language][origin]:
                assert label in mail.text, (origin, language, label)
            # the other set's account never appears: one set per order
            for needle in (*shown[other], labels[language][other][0]):
                assert needle not in mail.text and needle not in mail.html, (
                    origin,
                    language,
                    needle,
                )
