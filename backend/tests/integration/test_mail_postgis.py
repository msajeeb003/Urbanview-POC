"""Transactional e-mail on PostGIS through the API with Celery in eager mode and the SMTP
transport faked: the payment e-mail of a new order lands in ``email_log`` with the provider's
message id and shows on the order, the log endpoint and the jobs API; the magic-link login
(request → e-mail → exchange → session; single use; expiry); the staging allow-list; bounces
surfacing on the order; transient SMTP trouble retried then failed."""

from __future__ import annotations

from email.utils import getaddresses
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.mail import MailTransientError, SendReceipt, plain_text_of
from core.staff import create_user
from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.email import configure_email
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import FakeStorage
from tests.integration.test_orders_postgis import FORM, LEGAL

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234-staging-ready"  # ≥ 24 characters: the staging guard
CLEANUP = (
    "DELETE FROM staff_login_tokens",
    "DELETE FROM email_log WHERE municipality_id = 'podgorica'",
    "DELETE FROM orders WHERE municipality_id = 'podgorica'",
    "DELETE FROM pipeline_jobs WHERE municipality_id = 'podgorica'",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)


class Transport:
    """The SMTP double: records what the send_email job hands it."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[SimpleNamespace] = []
        self.fail = fail

    def send(self, mime):
        if self.fail:
            raise MailTransientError("451 provider busy")
        self.sent.append(
            SimpleNamespace(
                to=[address for _, address in getaddresses(mime.get_all("To", []))],
                subject=str(mime["Subject"]),
                text=plain_text_of(mime),
                message_id=str(mime["Message-ID"]).strip("<>"),
                template=str(mime["X-UrbanView-Template"]),
            )
        )
        return SendReceipt(
            provider_message_id="ses-0100019a2b3c", response="250 Ok ses-0100019a2b3c"
        )


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
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


@pytest.fixture
def transport():
    return Transport()


@pytest.fixture
def mail_env(postgis_url, transport, monkeypatch):
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))

    def build(transport_override=None, **overrides):
        settings = make_settings(
            location_resolver="postgis",
            database_url=postgis_url,
            rate_limit_requests=100_000,
            admin_api_tokens=f"{TOKEN}:admin:ops",
            smtp_host="smtp.test",
            admin_base_url="http://admin.test",
            **overrides,
        )
        configure_email(
            database_url=postgis_url,
            transport=transport_override or transport,
            storage=FakeStorage(),
            settings=settings,
        )
        return make_app(settings, storage=FakeStorage(), admin_dispatcher=CeleryDispatcher())

    yield build
    configure_job_store(None)
    configure_email(database_url=None, transport=None, storage=None, settings=None)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def rows(app, sql: str, **params) -> list[dict]:
    async with app.state.session_factory() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


async def test_the_payment_email_is_sent_logged_and_visible_everywhere(mail_env, transport):
    app = mail_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/orders", json=FORM)
        assert created.status_code == 201, created.text
        reference = created.json()["reference"]
        (order,) = await rows(app, "SELECT id FROM orders WHERE reference = :r", r=reference)
        log = await client.get(
            "/v1/admin/email-log", params={"order_id": order["id"]}, headers=auth()
        )
        detail = await client.get(f"/v1/admin/orders/{order['id']}", headers=auth())
        entry_id = log.json()["items"][0]["id"]
        one = await client.get(f"/v1/admin/email-log/{entry_id}", headers=auth())
        jobs = await client.get(
            "/v1/admin/jobs", params={"target": f"email:{entry_id}"}, headers=auth()
        )
        by_template = await client.get(
            "/v1/admin/email-log", params={"template": "magic_link"}, headers=auth()
        )
        anonymous = await client.get("/v1/admin/email-log")

    assert created.json()["email_status"] == "sent"  # eager: the job already ran
    mail = transport.sent[0]
    assert mail.to == ["ana.novak@example.com"] and mail.template == "payment_instructions"
    assert reference in mail.subject and reference in mail.text and "200.00 EUR" in mail.text
    assert "KO " in mail.text and "Podgorica" in mail.text
    entry = log.json()["items"][0]
    assert log.json()["total"] == 1
    assert (entry["template"], entry["status"], entry["attempts"]) == (
        "payment_instructions",
        "sent",
        1,
    )
    assert entry["to_email"] == "ana.novak@example.com" and entry["order_id"] == order["id"]
    assert entry["provider_message_id"] == "ses-0100019a2b3c" and entry["sent_at"]
    assert entry["subject"] == mail.subject and entry["job_id"]
    assert "text" not in entry and "html" not in entry and "body" not in entry
    assert one.json() == entry
    body = detail.json()
    assert body["email_alerts"] == 0 and [e["id"] for e in body["emails"]] == [entry_id]
    job = jobs.json()["items"][0]
    assert job["type"] == "send_email" and job["status"] == "succeeded"
    assert job["payload"] == {
        "template": "payment_instructions",
        "email_log_id": entry_id,
        "order_id": order["id"],
        "user_id": None,
    }
    assert job["result"]["provider_message_id"] == "ses-0100019a2b3c"
    assert "@" not in str(job["payload"]) and "@" not in str(job["result"])  # ids only
    assert by_template.json()["total"] == 0 and anonymous.status_code == 401
    (stored,) = await rows(
        app,
        "SELECT provider_response, error, suppressed_reason FROM email_log WHERE id = :id",
        id=entry_id,
    )
    assert stored == {
        "provider_response": "250 Ok ses-0100019a2b3c",
        "error": None,
        "suppressed_reason": None,
    }


async def test_magic_link_login_is_single_use_and_short_lived(mail_env, transport):
    app = mail_env(magic_link_expires_seconds=600)
    async with app.router.lifespan_context(app), make_client(app) as client:
        user_id = await create_user(
            app.state.session_factory,
            municipality_id="podgorica",
            email="Vesna@Example.com",
            role="admin",
        )
        requested = await client.post("/v1/auth/magic-link", json={"email": "vesna@example.com"})
        unknown = await client.post("/v1/auth/magic-link", json={"email": "nobody@example.com"})
        malformed = await client.post("/v1/auth/magic-link", json={"email": "not-an-address"})
        mail = transport.sent[0]
        url = next(line for line in mail.text.splitlines() if "login?token=" in line)
        token = url.split("token=", 1)[1].strip()
        exchanged = await client.post("/v1/auth/magic-link/exchange", json={"token": token})
        session_token = exchanged.json()["token"]
        as_staff = await client.get("/v1/admin/users", headers=auth(session_token))
        again = await client.post("/v1/auth/magic-link/exchange", json={"token": token})
        garbage = await client.post("/v1/auth/magic-link/exchange", json={"token": "x" * 40})
        # a second link that has expired
        await client.post("/v1/auth/magic-link", json={"email": "vesna@example.com"})
        stale_url = next(line for line in transport.sent[1].text.splitlines() if "token=" in line)
        stale_token = stale_url.split("token=", 1)[1].strip()
        async with app.state.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE staff_login_tokens SET expires_at = now() - interval '1 minute' "
                    "WHERE used_at IS NULL"
                )
            )
            await session.commit()
        expired = await client.post("/v1/auth/magic-link/exchange", json={"token": stale_token})
        log = await client.get("/v1/admin/email-log", params={"user_id": user_id}, headers=auth())

    assert requested.status_code == 202 and unknown.status_code == 202
    assert requested.json() == unknown.json()  # no account enumeration
    assert malformed.status_code == 422
    assert len(transport.sent) == 2 and mail.to == ["vesna@example.com"]
    assert mail.template == "magic_link" and url.startswith("http://admin.test/login?token=")
    assert "10 minut" in mail.text
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    assert body["user"] == {
        "id": user_id,
        "email": "vesna@example.com",
        "display_name": None,
        "role": "admin",
    }
    assert body["expires_at"] and len(session_token) >= 32
    assert as_staff.status_code == 200
    assert again.status_code == 401 and garbage.status_code == 401 and expired.status_code == 401
    assert again.json()["error"]["code"] == "unauthorized"
    entries = log.json()["items"]
    assert [e["template"] for e in entries] == ["magic_link", "magic_link"]
    assert all(e["status"] == "sent" and e["user_id"] == user_id for e in entries)
    tokens = await rows(
        app, "SELECT used_at IS NOT NULL AS used FROM staff_login_tokens ORDER BY id"
    )
    assert [t["used"] for t in tokens] == [True, False]
    audit = await rows(
        app,
        "SELECT action, actor FROM audit_log WHERE entity_type = 'staff_user' "
        "AND action LIKE 'auth.%' ORDER BY id",
    )
    assert [a["action"] for a in audit] == [
        "auth.magic_link_requested",
        "auth.login",
        "auth.magic_link_requested",
    ]
    assert {a["actor"] for a in audit} == {"vesna@example.com"}


async def test_staging_only_mails_the_allow_list(mail_env, transport):
    app = mail_env(
        app_env="staging",
        s3_access_key="minio",
        s3_secret_key="minio-secret",
        mail_allowlist=["@example.com"],
    )
    async with app.router.lifespan_context(app), make_client(app) as client:
        allowed = await client.post("/v1/orders", json=FORM)
        blocked = await client.post("/v1/orders", json=LEGAL)
        log = await client.get("/v1/admin/email-log", headers=auth())
    assert allowed.json()["email_status"] == "sent"
    assert blocked.json()["email_status"] == "suppressed"
    assert [m.to for m in transport.sent] == [["ana.novak@example.com"]]
    by_address = {e["to_email"]: e for e in log.json()["items"]}
    assert by_address["office@gradnja.me"]["status"] == "suppressed"
    assert by_address["office@gradnja.me"]["suppressed_reason"] == "not_allowlisted"
    assert by_address["office@gradnja.me"]["subject"]  # rendered, just not sent

    nothing = mail_env(app_env="staging", s3_access_key="k", s3_secret_key="s")
    async with nothing.router.lifespan_context(nothing), make_client(nothing) as client:
        created = await client.post(
            "/v1/orders", json={**FORM, "email": "someone.else@example.com"}
        )
    assert created.json()["email_status"] == "suppressed"
    (entry,) = await rows(
        nothing,
        "SELECT suppressed_reason FROM email_log WHERE to_email = 'someone.else@example.com'",
    )
    assert entry["suppressed_reason"] == "staging_allowlist_empty"


async def test_bounces_are_recorded_and_surface_on_the_order(mail_env):
    app = mail_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/orders", json=FORM)
        (order,) = await rows(
            app, "SELECT id FROM orders WHERE reference = :r", r=created.json()["reference"]
        )
        entry = (
            await client.get(
                "/v1/admin/email-log", params={"order_id": order["id"]}, headers=auth()
            )
        ).json()["items"][0]
        bounced = await client.post(
            f"/v1/admin/email-log/{entry['id']}/bounce",
            json={"reason": "550 5.1.1 mailbox does not exist"},
            headers=auth(),
        )
        twice = await client.post(
            f"/v1/admin/email-log/{entry['id']}/bounce", json={"reason": "again"}, headers=auth()
        )
        queue = await client.get("/v1/admin/orders", headers=auth())
        detail = await client.get(f"/v1/admin/orders/{order['id']}", headers=auth())
        missing = await client.post(
            "/v1/admin/email-log/999999/bounce", json={"reason": "x"}, headers=auth()
        )
    assert bounced.status_code == 200, bounced.text
    body = bounced.json()
    assert body["status"] == "bounced" and body["bounced_at"]
    assert body["bounce_reason"] == "550 5.1.1 mailbox does not exist"
    assert twice.status_code == 409 and missing.status_code == 404
    assert queue.json()["items"][0]["email_alerts"] == 1
    assert detail.json()["emails"][0]["status"] == "bounced"
    audit = await rows(
        app,
        "SELECT action, before, after, note FROM audit_log WHERE entity_type = 'email_log' "
        "ORDER BY id",
    )
    assert audit == [
        {
            "action": "email.bounce",
            "before": {"status": "sent"},
            "after": {"status": "bounced"},
            "note": "550 5.1.1 mailbox does not exist",
        }
    ]


async def test_transient_smtp_trouble_is_retried_then_failed(mail_env):
    app = mail_env(transport_override=Transport(fail=True))
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/orders", json=FORM)
        assert created.status_code == 201
        log = await client.get("/v1/admin/email-log", params={"status": "failed"}, headers=auth())
        jobs = await client.get("/v1/admin/jobs", params={"type": "send_email"}, headers=auth())
    assert created.json()["email_status"] == "failed"  # the order stands
    entry = log.json()["items"][0]
    assert entry["attempts"] == 3 and "451 provider busy" in entry["error"]
    job = jobs.json()["items"][0]
    assert job["status"] == "failed" and job["attempts"] == 3
    assert "451 provider busy" in job["error"]
