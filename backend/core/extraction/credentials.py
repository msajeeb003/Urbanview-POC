"""Which Anthropic API key a job uses, and where it came from.

Precedence: the server environment (``ANTHROPIC_API_KEY`` of the process) first, without
touching the database; else the key saved in the admin console (``app_secrets``, decrypted with
``SECRETS_ENCRYPTION_KEY``, ``core.app_secrets``); else none. :class:`ResolvedKey` carries the
key for the SDK client (never in its ``repr``) and a ``fingerprint`` (source, last four
characters, when a console key was saved) that names the key without revealing it: the
connection test records it and the console compares it with the key it sees now.

The developer CLIs (``python -m core.extraction eval``, the corpus harness, ``python -m
core.market``) stay env-only; the workers (extraction, market imports, connection test) resolve
the key per job, so a key saved in the console is used without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from core.app_secrets import (
    ANTHROPIC_API_KEY,
    SecretsUnavailable,
    SecretUnreadable,
    StoredSecret,
    decrypt,
    load_secret_with,
)
from core.extraction.llm import ModelNotConfigured

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KeySource = Literal["server_env", "console", "none"]
KeyProblem = Literal["encryption_key_missing", "unreadable"]

NO_KEY_MESSAGE = (
    "No Anthropic API key: set ANTHROPIC_API_KEY on the server or paste a key in the admin "
    "console (AI extraction)"
)
ENCRYPTION_KEY_MISSING_MESSAGE = (
    "An API key is saved in the admin console but SECRETS_ENCRYPTION_KEY is not set on this server"
)
UNREADABLE_MESSAGE = (
    "The API key saved in the admin console cannot be decrypted with this server's "
    "SECRETS_ENCRYPTION_KEY: paste it again"
)


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    source: KeySource
    last4: str | None = None
    set_at: datetime | None = None  # console only
    problem: KeyProblem | None = None
    api_key: str | None = field(default=None, repr=False)

    @property
    def fingerprint(self) -> dict[str, str | None]:
        """Names the key without revealing it: ``{source, last4, set_at}`` (ISO or None)."""
        return {
            "source": self.source,
            "last4": self.last4,
            "set_at": self.set_at.isoformat() if self.set_at is not None else None,
        }


def env_key(settings: Any) -> str | None:
    """``ANTHROPIC_API_KEY`` of this process, trimmed; None when unset or blank."""
    secret = getattr(settings, "anthropic_api_key", None)
    return (secret.get_secret_value() if secret is not None else "").strip() or None


def resolve_from(settings: Any, secret: StoredSecret | None) -> ResolvedKey:
    """The key to use given the stored console secret (pure apart from decryption)."""
    env = env_key(settings)
    if env is not None:
        return ResolvedKey("server_env", env[-4:], api_key=env)
    if secret is None:
        return ResolvedKey("none")
    try:
        value = decrypt(settings, secret.ciphertext)
    except SecretsUnavailable:
        return ResolvedKey("console", secret.last4, secret.set_at, problem="encryption_key_missing")
    except SecretUnreadable:
        return ResolvedKey("console", secret.last4, secret.set_at, problem="unreadable")
    return ResolvedKey("console", secret.last4, secret.set_at, api_key=value)


async def resolve_anthropic_key(
    session_factory: async_sessionmaker[AsyncSession], settings: Any, *, municipality_id: str
) -> ResolvedKey:
    """The server environment's key without touching the database, else the console's."""
    if env_key(settings) is not None:
        return resolve_from(settings, None)
    secret = await load_secret_with(
        session_factory, municipality_id=municipality_id, name=ANTHROPIC_API_KEY
    )
    return resolve_from(settings, secret)


def missing_key_message(resolved: ResolvedKey) -> str:
    """Why no key can be used (the job error, the console's checklist detail)."""
    if resolved.problem == "encryption_key_missing":
        return ENCRYPTION_KEY_MISSING_MESSAGE
    if resolved.problem == "unreadable":
        return UNREADABLE_MESSAGE
    return NO_KEY_MESSAGE


def require_key(resolved: ResolvedKey) -> str:
    """The key, or ModelNotConfigured (not retried) saying why there is none."""
    if resolved.api_key is None:
        raise ModelNotConfigured(missing_key_message(resolved))
    return resolved.api_key
