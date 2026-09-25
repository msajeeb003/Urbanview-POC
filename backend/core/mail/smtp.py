"""SMTP transport: one connection per message, STARTTLS (587) or implicit TLS (465), the
provider's DATA reply captured for the log.

Errors are classified for the job queue: connection trouble, timeouts and 4xx replies are
transient (retried with backoff); authentication failures, 5xx replies and refused recipients
are permanent (the log row says why, nobody retries a bounce-to-be).
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses, parseaddr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings

log = logging.getLogger("urbanview.mail")


class MailTransientError(Exception):
    """The provider may accept the message on a later attempt."""


class MailPermanentError(Exception):
    """Sending this message will not succeed; configuration or recipient problem."""


@dataclass(frozen=True, slots=True)
class SendReceipt:
    provider_message_id: str
    response: str


_ID_IN_REPLY = re.compile(r"(?:queued as|id=|Ok|OK)\s+([A-Za-z0-9._@<>=+/-]{8,})")


def provider_message_id_from(reply: str, fallback: str) -> str:
    """The id a provider reports in its 250 reply (SES: ``250 Ok 0100…``, Postfix: ``queued as
    ABC123``); our ``Message-ID`` when the reply carries none."""
    match = _ID_IN_REPLY.search(reply or "")
    return match.group(1).strip("<>") if match else fallback.strip("<>")


class SmtpTransport:
    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = float(timeout)

    @classmethod
    def from_settings(cls, settings: Settings) -> SmtpTransport:
        if not settings.smtp_host:
            raise MailPermanentError("SMTP_HOST is not configured")
        return cls(
            settings.smtp_host,
            settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            use_tls=settings.smtp_use_tls,
            use_ssl=settings.smtp_use_ssl,
            timeout=settings.smtp_timeout_seconds,
        )

    def _connect(self) -> smtplib.SMTP:
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, mime: MimeMessage) -> SendReceipt:
        sender = parseaddr(mime["From"])[1]
        recipients = [address for _, address in getaddresses(mime.get_all("To", [])) if address]
        if not sender or not recipients:
            raise MailPermanentError("the message needs a From and at least one To address")
        message_id = str(mime["Message-ID"] or "")
        try:
            with self._connect() as smtp:
                if self.use_tls and not self.use_ssl:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                code, reply = smtp.mail(sender)
                _check(code, reply, "MAIL FROM")
                for address in recipients:
                    code, reply = smtp.rcpt(address)
                    if code not in (250, 251):
                        _check(code, reply, f"recipient {address}")
                code, reply = smtp.data(mime.as_bytes())
                _check(code, reply, "DATA")
        except smtplib.SMTPAuthenticationError as exc:
            raise MailPermanentError(f"SMTP authentication failed: {exc}") from exc
        except smtplib.SMTPResponseException as exc:
            text = _text(exc.smtp_error)
            if 400 <= exc.smtp_code < 500:
                raise MailTransientError(f"SMTP {exc.smtp_code} {text}") from exc
            raise MailPermanentError(f"SMTP {exc.smtp_code} {text}") from exc
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as exc:
            raise MailTransientError(f"{type(exc).__name__}: {exc}") from exc
        response = _text(reply)
        log.info("email sent", extra={"smtp_reply": response, "message_id": message_id})
        return SendReceipt(
            provider_message_id=provider_message_id_from(response, message_id), response=response
        )


def _text(reply: bytes | str | None) -> str:
    if reply is None:
        return ""
    return reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)


def _check(code: int, reply: bytes | str, step: str) -> None:
    if code == 250:
        return
    if 400 <= code < 500:
        raise MailTransientError(f"{step}: SMTP {code} {_text(reply)}")
    raise MailPermanentError(f"{step}: SMTP {code} {_text(reply)}")
