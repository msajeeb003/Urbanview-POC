"""Principals for the staff-facing routes (``/v1/admin/...``): who is calling, with which role.

Two sources, both presented as ``Authorization: Bearer <token>``:

- **configured tokens** (``ADMIN_API_TOKENS="<token>:<role>[:<subject>],..."``): service access for
  the client dashboard and for bootstrapping before anyone has logged in;
- **staff sessions** (``staff_sessions`` joined to ``staff_users``, migration 0006): the users /
  roles model. The magic-link login item issues a session after verifying an e-mail link
  (``core.staff.issue_session``); here the session is only verified: not revoked, not expired,
  user active, same municipality.

Roles are ``admin`` / ``reviewer`` / ``expert``; a route states the roles it accepts
(``api.deps.require_role``). Tokens are compared in constant time or looked up by SHA-256, and
never logged. Nothing configured and no sessions means every role-gated route answers 401.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class Role(StrEnum):
    admin = "admin"
    reviewer = "reviewer"
    expert = "expert"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: Role
    user_id: int | None = None  # staff_users.id for session principals; None for config tokens


def generate_token() -> str:
    """A new bearer token (256 bits, URL-safe). Shown once; only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def parse_api_tokens(raw: str | None) -> dict[str, Principal]:
    """``token:role[:subject]`` entries separated by commas (whitespace ignored)."""
    tokens: dict[str, Principal] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("ADMIN_API_TOKENS entries must look like token:role[:subject]")
        token, role_name = parts[0], parts[1].strip().lower()
        try:
            role = Role(role_name)
        except ValueError:
            known = ", ".join(r.value for r in Role)
            raise ValueError(
                f"unknown role {role_name!r} in ADMIN_API_TOKENS (known: {known})"
            ) from None
        subject = parts[2].strip() if len(parts) == 3 and parts[2].strip() else role.value
        if token in tokens:
            raise ValueError("duplicate token in ADMIN_API_TOKENS")
        tokens[token] = Principal(subject=subject, role=role)
    return tokens


class TokenAuthenticator:
    """Configured service tokens."""

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        self._tokens = dict(tokens)

    @property
    def configured(self) -> bool:
        return bool(self._tokens)

    def authenticate(self, authorization: str | None) -> Principal | None:
        token = bearer_token(authorization)
        if token is None:
            return None
        found: Principal | None = None
        for known, principal in self._tokens.items():  # constant time per token, no early exit
            if hmac.compare_digest(known.encode(), token.encode()):
                found = principal
        return found


STAFF_SESSION_SQL = text(
    """
    SELECT u.id AS user_id, u.email, u.role
    FROM staff_sessions s
    JOIN staff_users u ON u.id = s.user_id
    WHERE s.token_hash = :token_hash
      AND s.revoked_at IS NULL
      AND s.expires_at > now()
      AND u.is_active
      AND u.municipality_id = :municipality_id
    """
)


class StaffSessionAuthenticator:
    """The users / roles model: a bearer session token -> the staff user behind it."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], municipality_id: str
    ) -> None:
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def authenticate(self, authorization: str | None) -> Principal | None:
        token = bearer_token(authorization)
        if token is None:
            return None
        params = {"token_hash": hash_token(token), "municipality_id": self.municipality_id}
        async with self.session_factory() as session:
            row = (await session.execute(STAFF_SESSION_SQL, params)).mappings().first()
        if row is None:
            return None
        return Principal(subject=row["email"], role=Role(row["role"]), user_id=int(row["user_id"]))
