"""Order pieces that need no database: price tiers from configuration, turnaround in business
days, references, the transition table, form validation, the payment seam and the e-mails."""

from __future__ import annotations

from datetime import UTC, date, datetime

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
    assert ko_short(None) == "UP"
    assert reference_token("1042/3") == "1042-3"
    assert reference_token("UP 12") == "UP-12"
    assert reference_token("Čćžšđ") == "CCZS"
    assert build_reference("PODI", "1042-3", date(2026, 9, 24), 7) == "UV-PODI-1042-3-260924-07"


def test_transition_table():
    assert set(TRANSITIONS) == set(STATUSES)
    assert can_transition("pending_payment", "paid")
    assert not can_transition("pending_payment", "in_progress")
    assert not can_transition("pending_payment", "refunded")
    assert can_transition("paid", "in_progress") and can_transition("paid", "refunded")
    assert not can_transition("paid", "delivered")
    assert can_transition("in_progress", "delivered") and can_transition("in_progress", "refunded")
    assert not any(can_transition("delivered", s) for s in STATUSES)
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
        {"first_name": None},
        {"location": {"parcel_type": "zone", "parcel_id": 1}},
        {"location": {"parcel_type": "cadastral", "parcel_id": 0}},
        {"assumptions": {"saleable_share": 1.5}},
        {"password": "x"},
    ):
        with pytest.raises(ValidationError):
            OrderIn(**{**FORM, **bad})
    with pytest.raises(ValidationError) as info:
        OrderIn(**{**FORM, "purchaser_type": "legal_entity", "company_name": "Gradnja d.o.o."})
    assert "tax_number" in str(info.value) and "registered_address" in str(info.value)
    legal = OrderIn(
        **{
            **FORM,
            "purchaser_type": "legal_entity",
            "company_name": "Gradnja d.o.o.",
            "tax_number": "02123456",
            "contact_person": "Marko M.",
            "registered_address": "Bulevar 1, Podgorica",
        }
    )
    assert legal.company_name == "Gradnja d.o.o."
    # the wireframe's forms: an individual may leave the last name out; a legal entity gives a
    # contact person instead of a name, who the e-mails then address
    assert OrderIn(**{**FORM, "last_name": None}).last_name == ""
    assert OrderIn(**{k: v for k, v in FORM.items() if k != "last_name"}).last_name == ""
    company = OrderIn(
        **{
            k: v
            for k, v in {
                **FORM,
                "purchaser_type": "legal_entity",
                "company_name": "Gradnja d.o.o.",
                "tax_number": "02123456",
                "contact_person": " Marko Petrović ",
                "registered_address": "Bulevar 1, Podgorica",
            }.items()
            if k not in ("first_name", "last_name")
        }
    )
    assert company.first_name == "Marko Petrović" and company.last_name == ""


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
    assert provider.checkout_url(reference="x", amount_eur=1, return_url="http://x") is None
    assert provider.parse_webhook(b"{}", {}) is None

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
        "CKBCMEPG",
        "01.10.2026",
        facts.status_url,
        "Poštovani",
        "DUP Centar – Zona C2",
    ):
        assert needle in mail.text, needle
        assert needle in mail.html, needle
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
