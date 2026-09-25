"""Who may receive mail from which environment.

- no ``SMTP_HOST``: nothing is sent, the log row says ``suppressed`` (``no_smtp_host``);
- ``APP_ENV=staging``: only allow-listed addresses (``MAIL_ALLOWLIST``: addresses or
  ``@domain`` entries); an empty list means nothing goes out, so staging can never mail a real
  customer;
- any environment with a non-empty ``MAIL_ALLOWLIST`` enforces it (a sandbox for prod dry runs);
- otherwise (dev with Mailpit, prod) every address is sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SendDecision:
    send: bool
    reason: str | None = None


def allowlisted(address: str, allowlist: Sequence[str]) -> bool:
    address = address.strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for entry in allowlist:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if domain == entry[1:]:
                return True
        elif entry == address:
            return True
    return False


def decide(
    *, app_env: str, smtp_host: str | None, to: str, allowlist: Sequence[str]
) -> SendDecision:
    if not smtp_host:
        return SendDecision(False, "no_smtp_host")
    entries = [entry for entry in allowlist if entry.strip()]
    if app_env == "staging" or entries:
        if not entries:
            return SendDecision(False, "staging_allowlist_empty")
        if not allowlisted(to, entries):
            return SendDecision(False, "not_allowlisted")
    return SendDecision(True, None)
