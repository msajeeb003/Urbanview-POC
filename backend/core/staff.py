"""Staff accounts: users, bearer sessions, one-time sign-in links.

The admin console signs staff in with e-mailed magic links (``api.services.auth``) and manages
users on its Users page (``/v1/admin/users``, audited); for bootstrapping (a server without staff
users or without SMTP) the CLI prints a sign-in link from a shell:

    python -m core.staff login-link --email ana@example.com [--create --role admin --name "Ana"] \
        [--minutes 15]                           # prints a one-time sign-in link (no SMTP needed)
    python -m core.staff list

``login-link`` mints the same single-use token the ``magic_link`` e-mail carries
(``staff_login_tokens``, no e-mail row) and prints ``{ADMIN_BASE_URL}/login?token=…`` once; the
console's ``/admin/login?token=`` exchanges it for a session. Audited ``auth.login_link_issued``
(actor ``cli``), and ``user.create`` when ``--create`` added the user.

Users belong to the configured municipality (``MUNICIPALITY_ID``). E-mail is the only personal
datum stored about staff; tokens are stored as SHA-256 hashes only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.auth import Role, generate_token, hash_token

SessionFactory = async_sessionmaker[AsyncSession]


async def _insert_user(
    session: AsyncSession,
    *,
    municipality_id: str,
    email: str,
    role: Role | str,
    display_name: str | None,
    is_active: bool = True,
) -> int:
    """Insert a staff user in the caller's transaction (no commit)."""
    role = Role(role)
    user_id = (
        await session.execute(
            text(
                "INSERT INTO staff_users (municipality_id, email, display_name, role, "
                "is_active) VALUES (:m, :email, :display_name, :role, :is_active) "
                "RETURNING id"
            ),
            {
                "m": municipality_id,
                "email": email.strip().lower(),
                "display_name": display_name,
                "role": role.value,
                "is_active": is_active,
            },
        )
    ).scalar_one()
    return int(user_id)


async def create_user(
    factory: SessionFactory,
    *,
    municipality_id: str,
    email: str,
    role: Role | str,
    display_name: str | None = None,
    is_active: bool = True,
) -> int:
    async with factory() as session:
        user_id = await _insert_user(
            session,
            municipality_id=municipality_id,
            email=email,
            role=role,
            display_name=display_name,
            is_active=is_active,
        )
        await session.commit()
    return user_id


@dataclass(frozen=True, slots=True)
class LoginLink:
    user_id: int
    email: str
    created: bool
    expires_at: datetime
    url: str = field(repr=False)


async def issue_login_link(
    factory: SessionFactory,
    *,
    municipality_id: str,
    email: str,
    admin_base_url: str,
    expires_seconds: int,
    create_role: Role | str | None = None,
    display_name: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LoginLink:
    """A one-time sign-in link for the admin console (the magic link, without the e-mail).

    One transaction: the user (created with ``create_role`` when missing), the single-use token
    (``staff_login_tokens``, hash only, no e-mail row), the audit rows. LookupError for an
    unknown user without ``create_role`` or an inactive one. The URL carries the token: shown
    once, never logged."""
    from api.services.audit import write_audit
    from core.mail.repository import INSERT_TOKEN_SQL

    address = email.strip().lower()
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, is_active FROM staff_users "
                        "WHERE municipality_id = :m AND email = :email FOR UPDATE"
                    ),
                    {"m": municipality_id, "email": address},
                )
            )
            .mappings()
            .first()
        )
        created = False
        if row is None:
            if create_role is None:
                raise LookupError(f"no staff user {address!r}: add --create --role admin")
            role = Role(create_role)
            user_id = await _insert_user(
                session,
                municipality_id=municipality_id,
                email=address,
                role=role,
                display_name=display_name,
            )
            created = True
            await write_audit(
                session,
                municipality_id=municipality_id,
                actor="cli",
                action="user.create",
                entity_type="staff_user",
                entity_id=user_id,
                details={"email": address, "role": role.value},
                after={
                    "email": address,
                    "role": role.value,
                    "display_name": display_name,
                    "is_active": True,
                },
            )
        elif not row["is_active"]:
            raise LookupError(f"staff user {address!r} is inactive")
        else:
            user_id = int(row["id"])
        token = generate_token()
        expires_at = clock() + timedelta(seconds=expires_seconds)
        await session.execute(
            INSERT_TOKEN_SQL,
            {
                "user_id": user_id,
                "token_hash": hash_token(token),
                "expires_at": expires_at,
                "email_log_id": None,
            },
        )
        await write_audit(
            session,
            municipality_id=municipality_id,
            actor="cli",
            action="auth.login_link_issued",
            entity_type="staff_user",
            entity_id=user_id,
            details={
                "via": "cli",
                "expires_at": expires_at.isoformat(),
                "created_user": created,
            },
        )
        await session.commit()
    return LoginLink(
        user_id=user_id,
        email=address,
        created=created,
        expires_at=expires_at,
        url=f"{admin_base_url.rstrip('/')}/login?token={token}",
    )


