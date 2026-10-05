"""The e-mail templates (Jinja2): subject, plain text and HTML per kind, each written in both of
the app's languages. A message is rendered in one language (``LANGUAGES``): the one the app was
in when the order was placed or the sign-in link was asked for (product owner, 2026-10-02;
before, every message carried Montenegrin then English). Wording is provisional until the client
approves it.

Each template declares the context keys it needs; ``render`` refuses a context that lacks one
(``StrictUndefined``: a typo in a template or a missing fact fails in tests, not in a customer's
inbox). HTML templates auto-escape; text templates do not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
APP_NAME = "UrbanView"
# the app's languages (the public map's switch): English and Montenegrin
LANGUAGES: tuple[str, ...] = ("en", "me")
DEFAULT_LANGUAGE = "en"
HTML_LANG = {"en": "en", "me": "cnr-Latn"}  # as the site's <html lang>


def mail_language(*candidates: Any, default: str = DEFAULT_LANGUAGE) -> str:
    """The first candidate that is one of the app's languages, else the default (a request
    without a language, an order placed before orders kept theirs)."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate in LANGUAGES:
            return candidate
    return default if default in LANGUAGES else DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    name: str
    required: tuple[str, ...]
    description: str


TEMPLATES: dict[str, TemplateSpec] = {
    spec.name: spec
    for spec in (
        TemplateSpec(
            "payment_instructions",
            (
                "reference",
                "first_name",
                "location",
                "price_eur",
                "currency",
                "payment_title_en",
                "payment_title_me",
                "beneficiary",
                "beneficiary_address",
                "bank_name",
                "account_number",
                "iban",
                "swift",
                "amount_eur",
                "reference_to_quote",
                "note_en",
                "note_me",
                "turnaround_business_days",
                "expected_by",
                "status_url",
                "support_email",
            ),
            "Sent on order creation: reference, location, price, the bank details of the order's "
            "payment origin (domestic account, or IBAN and SWIFT), turnaround",
        ),
        TemplateSpec(
            "order_delivered",
            (
                "reference",
                "first_name",
                "location",
                "download_url",
                "expires_at",
                "status_url",
                "support_email",
            ),
            "Sent when the expert report is uploaded: signed download link and the support inbox",
        ),
        TemplateSpec(
            "magic_link",
            ("login_url", "expires_minutes", "email", "support_email"),
            "Staff login link for the admin panel: single use, short expiry",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    template: str
    subject: str
    text: str
    html: str


def _money(value: Any) -> str:
    return f"{float(value):.2f}"


def _date_short(value: Any) -> str:
    if isinstance(value, datetime | date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _datetime_utc(value: Any) -> str:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo is None else value.astimezone(UTC)
        return stamp.strftime("%d.%m.%Y %H:%M") + " UTC"
    return str(value)


def _autoescape(name: str | None) -> bool:
    return bool(name) and ".html" in name


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=_autoescape,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_env.filters.update(money=_money, date_short=_date_short, datetime_utc=_datetime_utc)


def render(
    template: str,
    context: Mapping[str, Any],
    *,
    app_name: str = APP_NAME,
    language: str = DEFAULT_LANGUAGE,
) -> RenderedEmail:
    """Subject, text and HTML for ``template`` in one language; ``ValueError`` on an unknown
    template or language, or a context missing a required key."""
    spec = TEMPLATES.get(template)
    if spec is None:
        raise ValueError(f"unknown e-mail template {template!r}")
    if language not in LANGUAGES:
        raise ValueError(f"unknown e-mail language {language!r} (have {', '.join(LANGUAGES)})")
    missing = [key for key in spec.required if key not in context]
    if missing:
        raise ValueError(f"template {template!r} needs {', '.join(missing)}")
    ctx = {"app_name": app_name, **context, "lang": language, "html_lang": HTML_LANG[language]}
    subject = _env.get_template(f"{template}.subject.j2").render(ctx).strip()
    text = _env.get_template(f"{template}.txt.j2").render(ctx)
    html = _env.get_template(f"{template}.html.j2").render(ctx)
    return RenderedEmail(template=template, subject=subject, text=text, html=html)
