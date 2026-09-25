"""The order flow on PostGIS with storage and mail mocked: guest checkout with snapshot and
e-mail, pricing from configuration, the per-address cap, the guarded status flow with expert
scope and report delivery, snapshot immutability after a republish, and the public status page
without personal data."""

from __future__ import annotations

import re
from email.utils import getaddresses
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core.mail import MailTransientError, SendReceipt, plain_text_of
from core.seeds import placeholder_pdf
from core.staff import create_user, issue_session
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.email import configure_email
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import FakeStorage

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
REFERENCE = re.compile(r"^UV-[A-Z0-9]+-[A-Z0-9-]+-\d{6}-\d{2}$")
CLEANUP = (
    "DELETE FROM staff_login_tokens",
    "DELETE FROM email_log WHERE municipality_id = 'podgorica'",
    "DELETE FROM pipeline_jobs WHERE municipality_id = 'podgorica'",
    "DELETE FROM orders WHERE municipality_id = 'podgorica'",
    "DELETE FROM stored_files WHERE kind = 'expert_report'",
    "DELETE FROM financial_assumptions WHERE created_by <> 'seed'",
    "UPDATE financial_assumptions SET is_current = true, retired_at = NULL, retired_by = NULL "
    "WHERE created_by = 'seed'",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)
FORM = {
    "location": {"parcel_type": "cadastral", "parcel_id": 1001},
    "purchaser_type": "individual",
    "first_name": "Ana",
    "last_name": "Novak",
    "email": "Ana.Novak@Example.com",
    "telephone": "+382 67 123 456",
}
LEGAL = {
    **FORM,
    "purchaser_type": "legal_entity",
    "email": "office@gradnja.me",
    "company_name": "Gradnja d.o.o.",
    "tax_number": "02123456",
    "contact_person": "Marko M.",
    "registered_address": "Bulevar 1, Podgorica",
}
PDF = placeholder_pdf("Expert analysis", 2)


