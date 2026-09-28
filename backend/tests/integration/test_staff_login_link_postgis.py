"""The bootstrap sign-in link (``python -m core.staff login-link``) on PostGIS: the single-use
token row (hash only, no e-mail), the console's exchange, user creation with its audit rows,
refusals, and the CLI printing the link."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.auth import hash_token
from core.staff import _main, build_parser, create_user, issue_login_link
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

M = "podgorica"
BASE = "https://uv.test/admin"
CLEANUP = (
    "DELETE FROM staff_login_tokens",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)


@pytest.fixture
async def factory(postgis_url):
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with sessions() as session:
            for statement in CLEANUP:
                await session.execute(text(statement))
            await session.commit()

    await clean()
    yield sessions
    await clean()
    await engine.dispose()


def token_of(url: str) -> str:
    (token,) = parse_qs(urlsplit(url).query)["token"]
    return token


async def rows(factory, sql: str, **params) -> list[dict]:
    async with factory() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


async def test_a_link_for_an_existing_user_signs_in_once(factory, postgis_url):
    user_id = await create_user(factory, municipality_id=M, email="Ana@Example.com", role="admin")
    now = datetime.now(UTC).replace(microsecond=0)
    link = await issue_login_link(
        factory,
        municipality_id=M,
        email="ana@example.com ",
        admin_base_url=BASE + "/",
        expires_seconds=900,
        clock=lambda: now,
    )
    assert (link.user_id, link.email, link.created) == (user_id, "ana@example.com", False)
    assert link.url.startswith(f"{BASE}/login?token=") and link.url not in repr(link)
    token = token_of(link.url)
    (row,) = await rows(factory, "SELECT * FROM staff_login_tokens WHERE user_id = :u", u=user_id)
    assert row["email_log_id"] is None and row["used_at"] is None
    assert row["token_hash"] == hash_token(token) and token not in str(row)
    assert row["expires_at"] == now + timedelta(seconds=900) == link.expires_at
    (audit,) = await rows(
        factory,
        "SELECT actor, details FROM audit_log WHERE action = 'auth.login_link_issued' "
        "AND entity_id = :u",
        u=user_id,
    )
    assert audit["actor"] == "cli" and audit["details"]["via"] == "cli"
    assert audit["details"]["created_user"] is False and token not in str(audit)

    settings = make_settings(
        location_resolver="postgis", database_url=postgis_url, rate_limit_requests=100_000
    )
    app = make_app(settings)
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await client.post("/v1/auth/magic-link/exchange", json={"token": token})
        second = await client.post("/v1/auth/magic-link/exchange", json={"token": token})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["token"] and body["user"]["email"] == "ana@example.com"
    assert body["user"]["role"] == "admin"
    assert second.status_code == 401


async def test_unknown_users_are_created_only_when_asked(factory):
    with pytest.raises(LookupError) as caught:
        await issue_login_link(
            factory,
            municipality_id=M,
            email="new@example.com",
            admin_base_url=BASE,
            expires_seconds=900,
        )
    assert "--create" in str(caught.value)
    assert await rows(factory, "SELECT id FROM staff_users WHERE email = 'new@example.com'") == []

    link = await issue_login_link(
        factory,
        municipality_id=M,
        email="New@Example.com",
        admin_base_url=BASE,
        expires_seconds=600,
        create_role="admin",
        display_name="New Admin",
    )
    assert link.created
    (user,) = await rows(
        factory,
        "SELECT id, role, display_name, is_active FROM staff_users WHERE id = :u",
        u=link.user_id,
    )
    assert (user["role"], user["display_name"], user["is_active"]) == ("admin", "New Admin", True)
    audits = await rows(
        factory,
        "SELECT action, actor, details, after FROM audit_log WHERE entity_type = 'staff_user' "
        "AND entity_id = :u ORDER BY id",
        u=link.user_id,
    )
    assert [(a["action"], a["actor"]) for a in audits] == [
        ("user.create", "cli"),
        ("auth.login_link_issued", "cli"),
    ]
    assert audits[0]["details"] == {"email": "new@example.com", "role": "admin"}
    assert audits[0]["after"]["is_active"] is True
    assert audits[1]["details"]["created_user"] is True


async def test_an_inactive_user_is_refused(factory):
    await create_user(
        factory, municipality_id=M, email="old@example.com", role="reviewer", is_active=False
    )
    with pytest.raises(LookupError) as caught:
        await issue_login_link(
            factory,
            municipality_id=M,
            email="old@example.com",
            admin_base_url=BASE,
            expires_seconds=900,
            create_role="admin",
        )
    assert "inactive" in str(caught.value)
    assert await rows(factory, "SELECT id FROM staff_login_tokens") == []


async def test_the_cli_prints_the_link(factory, postgis_url, monkeypatch, capsys):
    import core.config

    settings = make_settings(database_url=postgis_url, admin_base_url=BASE)
    monkeypatch.setattr(core.config, "get_settings", lambda: settings)
    args = build_parser().parse_args(
        ["login-link", "--email", "cli@example.com", "--create", "--name", "Cli", "--minutes", "90"]
    )
    assert await _main(args) == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert lines[0].startswith("Sign-in link for cli@example.com (single use, expires ")
    assert lines[1].startswith(settings.admin_base_url + "/login?token=")
    assert err.strip() == "created staff user cli@example.com (admin)"
    (row,) = await rows(factory, "SELECT created_at, expires_at FROM staff_login_tokens")
    assert row["expires_at"] - row["created_at"] <= timedelta(minutes=60, seconds=5)

    missing = build_parser().parse_args(["login-link", "--email", "nobody@example.com"])
    assert await _main(missing) == 1
    out, err = capsys.readouterr()
    assert out == "" and "no staff user 'nobody@example.com'" in err
