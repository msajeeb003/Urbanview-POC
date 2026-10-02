"""Transactional e-mail without a database or an SMTP server: every template rendered against
fixture data in each of the app's languages (required fields present in subject, text and HTML;
one language per message; HTML escaped), the language a message takes (the request's, else the
order's, else the default), the sending policy, the MIME message, the provider-id parsing, and
the send_email job body on the in-memory repository (sent with a provider id, retried on
transient trouble then failed, permanent failures not retried, suppressed without SMTP_HOST, the
magic-link token minted by the job)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from api.services.order_mail import (
    OrderFacts,
    magic_link_context,
    order_delivered_context,
    order_status_url,
    payment_instructions_context,
)
from core.auth import hash_token
from core.mail import (
    LANGUAGES,
    TEMPLATES,
    EmailMessage,
    MailPermanentError,
    MailTransientError,
    SendReceipt,
    build_mime,
    decide,
    mail_language,
    new_message_id,
    plain_text_of,
    render,
)
from core.mail.message import html_of
from core.mail.repository import MemoryEmailRepository, OrderMailFacts, StaffUserFacts
from core.mail.smtp import provider_message_id_from
from core.payments import BankTransferProvider
from jobs.base import MemoryJobStore, run_job_async
from jobs.tasks.email import deliver
from tests.helpers import make_settings

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=UTC)
FACTS = OrderFacts(
    reference="UV-PODI-1042-260925-01",
    first_name="Ana",
    parcel_label="KO Podgorica I, parcela 1042",
    document_name="DUP Centar – Zona C2",
    price_eur=200.0,
    turnaround_business_days=5,
    expected_by=date(2026, 10, 2),
    status_url=order_status_url("http://localhost:3000/", "UV-PODI-1042-260925-01"),
    support_email="support@urbanview.io",
)
PROVIDER = BankTransferProvider(
    beneficiary="UrbanView d.o.o.", iban="ME12 3456 7890", bank_name="CKB", swift="CKBCMEPG"
)
INSTRUCTIONS = PROVIDER.instructions(reference=FACTS.reference, amount_eur=200.0)


def test_status_url_helper():
    assert FACTS.status_url == "http://localhost:3000/orders/UV-PODI-1042-260925-01"


# what only one of the two languages says, per template: a message must hold its own language's
# wording and none of the other's
ONLY = {
    "payment_instructions": {
        "en": ("Dear Ana", "5 business days", "Beneficiary", "Payment reference", "Questions"),
        "me": ("Poštovani/a Ana", "5 radnih dana", "Primalac", "Poziv na broj", "Pitanja"),
    },
    "order_delivered": {
        "en": ("Dear Ana", "is ready", "Track your order"),
        "me": ("Poštovani/a Ana", "je gotova", "Status narudžbe"),
    },
    "magic_link": {
        "en": ("Sign in", "works once", "Questions"),
        "me": ("Prijav", "samo jednom", "Pitanja"),
    },
}


def one_language(mail, template: str, language: str) -> None:
    other = "me" if language == "en" else "en"
    for needle in ONLY[template][language]:
        assert needle in mail.text and needle in mail.html, (language, needle)
    for needle in ONLY[template][other]:
        assert needle not in mail.text and needle not in mail.html, (language, needle)
    assert "\n---\n" not in mail.text and "<hr" not in mail.html  # no second half


def test_payment_instructions_template_carries_every_required_fact():
    context = payment_instructions_context(FACTS, INSTRUCTIONS)
    subjects = {
        "en": "UrbanView order UV-PODI-1042-260925-01: payment instructions",
        "me": "UrbanView narudžba UV-PODI-1042-260925-01: uputstvo za plaćanje",
    }
    for language, subject in subjects.items():
        mail = render("payment_instructions", context, language=language)
        assert mail.subject == subject
        for needle in (
            "UV-PODI-1042-260925-01",  # order reference (also the payment reference)
            "KO Podgorica I, parcela 1042 (DUP Centar – Zona C2)",  # location ordered
            "200.00 EUR",  # price
            "UrbanView d.o.o.",  # beneficiary
            "ME12 3456 7890",  # IBAN
            "CKB",
            "CKBCMEPG",
            "02.10.2026",  # expected by
            FACTS.status_url,
            "support@urbanview.io",
        ):
            assert needle in mail.text, (language, needle)
            assert needle in mail.html, (language, needle)
        one_language(mail, "payment_instructions", language)
        assert "<" not in mail.text.replace("<no-reply", "")
        assert "<!doctype html>" in mail.html.lower()
        assert f'<html lang="{"cnr-Latn" if language == "me" else "en"}">' in mail.html
        assert mail.text.rstrip().endswith("UrbanView")
    # English unless a language is asked for
    assert render("payment_instructions", context).subject == subjects["en"]


def test_order_delivered_template_has_the_link_and_the_support_inbox():
    expires = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    context = order_delivered_context(FACTS, "https://files.example/report.pdf?sig=abc", expires)
    subjects = {
        "en": "UrbanView order UV-PODI-1042-260925-01: your report is ready",
        "me": "UrbanView narudžba UV-PODI-1042-260925-01: izvještaj je spreman",
    }
    for language, subject in subjects.items():
        mail = render("order_delivered", context, language=language)
        assert mail.subject == subject
        for needle in (
            "UV-PODI-1042-260925-01",
            "KO Podgorica I, parcela 1042",
            "https://files.example/report.pdf?sig=abc",
            "09.10.2026 14:00 UTC",
            "support@urbanview.io",
        ):
            assert needle in mail.text, (language, needle)
            assert needle in mail.html, (language, needle)
        one_language(mail, "order_delivered", language)


def test_magic_link_template_says_single_use_and_expiry():
    context = magic_link_context(
        email="vesna@example.com",
        login_url="http://localhost:3001/login?token=abc123def456",
        expires_minutes=15,
        support_email="support@urbanview.io",
    )
    subjects = {
        "en": "UrbanView admin: your sign-in link",
        "me": "UrbanView admin: link za prijavu",
    }
    for language, subject in subjects.items():
        mail = render("magic_link", context, language=language)
        assert mail.subject == subject
        for needle in (
            "http://localhost:3001/login?token=abc123def456",
            "15 minut",
            "vesna@example.com",
            "support@urbanview.io",
        ):
            assert needle in mail.text and needle in mail.html, (language, needle)
        one_language(mail, "magic_link", language)
        # the link stands on a line of its own in the text part (the sign-in tests read it there)
        assert "http://localhost:3001/login?token=abc123def456" in mail.text.splitlines()


def test_html_is_escaped_and_contexts_are_checked():
    hostile = replace(FACTS, first_name="<b>Ana</b>")
    mail = render("payment_instructions", payment_instructions_context(hostile, INSTRUCTIONS))
    assert "&lt;b&gt;Ana&lt;/b&gt;" in mail.html and "<b>Ana</b>" in mail.text
    with pytest.raises(ValueError, match="needs"):
        render("magic_link", {"login_url": "x"})
    with pytest.raises(ValueError, match="unknown"):
        render("newsletter", {})
    with pytest.raises(ValueError, match="unknown e-mail language 'de'"):
        render("magic_link", {"login_url": "x"}, language="de")
    assert set(TEMPLATES) == {"payment_instructions", "order_delivered", "magic_link"}
    assert LANGUAGES == ("en", "me")


def test_the_language_of_a_message():
    # the first one that is a language of the app, else the default
    assert mail_language("me", "en") == "me"
    assert mail_language(None, "me") == "me"
    assert mail_language(None, None) == "en"
    assert mail_language(None, None, default="me") == "me"
    assert mail_language("de", "", 7, default="me") == "me"
    assert mail_language("de", default="fr") == "en"


def test_sending_policy():
    assert decide(app_env="dev", smtp_host=None, to="a@b.me", allowlist=[]).reason == "no_smtp_host"
    assert decide(app_env="dev", smtp_host="mailpit", to="a@b.me", allowlist=[]).send is True
    assert decide(app_env="prod", smtp_host="smtp.x", to="a@b.me", allowlist=[]).send is True
    staging_empty = decide(app_env="staging", smtp_host="smtp.x", to="a@b.me", allowlist=[])
    assert staging_empty.reason == "staging_allowlist_empty"
    allow = ["ops@urbanview.io", "@example.com"]
    assert decide(app_env="staging", smtp_host="smtp.x", to="Ana@Example.com", allowlist=allow).send
    assert decide(
        app_env="staging", smtp_host="smtp.x", to="ops@urbanview.io", allowlist=allow
    ).send
    blocked = decide(app_env="staging", smtp_host="smtp.x", to="ana@gradnja.me", allowlist=allow)
    assert blocked.reason == "not_allowlisted"
    # a non-empty allow-list is enforced in every environment (prod dry runs)
    assert decide(app_env="prod", smtp_host="smtp.x", to="x@y.me", allowlist=allow).send is False


def test_mime_message_and_provider_ids():
    message = EmailMessage(
        to=["ana@example.com"],
        subject="Hello",
        text="plain body",
        html="<p>html body</p>",
        reply_to="support@urbanview.io",
        headers={"X-UrbanView-Template": "magic_link"},
    )
    message_id = new_message_id("UrbanView <no-reply@urbanview.io>")
    assert message_id.startswith("<") and message_id.endswith("@urbanview.io>")
    mime = build_mime(message, sender="UrbanView <no-reply@urbanview.io>", message_id=message_id)
    assert mime["From"] == "UrbanView <no-reply@urbanview.io>" and mime["To"] == "ana@example.com"
    assert mime["Reply-To"] == "support@urbanview.io" and mime["Message-ID"] == message_id
    assert mime["X-UrbanView-Template"] == "magic_link"
    assert mime.get_content_type() == "multipart/alternative"
    assert plain_text_of(mime).strip() == "plain body" and "<p>html body</p>" in html_of(mime)
    assert provider_message_id_from("2.0.0 Ok 0100019a2b3c4d5e-abcd-1234", "<x@y>") == (
        "0100019a2b3c4d5e-abcd-1234"
    )
    assert provider_message_id_from("2.0.0 Ok: queued as 4Xy1Z2AbCd9", "<x@y>") == "4Xy1Z2AbCd9"
    assert provider_message_id_from("OK", "<our-id@urbanview.io>") == "our-id@urbanview.io"


# --- the job body --------------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, *, fail_times: int = 0, permanent: bool = False) -> None:
        self.sent = []
        self.fail_times = fail_times
        self.permanent = permanent

    def send(self, mime):
        if self.permanent:
            raise MailPermanentError("550 mailbox unavailable")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MailTransientError("451 try again later")
        self.sent.append(mime)
        return SendReceipt(provider_message_id="prov-123", response="250 Ok prov-123")


class FakeStorage:
    def presigned_get_url(self, key, expires_in=900, *, content_type=None, inline=False):
        return f"https://minio.test/{key}?X-Amz-Signature=sig"


def settings_with(**overrides):
    base = {
        "smtp_host": "smtp.test",
        "order_bank_iban": "ME12 3456 7890",
        "order_bank_beneficiary": "UrbanView d.o.o.",
        "admin_base_url": "http://admin.test",
    }
    return make_settings(**{**base, **overrides})


def repository() -> MemoryEmailRepository:
    repo = MemoryEmailRepository()
    repo.orders[7] = OrderMailFacts(
        id=7,
        reference="UV-PODI-1042-260925-01",
        email="ana@example.com",
        first_name="Ana",
        parcel_label="KO Podgorica I, parcela 1042",
        document_name="DUP Centar – Zona C2",
        price_eur=200.0,
        currency="EUR",
        turnaround_business_days=5,
        expected_by=date(2026, 10, 2),
        report_key="podgorica/uploads/expert_report/abc/report.pdf",
    )
    repo.users[3] = StaffUserFacts(3, "vesna@example.com", "Vesna", "reviewer", True)
    return repo


async def run(job_store, job_id, repo, transport, settings, *, max_attempts=3):
    return await run_job_async(
        job_store,
        job_id,
        "podgorica",
        lambda job: deliver(job, repo, transport, settings, FakeStorage(), clock=lambda: NOW),
        retry_base_seconds=1,
    )


async def test_payment_instructions_are_sent_and_logged_with_the_provider_id():
    repo, transport, settings = repository(), FakeTransport(), settings_with()
    log_id = repo.add_log("payment_instructions", "ana@example.com", order_id=7)
    store = MemoryJobStore()
    job_id = store.add(type="send_email", payload={"email_log_id": log_id})
    outcome = await run(store, job_id, repo, transport, settings)
    assert outcome.status == "succeeded"
    assert outcome.result == {
        "email_log_id": log_id,
        "status": "sent",
        "template": "payment_instructions",
        "language": "en",
        "provider_message_id": "prov-123",
    }
    row = repo.logs[log_id]
    assert (row["status"], row["attempts"], row["provider_message_id"]) == ("sent", 1, "prov-123")
    assert row["subject"] == "UrbanView order UV-PODI-1042-260925-01: payment instructions"
    assert "body" not in row and "text" not in row  # bodies are never stored
    mime = transport.sent[0]
    assert mime["To"] == "ana@example.com" and mime["Reply-To"] == "support@urbanview.io"
    assert "ME12 3456 7890" in plain_text_of(mime)
    assert "UV-PODI-1042-260925-01" in plain_text_of(mime)
    # a re-delivered message for a sent row does nothing
    again = await run(
        store,
        store.add(type="send_email", payload={"email_log_id": log_id}),
        repo,
        transport,
        settings,
    )
    assert again.result["skipped"] is True and len(transport.sent) == 1


async def test_a_message_is_written_in_the_orders_or_the_requests_language():
    """The order's language (the map's when it was placed) for its e-mails, the request's for a
    sign-in link, the configured default when neither says."""
    repo, transport = repository(), FakeTransport()
    repo.orders[8] = replace(repo.orders[7], id=8, language="me")
    store = MemoryJobStore()

    async def send(template, payload, settings, **target):
        log_id = repo.add_log(template, "x@example.com", **target)
        job_id = store.add(type="send_email", payload={"email_log_id": log_id, **payload})
        outcome = await run(store, job_id, repo, transport, settings)
        assert outcome.status == "succeeded", outcome.error
        return outcome.result["language"], repo.logs[log_id]["subject"], transport.sent[-1]

    english = settings_with()
    # an order placed in Montenegrin: both of its e-mails are Montenegrin
    language, subject, mime = await send("payment_instructions", {}, english, order_id=8)
    assert language == "me" and subject.endswith("uputstvo za plaćanje")
    assert "Poštovani/a Ana" in plain_text_of(mime) and "Dear Ana" not in plain_text_of(mime)
    language, subject, _ = await send("order_delivered", {}, english, order_id=8)
    assert language == "me" and subject.endswith("izvještaj je spreman")
    # an order that kept no language (placed before orders did): the default
    language, subject, _ = await send("order_delivered", {}, english, order_id=7)
    assert language == "en" and subject.endswith("your report is ready")
    language, subject, _ = await send(
        "order_delivered", {}, settings_with(mail_default_language="me"), order_id=7
    )
    assert language == "me" and subject.endswith("izvještaj je spreman")
    # a sign-in link: the language the console was in travels in the job's payload
    language, subject, mime = await send("magic_link", {"language": "me"}, english, user_id=3)
    assert language == "me" and subject == "UrbanView admin: link za prijavu"
    assert "Prijava na UrbanView admin panel" in plain_text_of(mime)
    language, subject, _ = await send("magic_link", {}, english, user_id=3)
    assert language == "en" and subject == "UrbanView admin: your sign-in link"
    # a payload language that is none of the app's is ignored
    language, _, _ = await send("magic_link", {"language": "de"}, english, user_id=3)
    assert language == "en"


async def test_transient_provider_errors_are_retried_then_failed():
    repo, settings = repository(), settings_with()
    log_id = repo.add_log("order_delivered", "ana@example.com", order_id=7)
    store = MemoryJobStore()
    job_id = store.add(type="send_email", payload={"email_log_id": log_id}, max_attempts=3)
    transport = FakeTransport(fail_times=2)
    first = await run(store, job_id, repo, transport, settings)
    second = await run(store, job_id, repo, transport, settings)
    third = await run(store, job_id, repo, transport, settings)
    assert [o.status for o in (first, second, third)] == ["retrying", "retrying", "succeeded"]
    row = repo.logs[log_id]
    assert row["status"] == "sent" and row["attempts"] == 3 and row["error"] is None
    assert "report.pdf?X-Amz-Signature=sig" in plain_text_of(transport.sent[0])

    exhausted = repo.add_log("order_delivered", "ana@example.com", order_id=7)
    down = FakeTransport(fail_times=99)
    job_id = store.add(type="send_email", payload={"email_log_id": exhausted}, max_attempts=3)
    outcomes = [await run(store, job_id, repo, down, settings) for _ in range(3)]
    assert [o.status for o in outcomes] == ["retrying", "retrying", "failed"]
    row = repo.logs[exhausted]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert "451 try again later" in row["error"]


async def test_permanent_failures_and_suppression():
    repo = repository()
    store = MemoryJobStore()
    bounced = repo.add_log("payment_instructions", "ana@example.com", order_id=7)
    job_id = store.add(type="send_email", payload={"email_log_id": bounced})
    outcome = await run(store, job_id, repo, FakeTransport(permanent=True), settings_with())
    assert outcome.status == "failed" and "550 mailbox unavailable" in outcome.error
    assert repo.logs[bounced]["status"] == "failed" and repo.logs[bounced]["attempts"] == 1

    quiet = repo.add_log("payment_instructions", "ana@example.com", order_id=7)
    transport = FakeTransport()
    job_id = store.add(type="send_email", payload={"email_log_id": quiet})
    outcome = await run(store, job_id, repo, transport, settings_with(smtp_host=None))
    assert outcome.status == "succeeded" and outcome.result["status"] == "suppressed"
    assert repo.logs[quiet]["status"] == "suppressed"
    assert repo.logs[quiet]["suppressed_reason"] == "no_smtp_host" and not transport.sent

    missing = repo.add_log("order_delivered", "x@example.com", order_id=999)
    job_id = store.add(type="send_email", payload={"email_log_id": missing})
    outcome = await run(store, job_id, repo, transport, settings_with())
    assert outcome.status == "failed" and "no longer exists" in outcome.error


async def test_magic_link_job_mints_a_single_use_token():
    repo, transport = repository(), FakeTransport()
    settings = settings_with(magic_link_expires_seconds=600)
    log_id = repo.add_log("magic_link", "vesna@example.com", user_id=3)
    store = MemoryJobStore()
    job_id = store.add(type="send_email", payload={"email_log_id": log_id})
    outcome = await run(store, job_id, repo, transport, settings)
    assert outcome.status == "succeeded" and repo.logs[log_id]["status"] == "sent"
    text = plain_text_of(transport.sent[0])
    url = next(
        line for line in text.splitlines() if line.startswith("http://admin.test/login?token=")
    )
    token = url.split("token=", 1)[1]
    assert len(token) >= 32
    assert len(repo.tokens) == 1
    assert repo.tokens[0]["token_hash"] == hash_token(token) and repo.tokens[0]["user_id"] == 3
    assert repo.tokens[0]["expires_at"] == NOW + timedelta(seconds=600)
    assert "10 minut" in text and repo.tokens[0]["email_log_id"] == log_id

    inactive = repo.add_log("magic_link", "gone@example.com", user_id=8)
    repo.users[8] = StaffUserFacts(8, "gone@example.com", None, "admin", False)
    outcome = await run(
        store,
        store.add(type="send_email", payload={"email_log_id": inactive}),
        repo,
        transport,
        settings,
    )
    assert outcome.status == "failed" and "inactive" in outcome.error and len(transport.sent) == 1