async def issue_session(
    factory: SessionFactory,
    *,
    municipality_id: str,
    email: str,
    ttl: timedelta = timedelta(days=1),
    created_via: str = "cli",
) -> str:
    """Create a session for an active user and return the bearer token (shown once)."""
    token = generate_token()
    async with factory() as session:
        user_id = (
            await session.execute(
                text(
                    "SELECT id FROM staff_users WHERE municipality_id = :m AND email = :email "
                    "AND is_active"
                ),
                {"m": municipality_id, "email": email.strip().lower()},
            )
        ).scalar_one_or_none()
        if user_id is None:
            raise LookupError(f"no active staff user {email!r} in municipality {municipality_id!r}")
        await session.execute(
            text(
                "INSERT INTO staff_sessions (user_id, token_hash, created_via, expires_at) "
                "VALUES (:user_id, :token_hash, :created_via, :expires_at)"
            ),
            {
                "user_id": user_id,
                "token_hash": hash_token(token),
                "created_via": created_via,
                "expires_at": datetime.now(UTC) + ttl,
            },
        )
        await session.execute(
            text("UPDATE staff_users SET last_login_at = now() WHERE id = :user_id"),
            {"user_id": user_id},
        )
        await session.commit()
    return token


async def revoke_sessions(factory: SessionFactory, *, municipality_id: str, email: str) -> int:
    async with factory() as session:
        result = await session.execute(
            text(
                "UPDATE staff_sessions SET revoked_at = now() WHERE revoked_at IS NULL AND user_id "
                "IN (SELECT id FROM staff_users WHERE municipality_id = :m AND email = :email)"
            ),
            {"m": municipality_id, "email": email.strip().lower()},
        )
        await session.commit()
    return int(result.rowcount or 0)


async def list_users(factory: SessionFactory, *, municipality_id: str) -> list[dict]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, email, display_name, role, is_active, created_at, last_login_at "
                    "FROM staff_users WHERE municipality_id = :m ORDER BY email"
                ),
                {"m": municipality_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


def _link_minutes(requested: int | None, default_seconds: int) -> int:
    """``--minutes`` (default: MAGIC_LINK_EXPIRES_SECONDS), clamped to 1-60."""
    minutes = requested if requested is not None else default_seconds // 60
    return max(1, min(60, minutes))


async def _main(args: argparse.Namespace) -> int:
    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    municipality_id = settings.municipality_id
    try:
        if args.command == "login-link":
            minutes = _link_minutes(args.minutes, settings.magic_link_expires_seconds)
            try:
                link = await issue_login_link(
                    factory,
                    municipality_id=municipality_id,
                    email=args.email,
                    admin_base_url=settings.admin_base_url,
                    expires_seconds=minutes * 60,
                    create_role=args.role if args.create else None,
                    display_name=args.name,
                )
            except LookupError as exc:
                print(exc, file=sys.stderr)
                return 1
            if link.created:
                print(f"created staff user {link.email} ({args.role})", file=sys.stderr)
            expires = link.expires_at.astimezone(UTC).strftime("%H:%M")
            print(f"Sign-in link for {link.email} (single use, expires {expires} UTC):")
            print(link.url)
        elif args.command == "list":
            for user in await list_users(factory, municipality_id=municipality_id):
                state = "active" if user["is_active"] else "inactive"
                print(f"{user['id']:>4}  {user['email']:<40} {user['role']:<9} {state}")
    finally:
        await engine.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap sign-in links and the staff list.")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login-link", help="print a one-time sign-in link for the admin console")
    login.add_argument("--email", required=True)
    login.add_argument("--create", action="store_true", help="create the user when missing")
    login.add_argument(
        "--role",
        choices=[r.value for r in Role],
        default=Role.admin.value,
        help="the new user's role (only with --create)",
    )
    login.add_argument("--name", default=None, help="the new user's display name (--create)")
    login.add_argument(
        "--minutes", type=int, default=None, help="link lifetime (default: the e-mailed link's)"
    )
    sub.add_parser("list", help="list staff users")
    return parser


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(build_parser().parse_args())))
