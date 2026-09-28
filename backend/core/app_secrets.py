"""Secrets saved from the admin console (table ``app_secrets``, migration 0028).

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) under ``SECRETS_ENCRYPTION_KEY``
before they reach the database; the API encrypts on save, the worker decrypts when a job needs
the value. Only ``last4`` (the last four characters) ever leaves the process: plaintext values
never reach responses, logs, audit rows, exceptions or job payloads.

Without ``SECRETS_ENCRYPTION_KEY`` nothing can be stored (``SecretsUnavailable``); a value
stored under another key cannot be read (``SecretUnreadable``) and has to be saved again.

Imports stay light: ``cryptography`` is imported inside the functions that need it.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ANTHROPIC_API_KEY = "anthropic_api_key"
SECRET_NAMES: tuple[str, ...] = (ANTHROPIC_API_KEY,)
ANTHROPIC_KEY_PREFIX = "sk-ant-"
ANTHROPIC_ADMIN_PREFIX = "sk-ant-admin"
ANTHROPIC_KEY_MIN_LENGTH = 40
ANTHROPIC_KEY_MAX_LENGTH = 256
FERNET_HINT = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)

# §1 of the AI settings contract: the exact messages the API and the console show
KEY_EMPTY = "Paste the API key."
KEY_WHITESPACE = "The key must not contain spaces or line breaks."
KEY_PREFIX = "An Anthropic API key starts with sk-ant-."
KEY_ADMIN = "This is an Admin API key: paste a regular API key."
KEY_TOO_SHORT = "The key is too short to be an Anthropic API key."
KEY_TOO_LONG = "The key is too long to be an Anthropic API key."
KEY_CHARACTERS = "The key contains characters an Anthropic API key does not use."

_WHITESPACE = re.compile(r"\s")
_KEY_CHARACTERS = re.compile(r"[A-Za-z0-9_-]+")


class SecretsUnavailable(Exception):
    """No SECRETS_ENCRYPTION_KEY is configured: nothing can be encrypted or decrypted."""


class SecretUnreadable(Exception):
    """The stored value was encrypted under another SECRETS_ENCRYPTION_KEY."""


@dataclass(frozen=True, slots=True)
class StoredSecret:
    id: int
    name: str
    last4: str
    set_by: str
    set_by_user_id: int | None
    set_at: datetime
    ciphertext: str = field(repr=False)


def check_fernet_key(value: str) -> None:
    """Raise ValueError unless ``value`` is a Fernet key; the message never includes the value."""
    message = (
        f"SECRETS_ENCRYPTION_KEY must be a Fernet key (32 url-safe base64 bytes): {FERNET_HINT}"
    )
    try:
        raw = base64.urlsafe_b64decode(value.strip().encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise ValueError(message) from None
    if len(raw) != 32:
        raise ValueError(message)


def _key(settings: Any) -> str | None:
    secret = getattr(settings, "secrets_encryption_key", None)
    if secret is None:
        return None
    value = secret.get_secret_value().strip()
    return value or None


def encryption_ready(settings: Any) -> bool:
    """True when SECRETS_ENCRYPTION_KEY is set (and non-blank)."""
    return _key(settings) is not None


def _fernet(settings: Any) -> Any:
    key = _key(settings)
    if key is None:
        raise SecretsUnavailable("SECRETS_ENCRYPTION_KEY is not set")
    from cryptography.fernet import Fernet

    return Fernet(key.encode("ascii"))


def encrypt(settings: Any, plaintext: str) -> str:
    """The Fernet token (ASCII) of ``plaintext``; raises SecretsUnavailable without a key."""
    return _fernet(settings).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(settings: Any, ciphertext: str) -> str:
    """The plaintext of a Fernet token; SecretsUnavailable / SecretUnreadable otherwise."""
    fernet = _fernet(settings)
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise SecretUnreadable("stored under another SECRETS_ENCRYPTION_KEY") from None


def readable(settings: Any, ciphertext: str) -> bool | None:
    """Whether the stored value decrypts under this server's key (None: no key configured)."""
    if not encryption_ready(settings):
        return None
    try:
        decrypt(settings, ciphertext)
    except SecretUnreadable:
        return False
    return True


def last4(value: str) -> str:
    return value[-4:]


