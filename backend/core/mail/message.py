"""The e-mail message: a small dataclass and its MIME form (text + HTML alternative)."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid, parseaddr


@dataclass
class EmailMessage:
    to: list[str]
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def sender_address(sender: str) -> str:
    """``"UrbanView <no-reply@urbanview.io>"`` -> ``no-reply@urbanview.io``."""
    return parseaddr(sender)[1] or sender


def new_message_id(sender: str) -> str:
    """A ``Message-ID`` on the sender's domain (the id we log when the provider reports none)."""
    address = sender_address(sender)
    domain = address.rsplit("@", 1)[-1] if "@" in address else "urbanview.local"
    return make_msgid(domain=domain)


def build_mime(message: EmailMessage, *, sender: str, message_id: str | None = None) -> MimeMessage:
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = ", ".join(message.to)
    mime["Subject"] = message.subject
    mime["Message-ID"] = message_id or new_message_id(sender)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    for name, value in message.headers.items():
        mime[name] = value
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    return mime


def plain_text_of(mime: MimeMessage) -> str:
    body = mime.get_body(preferencelist=("plain",))
    return body.get_content() if body is not None else ""


def html_of(mime: MimeMessage) -> str | None:
    body = mime.get_body(preferencelist=("html",))
    return body.get_content() if body is not None else None
