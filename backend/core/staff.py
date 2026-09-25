"""Staff accounts: create users, issue and revoke bearer sessions.

The magic-link login item verifies an e-mail link and then calls ``issue_session``; until it
exists (and for bootstrapping), the CLI does the same from a shell:

    python -m core.staff add --email ana@example.com --role admin --name "Ana"
    python -m core.staff token --email ana@example.com --days 30     # prints the bearer token once
    python -m core.staff revoke --email ana@example.com
    python -m core.staff list

Users belong to the configured municipality (``MUNICIPALITY_ID``). E-mail is the only personal
datum stored about staff; tokens are stored as SHA-256 hashes only.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.auth import Role, generate_token, hash_token

SessionFactory = async_sessionmaker[AsyncSession]


async def create_user(
    factory: SessionFactory,
    *,
    municipality_id: str,
    email: str,
    role: Role | str,
    display_name: str | None = None,
    is_active: bool = True,
) -> int:
    role = Role(role)
    async with factory() as session:
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
        await session.commit()
    return int(user_id)


async def issue_session(
    factory: SessionFactory,
    *,
    municipality_id: str,
    email: str,
    ttl: timedelta = timedelta(days=30),
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


async def _main(args: argparse.Namespace) -> None:
    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    municipality_id = settings.municipality_id
    try:
        if args.command == "add":
            user_id = await create_user(
                factory,
                municipality_id=municipality_id,
                email=args.email,
                role=args.role,
                display_name=args.name,
            )
            print(f"created staff user {args.email} ({args.role}) id={user_id}")
        elif args.command == "token":
            token = await issue_session(
                factory,
                municipality_id=municipality_id,
                email=args.email,
                ttl=timedelta(days=args.days),
            )
            print(token)
        elif args.command == "revoke":
            n = await revoke_sessions(factory, municipality_id=municipality_id, email=args.email)
            print(f"revoked {n} session(s)")
        elif args.command == "list":
            for user in await list_users(factory, municipality_id=municipality_id):
                state = "active" if user["is_active"] else "inactive"
                print(f"{user['id']:>4}  {user['email']:<40} {user['role']:<9} {state}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage staff users and sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create a staff user")
    add.add_argument("--email", required=True)
    add.add_argument("--role", required=True, choices=[r.value for r in Role])
    add.add_argument("--name", default=None)
    token = sub.add_parser("token", help="issue a bearer session token for a user")
    token.add_argument("--email", required=True)
    token.add_argument("--days", type=int, default=30)
    revoke = sub.add_parser("revoke", help="revoke every open session of a user")
    revoke.add_argument("--email", required=True)
    sub.add_parser("list", help="list staff users")
    asyncio.run(_main(parser.parse_args()))