class FakeMailer:
    """The SMTP transport double the send_email job hands its MIME message to; ``sent`` entries
    expose the recipient list, the subject and the plain-text body."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent = []
        self.fail = fail

    def send(self, mime):
        if self.fail:
            raise MailTransientError("smtp down")
        self.sent.append(
            SimpleNamespace(
                to=[address for _, address in getaddresses(mime.get_all("To", []))],
                subject=str(mime["Subject"]),
                text=plain_text_of(mime),
                message_id=str(mime["Message-ID"]),
            )
        )
        return SendReceipt(
            provider_message_id=str(mime["Message-ID"]).strip("<>"), response="250 OK"
        )


def build(postgis_url, mailer=None, **overrides):
    """An app whose e-mails run inline: eager Celery, the SQL job store on the test database and
    the transport double wired into the send_email job."""
    from jobs.enqueue import CeleryDispatcher

    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
        smtp_host="smtp.test",
        **overrides,
    )
    configure_email(
        database_url=postgis_url,
        transport=mailer or FakeMailer(),
        storage=FakeStorage(),
        settings=settings,
    )
    return make_app(settings, storage=FakeStorage(), admin_dispatcher=CeleryDispatcher())


@pytest.fixture(autouse=True)
def _eager_mail(postgis_url, monkeypatch):
    from jobs.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    yield
    configure_job_store(None)
    configure_email(database_url=None, transport=None, storage=None, settings=None)


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def order_app(postgis_url, mailer):
    return build(postgis_url, mailer)


@pytest.fixture(autouse=True)
async def _clean_orders(postgis_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    async def clean() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in CLEANUP:
                    await session.execute(text(statement))
                await session.commit()
        finally:
            await engine.dispose()

    await clean()
    yield
    await clean()


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def staff_token(app, email: str, role: str) -> tuple[int, str]:
    factory = app.state.session_factory
    user_id = await create_user(factory, municipality_id="podgorica", email=email, role=role)
    token = await issue_session(
        factory, municipality_id="podgorica", email=email, created_via="test"
    )
    return user_id, token


async def rows(app, sql: str, **params):
    async with app.state.session_factory() as session:
        result = await session.execute(text(sql), params)
        return [dict(r) for r in result.mappings()]


async def order_id_of(app, reference: str) -> int:
    (row,) = await rows(app, "SELECT id FROM orders WHERE reference = :r", r=reference)
    return int(row["id"])


# --- creation -------------------------------------------------------------------------------------


async def test_guest_order_gets_a_reference_a_snapshot_and_the_payment_email(order_app, mailer):
    app = order_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post(
            "/v1/orders", json={**FORM, "assumptions": {"saleable_share": 0.75}}
        )
        assert created.status_code == 201, created.text
        body = created.json()
        reference = body["reference"]
        public = await client.get(f"/v1/orders/{reference}/status")
        unknown = await client.get("/v1/orders/UV-NOPE-0-000000-00/status")
        legal = await client.post("/v1/orders", json=LEGAL)
        no_company = await client.post(
            "/v1/orders", json={**LEGAL, "company_name": None, "email": "x@gradnja.me"}
        )
        bad_phone = await client.post("/v1/orders", json={**FORM, "telephone": "12"})
        no_parcel = await client.post(
            "/v1/orders",
            json={**FORM, "location": {"parcel_type": "cadastral", "parcel_id": 999_999}},
        )
        urban = await client.post(
            "/v1/orders",
            json={
                **FORM,
                "email": "b@example.com",
                "location": {"parcel_type": "urban", "parcel_id": 1},
            },
        )
        detail = await client.get(
            f"/v1/admin/orders/{await order_id_of(app, reference)}", headers=auth()
        )

    assert REFERENCE.match(reference), reference
    assert reference.startswith("UV-POD")
    assert body["status"] == "pending_payment" and body["status_label_me"] == "čeka uplatu"
    assert body["pricing"] == {
        "basis_area_m2": 959.6,
        "calculation_basis": "urban",
        "tier_up_to_m2": None,
        "price_eur": 200.0,
        "currency": "EUR",
    }
    assert body["turnaround"]["business_days"] == 5
    assert body["location"]["parcel_type"] == "cadastral" and body["location"]["parcel_id"] == 1001
    assert body["location"]["parcel_label"].startswith("KO ")
    assert body["location"]["document_name"] == "DUP Centar – Zona C2"
    instructions = body["payment_instructions"]
    assert instructions["method"] == "bank_transfer"
    assert instructions["amount_eur"] == 200.0 and instructions["reference_to_quote"] == reference
    assert body["status_url"].endswith(f"/orders/{reference}")
    assert body["email_status"] == "sent"
    assert "email" not in body and "first_name" not in body

    mail = mailer.sent[0]
    assert mail.to == ["ana.novak@example.com"]
    assert reference in mail.subject
    for needle in ("200.00 EUR", "placeholder", reference, "Poštovani", body["status_url"]):
        assert needle in mail.text, needle
    log = await rows(app, "SELECT template, status, to_email FROM email_log ORDER BY id")
    assert log[0] == {
        "template": "payment_instructions",
        "status": "sent",
        "to_email": "ana.novak@example.com",
    }
    audit = await rows(
        app,
        "SELECT actor, action, after FROM audit_log WHERE entity_type = 'order' "
        "ORDER BY id LIMIT 1",
    )
    assert audit[0]["actor"] == "guest" and audit[0]["action"] == "order.create"

    assert public.status_code == 200
    assert set(public.json()) == {
        "reference",
        "status",
        "status_label_en",
        "status_label_me",
        "placed_at",
        "status_changed_at",
        "location",
        "turnaround",
    }
    assert public.json()["location"]["parcel_label"] == body["location"]["parcel_label"]
    assert unknown.status_code == 404

    assert legal.status_code == 201, legal.text
    assert no_company.status_code == 422 and bad_phone.status_code == 422
    assert no_parcel.status_code == 404
    assert urban.status_code == 201 and urban.json()["location"]["parcel_type"] == "urban"

    order = detail.json()
    assert order["email"] == "ana.novak@example.com" and order["customer_name"] == "Ana Novak"
    assert order["assumption_edits"] == {"saleable_share": 0.75}
    assert order["snapshot"]["type"] == "cadastral"
    assert order["snapshot"]["assumptions"]["saleable_share"] == 0.75
    assert order["snapshot"]["data_version"] == "sample-2026-09-22"
    assert order["data_version"] == "sample-2026-09-22" and order["formula_version"] == "poc-1"
    assert (order["market_version_id"], order["market_version"]) == (1, 1)
    assert order["assignee"] is None and order["has_report"] is False


async def test_price_comes_from_configuration(postgis_url):
    app = build(postgis_url, order_price_tiers="2000:100,inf:200", order_turnaround_business_days=3)
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/orders", json=FORM)
    assert created.status_code == 201, created.text
    assert created.json()["pricing"]["price_eur"] == 100.0
    assert created.json()["pricing"]["tier_up_to_m2"] == 2000.0
    assert created.json()["turnaround"]["business_days"] == 3


async def test_orders_are_capped_per_email_address_and_day(postgis_url):
    app = build(postgis_url, order_max_per_email_per_day=2)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.post("/v1/orders", json=FORM)
        second = await client.post("/v1/orders", json=FORM)
        third = await client.post("/v1/orders", json=FORM)
    assert first.status_code == 201 and second.status_code == 201
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limited"
    assert third.headers["Retry-After"] == "3600"
    assert first.json()["reference"] != second.json()["reference"]


async def test_mail_trouble_never_fails_the_order(postgis_url):
    app = build(postgis_url, FakeMailer(fail=True))
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/orders", json=FORM)
    assert created.status_code == 201
    assert created.json()["email_status"] == "failed"
    log = await rows(app, "SELECT status, error FROM email_log ORDER BY id")
    assert log[0]["status"] == "failed" and "smtp down" in log[0]["error"]


# --- status flow ----------------------------------------------------------------------------------


async def test_status_flow_guards_expert_scope_and_delivery(order_app, mailer):
    app = order_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reference = (await client.post("/v1/orders", json=FORM)).json()["reference"]
        other_ref = (
            await client.post("/v1/orders", json={**FORM, "email": "c@example.com"})
        ).json()["reference"]
        oid = await order_id_of(app, reference)
        other = await order_id_of(app, other_ref)
        expert_id, expert = await staff_token(app, "expert@example.com", "expert")
        reviewer_id, reviewer = await staff_token(app, "reviewer@example.com", "reviewer")

        too_early = await client.patch(
            f"/v1/admin/orders/{oid}/status", json={"status": "in_progress"}, headers=auth()
        )
        checked = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={"status": "not_received", "note": "nothing on the statement yet"},
            headers=auth(),
        )
        paid = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={"status": "received", "amount_eur": 200, "reference": "BANK-1"},
            headers=auth(reviewer),
        )
        paid_again = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={"status": "received", "amount_eur": 200},
            headers=auth(),
        )
        not_an_expert = await client.post(
            f"/v1/admin/orders/{oid}/assign", json={"expert_user_id": reviewer_id}, headers=auth()
        )
        assigned = await client.post(
            f"/v1/admin/orders/{oid}/assign", json={"expert_user_id": expert_id}, headers=auth()
        )
        no_report = await client.patch(
            f"/v1/admin/orders/{oid}/status", json={"status": "delivered"}, headers=auth()
        )
        expert_list = await client.get("/v1/admin/orders", headers=auth(expert))
        expert_other = await client.get(f"/v1/admin/orders/{other}", headers=auth(expert))
        expert_payment = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={"status": "received", "amount_eur": 1},
            headers=auth(expert),
        )
        not_pdf = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("report.docx", b"PK\x03\x04", "application/octet-stream")},
            headers=auth(expert),
        )
        delivered = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("Expert analysis.pdf", PDF, "application/pdf")},
            data={"note": "final"},
            headers=auth(expert),
        )
        refund_delivered = await client.post(
            f"/v1/admin/orders/{oid}/payment", json={"status": "refunded"}, headers=auth()
        )
        report_pending = await client.post(
            f"/v1/admin/orders/{other}/report",
            files={"file": ("r.pdf", PDF, "application/pdf")},
            headers=auth(),
        )
        await client.post(
            f"/v1/admin/orders/{other}/payment",
            json={"status": "received", "amount_eur": 200},
            headers=auth(),
        )
        refunded = await client.post(
            f"/v1/admin/orders/{other}/payment",
            json={"status": "refunded", "note": "customer cancelled"},
            headers=auth(),
        )
        queue = await client.get("/v1/admin/orders", params={"status": "delivered"}, headers=auth())
        found = await client.get("/v1/admin/orders", params={"search": "novak"}, headers=auth())
        public = await client.get(f"/v1/orders/{reference}/status")
        trail = await client.get(
            "/v1/admin/audit",
            params={"entity_type": "order", "entity_id": oid, "limit": 50},
            headers=auth(),
        )

    assert too_early.status_code == 409
    assert too_early.json()["error"]["details"]["allowed"] == ["paid"]
    assert checked.status_code == 200 and checked.json()["status"] == "pending_payment"
    assert "nothing on the statement yet" in checked.json()["notes"]
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid" and paid.json()["paid_at"]
    assert (
        paid.json()["payment_amount_eur"] == 200.0 and paid.json()["payment_reference"] == "BANK-1"
    )
    assert paid_again.status_code == 409
    assert not_an_expert.status_code == 422
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "in_progress"
    assert assigned.json()["assignee"]["email"] == "expert@example.com"
    assert (
        no_report.status_code == 409
        and no_report.json()["error"]["details"]["reason"] == "no_report"
    )
    assert [o["id"] for o in expert_list.json()["items"]] == [oid]  # only the assigned order
    assert expert_other.status_code == 403 and expert_payment.status_code == 403
    assert not_pdf.status_code == 422
    assert delivered.status_code == 200, delivered.text
    body = delivered.json()
    assert body["status"] == "delivered" and body["has_report"] and body["delivered_at"]
    assert body["report"]["original_filename"] == "Expert_analysis.pdf"
    assert body["report"]["download_url"].endswith("?X-Amz-Signature=sig")
    assert body["report"]["download_expires_at"]
    assert (
        "ready" in mailer.sent[-1].subject
        and body["report"]["download_url"] in mailer.sent[-1].text
    )
    assert refund_delivered.status_code == 409
    assert report_pending.status_code == 409
    assert refunded.status_code == 200 and refunded.json()["status"] == "refunded"
    assert refunded.json()["refunded_at"]
    assert [o["id"] for o in queue.json()["items"]] == [oid]
    assert {o["id"] for o in found.json()["items"]} == {oid, other}
    assert public.json()["status"] == "delivered" and "email" not in public.json()

    log = await rows(
        app, "SELECT template, status FROM email_log WHERE order_id = :o ORDER BY id", o=oid
    )
    assert [entry["template"] for entry in log] == ["payment_instructions", "order_delivered"]
    actions = [e["action"] for e in trail.json()["items"]]  # newest first
    assert actions == [
        "order.status",
        "order.report",
        "order.status",
        "order.assign",
        "order.payment",
        "order.payment_check",
        "order.create",
    ]
    entries = trail.json()["items"]
    assert entries[0]["before"] == {"status": "in_progress"} and entries[0]["after"] == {
        "status": "delivered"
    }
    assert entries[0]["actor"] == "expert@example.com"
    assert entries[4]["actor"] == "reviewer@example.com" and entries[4]["after"]["status"] == "paid"


async def test_snapshot_keeps_the_numbers_the_visitor_saw(order_app):
    app = order_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reference = (await client.post("/v1/orders", json=FORM)).json()["reference"]
        oid = await order_id_of(app, reference)
        before = (await client.get(f"/v1/admin/orders/{oid}", headers=auth())).json()
        republished = await client.post(
            "/v1/admin/assumptions",
            json={
                "zone_id": 1,
                "land_rate": {"expected": 1350},
                "build_rate": {"expected": 999},
                "design_rate": {"expected": 90},
                "sale_rate": {"expected": 2450},
                "source": "Realitica (later publish)",
            },
            headers=auth(),
        )
        fresh = (await client.get("/v1/panel", params={"type": "cadastral", "id": 1001})).json()
        after = (await client.get(f"/v1/admin/orders/{oid}", headers=auth())).json()
    assert republished.status_code == 201
    assert fresh["assumptions"]["construction_cost_eur_m2"] == 999
    assert fresh["assumptions"]["market_version"]["version"] == 2
    assert before["snapshot"]["assumptions"]["construction_cost_eur_m2"] == 860
    assert after["snapshot"] == before["snapshot"]  # the order still shows what was sold
    assert after["market_version_id"] == 1 and after["market_version"] == 1
    assert after["snapshot"]["feasibility"] == before["snapshot"]["feasibility"]
