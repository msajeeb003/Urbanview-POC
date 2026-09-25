"""Send one template with fixture data straight over the configured SMTP provider, for the
deliverability check (DKIM / SPF / spam placement in Gmail and Outlook)::

    python -m core.mail.testsend --template payment_instructions --to you@example.com

Bypasses the queue and the log on purpose; prints the provider's reply and the message id.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta

from api.services.order_mail import (
    OrderFacts,
    magic_link_context,
    order_delivered_context,
    order_status_url,
    payment_instructions_context,
)
from core.config import get_settings
from core.mail import EmailMessage, SmtpTransport, build_mime, decide, new_message_id, render
from core.payments import BankTransferProvider


def fixture_context(template: str, settings, to: str) -> dict:
    facts = OrderFacts(
        reference="UV-PODI-1042-260925-01",
        first_name="Ana",
        parcel_label="KO Podgorica I, parcela 1042",
        document_name="DUP Centar – Zona C2",
        price_eur=200.0,
        turnaround_business_days=settings.order_turnaround_business_days,
        expected_by=date.today() + timedelta(days=7),
        status_url=order_status_url(settings.order_public_base_url, "UV-PODI-1042-260925-01"),
        support_email=settings.order_support_email,
    )
    if template == "payment_instructions":
        provider = BankTransferProvider(
            beneficiary=settings.order_bank_beneficiary,
            iban=settings.order_bank_iban,
            bank_name=settings.order_bank_name,
            swift=settings.order_bank_swift,
        )
        return payment_instructions_context(
            facts, provider.instructions(reference=facts.reference, amount_eur=200.0)
        )
    if template == "order_delivered":
        return order_delivered_context(
            facts,
            "https://example.com/report.pdf?signature=test",
            datetime.now(UTC) + timedelta(days=7),
        )
    return magic_link_context(
        email=to,
        login_url=f"{settings.admin_base_url}/login?token=test-token-not-valid",
        expires_minutes=15,
        support_email=settings.order_support_email,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("::")[0].strip())
    parser.add_argument(
        "--template",
        choices=("payment_instructions", "order_delivered", "magic_link"),
        default="payment_instructions",
    )
    parser.add_argument("--to", required=True, help="recipient address")
    args = parser.parse_args(argv)
    settings = get_settings()
    decision = decide(
        app_env=settings.app_env.value,
        smtp_host=settings.smtp_host,
        to=args.to,
        allowlist=settings.mail_allowlist,
    )
    if not decision.send:
        print(f"refused by the sending policy: {decision.reason}", file=sys.stderr)
        return 2
    rendered = render(args.template, fixture_context(args.template, settings, args.to))
    mime = build_mime(
        EmailMessage(
            to=[args.to],
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            reply_to=settings.mail_reply_to or settings.order_support_email,
            headers={"X-UrbanView-Template": args.template},
        ),
        sender=settings.smtp_from,
        message_id=new_message_id(settings.smtp_from),
    )
    receipt = SmtpTransport.from_settings(settings).send(mime)
    print(f"sent {args.template} to {args.to}")
    print(f"provider message id: {receipt.provider_message_id}")
    print(f"provider reply: {receipt.response}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
