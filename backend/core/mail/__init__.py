"""Transactional e-mail.

- ``templates``: the three Jinja2 templates (subject, plain text, HTML; Montenegrin + English),
  rendered against a context that the ``send_email`` job builds from the order / staff user at
  send time, so no personal data travels through the job queue;
- ``message`` / ``smtp``: the MIME message and the SMTP transport (STARTTLS or implicit TLS,
  provider-agnostic: Postmark, SES, Resend, Mailpit in dev);
- ``policy``: what may be sent where (no ``SMTP_HOST`` → suppressed; staging → allow-list only);
- ``repository``: the ``email_log`` / order / staff-user access the job needs.

Nothing here sends on its own: the API queues a ``send_email`` job per ``email_log`` row
(``api.services.email.EmailService``) and the worker task (``jobs.tasks.email``) does the rest,
with retries on transient provider errors.
"""

from core.mail.message import EmailMessage, build_mime, new_message_id, plain_text_of
from core.mail.policy import SendDecision, decide
from core.mail.smtp import MailPermanentError, MailTransientError, SendReceipt, SmtpTransport
from core.mail.templates import TEMPLATES, RenderedEmail, render

__all__ = [
    "TEMPLATES",
    "EmailMessage",
    "MailPermanentError",
    "MailTransientError",
    "RenderedEmail",
    "SendDecision",
    "SendReceipt",
    "SmtpTransport",
    "build_mime",
    "decide",
    "new_message_id",
    "plain_text_of",
    "render",
]
