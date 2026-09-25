"""Magic-link login for the admin panel.

``request(email)`` queues the ``magic_link`` e-mail for an active staff user and answers the
same way whether or not the address is known (no enumeration); the worker mints the single-use
token (hash in ``staff_login_tokens``, ``MAGIC_LINK_EXPIRES_SECONDS``) and sends the link
``{ADMIN_BASE_URL}/login?token=…``. ``exchange(token)`` consumes the token once, opens a staff
session (``staff_sessions``, ``STAFF_SESSION_DAYS``) and returns the bearer token the admin panel
keeps. Both are audited.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.email import MagicLinkAccepted, SessionOut, SessionUser
from api.services.audit import write_audit
from api.services.email import EmailService
from core.auth import generate_token, hash_token
from core.errors import UnauthorizedError

ACCEPTED = MagicLinkAccepted(
    message_en="If this address belongs to a staff account, a login link is on its way.",
    message_me="Ako ova adresa pripada nalogu osoblja, link za prijavu je poslat.",
)
FIND_USER_SQL = text(
    "SELECT id, email FROM staff_users WHERE municipality_id = :m AND email = :email AND is_active"
)
TOKEN_SQL = text(
    """
    SELECT t.id, t.user_id, t.expires_at, t.used_at, u.email, u.display_name, u.role, u.is_active,
           u.municipality_id
    FROM staff_login_tokens t JOIN staff_users u ON u.id = t.user_id
    WHERE t.token_hash = :token_hash
    FOR UPDATE OF t
    """
)
USE_TOKEN_SQL = text("UPDATE staff_login_tokens SET used_at = now() WHERE id = :id")
INSERT_SESSION_SQL = text(
    """
    INSERT INTO staff_sessions (user_id, token_hash, created_via, expires_at)
    VALUES (:user_id, :token_hash, 'magic_link', :expires_at)
    """
)
LAST_LOGIN_SQL = text("UPDATE staff_users SET last_login_at = now() WHERE id = :user_id")


class MagicLinkService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        emails: EmailService,
        municipality_id: str,
        session_ttl_days: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.emails = emails
        self.municipality_id = municipality_id
        self.session_ttl = timedelta(days=int(session_ttl_days))
        self.clock = clock

    async def request(self, email: str) -> MagicLinkAccepted:
        normalised = email.strip().lower()
        async with self.session_factory() as session:
            user = (
                (
                    await session.execute(
                        FIND_USER_SQL, {"m": self.municipality_id, "email": normalised}
                    )
                )
                .mappings()
                .first()
            )
            if user is not None:
                await write_audit(
                    session,
                    municipality_id=self.municipality_id,
                    action="auth.magic_link_requested",
                    actor=normalised,
                    entity_type="staff_user",
                    entity_id=int(user["id"]),
                )
                await session.commit()
        if user is not None:
            await self.emails.queue(
                template="magic_link",
                to=user["email"],
                user_id=int(user["id"]),
                requested_by=normalised,
                requested_by_user_id=int(user["id"]),
            )
        return ACCEPTED

    async def exchange(self, token: str) -> SessionOut:
        token_hash = hash_token(token.strip())
        now = self.clock()
        async with self.session_factory() as session:
            row = (await session.execute(TOKEN_SQL, {"token_hash": token_hash})).mappings().first()
            valid = (
                row is not None
                and row["municipality_id"] == self.municipality_id
                and bool(row["is_active"])
                and row["used_at"] is None
                and row["expires_at"] > now
            )
            if not valid:
                raise UnauthorizedError("This login link is invalid, expired or already used")
            await session.execute(USE_TOKEN_SQL, {"id": row["id"]})
            session_token = generate_token()
            expires_at = now + self.session_ttl
            await session.execute(
                INSERT_SESSION_SQL,
                {
                    "user_id": row["user_id"],
                    "token_hash": hash_token(session_token),
                    "expires_at": expires_at,
                },
            )
            await session.execute(LAST_LOGIN_SQL, {"user_id": row["user_id"]})
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                action="auth.login",
                actor=row["email"],
                entity_type="staff_user",
                entity_id=int(row["user_id"]),
                details={"via": "magic_link", "login_token_id": int(row["id"])},
            )
            await session.commit()
        return SessionOut(
            token=session_token,
            expires_at=expires_at,
            user=SessionUser(
                id=int(row["user_id"]),
                email=row["email"],
                display_name=row["display_name"],
                role=row["role"],
            ),
        )