def anthropic_key_problem(value: str) -> str | None:
    """The first §1 rule an already-trimmed value breaks, or None for a well-formed key."""
    if not value:
        return KEY_EMPTY
    if _WHITESPACE.search(value):
        return KEY_WHITESPACE
    if not value.startswith(ANTHROPIC_KEY_PREFIX):
        return KEY_PREFIX
    if value.startswith(ANTHROPIC_ADMIN_PREFIX):
        return KEY_ADMIN
    if len(value) < ANTHROPIC_KEY_MIN_LENGTH:
        return KEY_TOO_SHORT
    if len(value) > ANTHROPIC_KEY_MAX_LENGTH:
        return KEY_TOO_LONG
    if not _KEY_CHARACTERS.fullmatch(value):
        return KEY_CHARACTERS
    return None


def normalise_anthropic_key(raw: str) -> str:
    """The trimmed key; ValueError with the §1 message (never the value) when it is malformed."""
    value = raw.strip()
    problem = anthropic_key_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def scrub(text_value: str | None, secret: str | None) -> str | None:
    """``text_value`` with every occurrence of ``secret`` replaced by its masked form."""
    if text_value is None or not secret:
        return text_value
    return text_value.replace(secret, f"{ANTHROPIC_KEY_PREFIX}…{last4(secret)}")


_COLUMNS = "id, name, ciphertext, last4, set_by, set_by_user_id, set_at"
SELECT_SQL = f"SELECT {_COLUMNS} FROM app_secrets WHERE municipality_id = :m AND name = :name"
UPSERT_SQL = f"""
    INSERT INTO app_secrets (municipality_id, name, ciphertext, last4, set_by, set_by_user_id)
    VALUES (:m, :name, :ciphertext, :last4, :set_by, :set_by_user_id)
    ON CONFLICT (municipality_id, name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext,
        last4 = EXCLUDED.last4, set_by = EXCLUDED.set_by,
        set_by_user_id = EXCLUDED.set_by_user_id, set_at = now()
    RETURNING {_COLUMNS}
"""
DELETE_SQL = (
    f"DELETE FROM app_secrets WHERE municipality_id = :m AND name = :name RETURNING {_COLUMNS}"
)


def _secret(row: Any) -> StoredSecret | None:
    if row is None:
        return None
    return StoredSecret(
        id=int(row["id"]),
        name=row["name"],
        last4=row["last4"],
        set_by=row["set_by"],
        set_by_user_id=row["set_by_user_id"],
        set_at=row["set_at"],
        ciphertext=row["ciphertext"],
    )


async def load_secret(
    session: AsyncSession, *, municipality_id: str, name: str, for_update: bool = False
) -> StoredSecret | None:
    sql = SELECT_SQL + (" FOR UPDATE" if for_update else "")
    row = (
        (await session.execute(text(sql), {"m": municipality_id, "name": name})).mappings().first()
    )
    return _secret(row)


async def store_secret(
    session: AsyncSession,
    *,
    municipality_id: str,
    name: str,
    ciphertext: str,
    last4: str,
    set_by: str,
    set_by_user_id: int | None,
) -> StoredSecret:
    """Insert or replace the value (the caller commits)."""
    row = (
        (
            await session.execute(
                text(UPSERT_SQL),
                {
                    "m": municipality_id,
                    "name": name,
                    "ciphertext": ciphertext,
                    "last4": last4,
                    "set_by": set_by,
                    "set_by_user_id": set_by_user_id,
                },
            )
        )
        .mappings()
        .one()
    )
    secret = _secret(row)
    assert secret is not None
    return secret


async def delete_secret(
    session: AsyncSession, *, municipality_id: str, name: str
) -> StoredSecret | None:
    """Delete the value and return what was stored (None: nothing was); the caller commits."""
    row = (
        (await session.execute(text(DELETE_SQL), {"m": municipality_id, "name": name}))
        .mappings()
        .first()
    )
    return _secret(row)


async def load_secret_with(
    session_factory: async_sessionmaker[AsyncSession], *, municipality_id: str, name: str
) -> StoredSecret | None:
    """``load_secret`` in a session of its own (the worker's path)."""
    async with session_factory() as session:
        return await load_secret(session, municipality_id=municipality_id, name=name)
