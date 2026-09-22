"""Transactional email over SMTP (payment instructions, report delivery, magic links).

Synchronous; call from Celery tasks. With no ``SMTP_HOST`` configured (dev/tests) messages are
logged instead of sent, so flows can be exercised without a mail provider.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as _EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings

log = logging.getLogger("urbanview.mail")


@dataclass
class EmailMessage:
    to: list[str]
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.smtp_host)

    def send(self, message: EmailMessage) -> bool:
        """True when handed to the SMTP server, False when suppressed (no SMTP_HOST)."""
        msg = _EmailMessage()
        msg["From"] = self.settings.smtp_from
        msg["To"] = ", ".join(message.to)
        msg["Subject"] = message.subject
        if message.reply_to:
            msg["Reply-To"] = message.reply_to
        for name, value in message.headers.items():
            msg[name] = value
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")

        if not self.enabled:
            log.info(
                "email suppressed (SMTP_HOST not set)",
                extra={"to": message.to, "subject": message.subject},
            )
            return False

        s = self.settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_use_tls:
                smtp.starttls()
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(msg)
        log.info("email sent", extra={"to": message.to, "subject": message.subject})
        return True
