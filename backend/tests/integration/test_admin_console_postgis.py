"""The admin console's own API: who am I, sign-out, and the role gates the console's tabs rely on
(a reviewer gets 403 on users and assumptions, an expert on the review queue)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core.staff import issue_session
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration
TOKEN = "console-admin-token-0123456789abcdef"
EMAILS = {
    "admin": "console.admin@example.com",
    "reviewer": "console.reviewer@example.com",
    "expert": "console.expert@example.com",
}


@pytest.fixture
def console_app(postgis_url):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
    )
    return make_app(settings)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_me_sign_out_and_role_gates(console_app) -> None:
    app = console_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        async with app.state.session_factory() as session:
            await session.execute(
                text("DELETE FROM staff_users WHERE email = ANY(:e)"), {"e": list(EMAILS.values())}
            )
            await session.commit()
        for role, email in EMAILS.items():
            created = await client.post(
                "/v1/admin/users", json={"email": email, "role": role}, headers=auth()
            )
            assert created.status_code == 201, created.text
        tokens = {
            role: await issue_session(
                app.state.session_factory, municipality_id="podgorica", email=email
            )
            for role, email in EMAILS.items()
        }

        # who am I: a session user, and a configured service token
        me = await client.get("/v1/admin/users/me", headers=auth(tokens["reviewer"]))
        assert me.status_code == 200
        body = me.json()
        assert body["role"] == "reviewer" and body["email"] == EMAILS["reviewer"]
        assert body["via"] == "session" and isinstance(body["id"], int)
        service = (await client.get("/v1/admin/users/me", headers=auth())).json()
        assert service == {
            "id": None,
            "email": None,
            "display_name": None,
            "role": "admin",
            "subject": "ops",
            "via": "token",
        }
        assert (await client.get("/v1/admin/users/me")).status_code == 401

        # the review queue: admins and reviewers; experts are refused (403, not 404)
        assert (
            await client.get("/v1/admin/review", headers=auth(tokens["reviewer"]))
        ).status_code == 200
        assert (
            await client.get("/v1/admin/review", headers=auth(tokens["expert"]))
        ).status_code == 403

        # the console hides these tabs from a reviewer; the API refuses them too
        for path in ("/v1/admin/users", "/v1/admin/assumptions"):
            refused = await client.get(path, headers=auth(tokens["reviewer"]))
            assert refused.status_code == 403, path
            assert refused.json()["error"]["details"]["required_roles"] == ["admin"]
        # an expert may read the order queue (only their assignments) but not the audit trail
        assert (
            await client.get("/v1/admin/orders", headers=auth(tokens["expert"]))
        ).status_code == 200
        assert (
            await client.get("/v1/admin/audit", headers=auth(tokens["expert"]))
        ).status_code == 403

        # sign-out revokes the session; a second sign-out and an unknown token answer the same
        out = await client.post("/v1/auth/sign-out", headers=auth(tokens["admin"]))
        assert out.status_code == 204
        assert (
            await client.get("/v1/admin/users/me", headers=auth(tokens["admin"]))
        ).status_code == 401
        assert (
            await client.post("/v1/auth/sign-out", headers=auth(tokens["admin"]))
        ).status_code == 204
        assert (await client.post("/v1/auth/sign-out", headers=auth("nope" * 8))).status_code == 204
        assert (await client.post("/v1/auth/sign-out")).status_code == 204
        logout = (
            await client.get("/v1/admin/audit", params={"action": "auth.logout"}, headers=auth())
        ).json()
        assert any(row["actor"] == EMAILS["admin"] for row in logout["items"])
