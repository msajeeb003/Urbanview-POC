"""Admin configuration on PostGIS: financial assumption versions (and what the panel and the
feasibility route state), absolute bounds flowing into the ranges, zone parameter set versions
on the zone panel, staff user CRUD with session revocation, and the audit rows behind it all."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from core.staff import issue_session
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
CLEANUP = (
    "DELETE FROM zone_parameter_sets WHERE created_by <> 'seed'",
    "UPDATE zone_parameter_sets SET is_current = true, retired_at = NULL, retired_by = NULL "
    "WHERE created_by = 'seed'",
    "DELETE FROM financial_assumptions WHERE created_by <> 'seed'",
    "UPDATE financial_assumptions SET is_current = true, retired_at = NULL, retired_by = NULL "
    "WHERE created_by = 'seed'",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)


@pytest.fixture
def config_app(postgis_url):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
    )
    return make_app(settings)


@pytest.fixture(autouse=True)
async def _clean_config_rows(postgis_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    async def clean() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in CLEANUP:
                    await session.execute(text(statement))
                await session.commit()
        finally:
            await engine.dispose()

    await clean()
    yield
    await clean()


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def rates(land=1400, build=900, design=95, sale=2500, **extra):
    return {
        "land_rate": {"expected": land},
        "build_rate": {"expected": build},
        "design_rate": {"expected": design},
        "sale_rate": {"expected": sale},
        "source": "Realitica, Estitor, Monstat (test)",
        "source_date": "2026-09-10",
        **extra,
    }


async def audit_actions(app, entity_type, entity_id):
    async with app.state.session_factory() as session:
        rows = await session.execute(
            text(
                "SELECT action, actor, details FROM audit_log WHERE municipality_id = 'podgorica' "
                "AND entity_type = :t AND entity_id = :i ORDER BY id"
            ),
            {"t": entity_type, "i": entity_id},
        )
        return [dict(r) for r in rows.mappings()]


# --- financial assumptions ------------------------------------------------------------------------


async def test_assumption_versions_and_what_the_panel_states(config_app):
    app = config_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        current = (await client.get("/v1/admin/assumptions", headers=auth())).json()["items"]
        v2 = await client.post(
            "/v1/admin/assumptions", json={"zone_id": 1, **rates()}, headers=auth()
        )
        assert v2.status_code == 201, v2.text
        v2 = v2.json()
        history = (
            await client.get(
                "/v1/admin/assumptions",
                params={"zone_id": 1, "include_history": "true"},
                headers=auth(),
            )
        ).json()["items"]
        only_current = (
            await client.get("/v1/admin/assumptions", params={"zone_id": 1}, headers=auth())
        ).json()["items"]
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        feasibility = (
            await client.post("/v1/feasibility", json={"parcel_id": 1, "type": "urban"})
        ).json()
        v3 = await client.put(
            f"/v1/admin/assumptions/{v2['id']}",
            json={"sale_rate": {"expected": 2600}},
            headers=auth(),
        )
        assert v3.status_code == 201, v3.text
        v3 = v3.json()
        stale = await client.put("/v1/admin/assumptions/1", json={"notes": "late"}, headers=auth())
        empty = await client.put(f"/v1/admin/assumptions/{v3['id']}", json={}, headers=auth())
        retired = await client.delete(f"/v1/admin/assumptions/{v3['id']}", headers=auth())
        panel_without = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        default = await client.post(
            "/v1/admin/assumptions", json={"zone_id": None, **rates(land=1000)}, headers=auth()
        )
        panel_default = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        bad_bounds = await client.post(
            "/v1/admin/assumptions",
            json={
                "zone_id": 1,
                **rates(),
                "land_rate": {"expected": 1400, "low": 1500, "high": 1600},
            },
            headers=auth(),
        )
        bad_zone = await client.post(
            "/v1/admin/assumptions", json={"zone_id": 999_999, **rates()}, headers=auth()
        )
        anonymous = await client.get("/v1/admin/assumptions")

    assert sorted(item["id"] for item in current) == [1, 2]  # the seeded current rows
    assert all(item["version"] == 1 and item["is_current"] for item in current)

    assert (v2["zone_id"], v2["zone_name"], v2["version"]) == (1, "Centar", 2)
    assert v2["is_current"] is True and v2["supersedes_id"] == 1
    assert v2["build_rate"] == {
        "expected": 900.0,
        "low": 774.0,
        "high": 1035.0,
        "kind": "multiplier",
    }
    assert v2["source"] == "Realitica, Estitor, Monstat (test)" and v2["created_by"] == "ops"
    assert [(item["id"], item["version"], item["is_current"]) for item in history] == [
        (v2["id"], 2, True),
        (1, 1, False),
    ]
    assert [item["id"] for item in only_current] == [v2["id"]]

    market = panel["market_inputs"]
    assert market["available"] is True
    assert {k: market["version"][k] for k in ("id", "version", "zone_id")} == {
        "id": v2["id"],
        "version": 2,
        "zone_id": 1,
    }
    assert market["version"]["effective_from"].endswith("Z")  # the panel speaks UTC
    assert datetime.fromisoformat(market["version"]["effective_from"]) == datetime.fromisoformat(
        v2["created_at"]
    )
    assert (
        market["build_rate_eur_m2"] == 900
        and market["ranges"]["build_rate"]["kind"] == "multiplier"
    )
    assert panel["assumptions"]["market_version"]["version"] == 2
    assert panel["assumptions"]["construction_cost_eur_m2"] == 900
    assert feasibility["assumptions"]["market_version"]["id"] == v2["id"]

    assert (v3["version"], v3["supersedes_id"], v3["sale_rate"]["expected"]) == (
        3,
        v2["id"],
        2600.0,
    )
    assert v3["build_rate"]["expected"] == 900.0  # carried over
    assert stale.status_code == 409 and stale.json()["error"]["details"]["reason"] == "not_current"
    assert empty.status_code == 422
    assert retired.status_code == 200
    assert retired.json()["is_current"] is False and retired.json()["retired_by"] == "ops"
    assert panel_without["market_inputs"]["available"] is False
    assert panel_without["market_inputs"]["reason_code"] == "no_market_data"
    assert panel_without["market_inputs"]["version"] is None
    assert default.status_code == 201 and default.json()["zone_id"] is None
    assert panel_default["market_inputs"]["version"]["zone_id"] is None
    assert panel_default["market_inputs"]["land_rate_eur_m2"] == 1000
    assert bad_bounds.status_code == 422 and "low" in bad_bounds.text
    assert bad_zone.status_code == 422 and "zone" in bad_zone.text
    assert anonymous.status_code == 401

    assert [a["action"] for a in await audit_actions(app, "financial_assumptions", v2["id"])] == [
        "assumptions.create"
    ]
    v3_audit = await audit_actions(app, "financial_assumptions", v3["id"])
    assert [a["action"] for a in v3_audit] == ["assumptions.update", "assumptions.retire"]
    assert v3_audit[0]["details"]["changed"] == ["sale_rate"]
    assert v3_audit[0]["details"]["supersedes_id"] == v2["id"]


async def test_absolute_bounds_flow_into_the_ranges(config_app):
    app = config_app
    body = {
        "zone_id": 1,
        **rates(land=1350, build=860, design=90, sale=2450),
        "build_rate": {"expected": 860, "low": 800, "high": 900},
        "sale_rate": {"expected": 2450, "low": 2300, "high": 2600},
    }
    async with app.router.lifespan_context(app), make_client(app) as client:
        created = await client.post("/v1/admin/assumptions", json=body, headers=auth())
        assert created.status_code == 201, created.text
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
    ranges = panel["market_inputs"]["ranges"]
    assert ranges["sale_rate"] == {
        "expected": 2450.0,
        "low": 2300.0,
        "high": 2600.0,
        "kind": "absolute",
    }
    assert ranges["build_rate"] == {
        "expected": 860.0,
        "low": 800.0,
        "high": 900.0,
        "kind": "absolute",
    }
    assert ranges["land_rate"] == {
        "expected": 1350.0,
        "low": 1161.0,
        "high": 1552.5,
        "kind": "multiplier",
    }
    fields = {f["key"]: f for f in panel["feasibility"]["fields"]}
    rows = {r["key"]: r for r in panel["feasibility"]["cost_rows"]}
    gfa = panel["basis_area_m2"] * 3.2  # the engine rounds outputs only, never intermediates
    saleable = gfa * 0.7
    assert fields["revenue_eur"]["low"] == pytest.approx(saleable * 2300, abs=0.05)
    assert fields["revenue_eur"]["high"] == pytest.approx(saleable * 2600, abs=0.05)
    assert rows["construction_cost_eur"]["low"] == pytest.approx(gfa * 800, abs=0.05)
    assert rows["construction_cost_eur"]["high"] == pytest.approx(gfa * 900, abs=0.05)
    assert rows["land_value_eur"]["low"] == pytest.approx(
        panel["basis_area_m2"] * 1350 * 0.86, abs=0.05
    )
    assert created.json()["sale_rate"]["kind"] == "absolute"


# --- zone parameter sets --------------------------------------------------------------------------


async def test_zone_parameter_versions_and_the_zone_panel(config_app):
    app = config_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        seeded = (await client.get("/v1/panel", params={"type": "zone", "id": 1})).json()
        empty = (await client.get("/v1/panel", params={"type": "zone", "id": 2})).json()
        v1 = await client.post(
            "/v1/admin/zone-parameters",
            json={
                "zone_id": 2,
                "land_use": "Residential – low density",
                "max_far": 1.5,
                "max_site_coverage_pct": 40,
                "source_document_id": 4,
                "source_page": 5,
                "source_note": "chapter 4",
                "verified_on": "2026-09-15",
                "verified_by": "expert",
            },
            headers=auth(),
        )
        assert v1.status_code == 201, v1.text
        v1 = v1.json()
        with_v1 = (await client.get("/v1/panel", params={"type": "zone", "id": 2})).json()
        v2 = await client.post(
            "/v1/admin/zone-parameters", json={"zone_id": 2, "max_floors": 4}, headers=auth()
        )
        assert v2.status_code == 201, v2.text
        v2 = v2.json()
        v3 = await client.put(
            f"/v1/admin/zone-parameters/{v2['id']}", json={"max_height_m": 12}, headers=auth()
        )
        assert v3.status_code == 201, v3.text
        v3 = v3.json()
        history = (
            await client.get(
                "/v1/admin/zone-parameters",
                params={"zone_id": 2, "include_history": "true"},
                headers=auth(),
            )
        ).json()["items"]
        stale = await client.put(
            f"/v1/admin/zone-parameters/{v1['id']}", json={"max_far": 2}, headers=auth()
        )
        retired = await client.delete(f"/v1/admin/zone-parameters/{v3['id']}", headers=auth())
        after = (await client.get("/v1/panel", params={"type": "zone", "id": 2})).json()
        bad = [
            await client.post("/v1/admin/zone-parameters", json=b, headers=auth())
            for b in (
                {"zone_id": 999_999, "max_far": 1},
                {"zone_id": 2, "max_far": 1, "source_document_id": 999_999},
                {"zone_id": 2, "max_far": 1, "source_page": 3},
                {"zone_id": 2, "max_site_coverage_pct": 101},
                {"zone_id": 2},
            )
        ]

    typical = seeded["typical_parameters"]
    assert typical["version"] == 1 and typical["max_far"] == 3.2 and typical["max_floors"] == 7
    assert typical["source"]["document_name"] == "DUP Centar – Zona C2"
    assert typical["source"]["page"] == 12 and typical["verified_on"] == "2026-09-01"
    assert "take precedence" in typical["note_en"] and typical["note_me"]
    assert empty["typical_parameters"] is None

    assert (v1["zone_id"], v1["zone_name"], v1["version"]) == (2, "Stari Aerodrom", 1)
    assert v1["source"]["document_name"] == "DUP Stari Aerodrom" and v1["source"]["page"] == 5
    assert with_v1["typical_parameters"]["max_far"] == 1.5
    assert with_v1["typical_parameters"]["source"]["document_id"] == 4
    assert with_v1["typical_parameters"]["verified_by"] == "expert"
    assert (v2["version"], v2["supersedes_id"], v2["max_far"]) == (2, v1["id"], None)  # fresh set
    assert (v3["version"], v3["supersedes_id"]) == (3, v2["id"])
    assert (v3["max_floors"], v3["max_height_m"]) == (4, 12.0)  # carried over + changed
    assert [(item["version"], item["is_current"]) for item in history] == [
        (3, True),
        (2, False),
        (1, False),
    ]
    assert stale.status_code == 409
    assert retired.status_code == 200 and retired.json()["is_current"] is False
    assert after["typical_parameters"] is None
    assert [r.status_code for r in bad] == [422, 422, 422, 422, 422]
    assert [a["action"] for a in await audit_actions(app, "zone_parameter_set", v3["id"])] == [
        "zone_parameters.update",
        "zone_parameters.retire",
    ]


# --- staff users ----------------------------------------------------------------------------------


async def test_staff_users_crud_without_passwords(config_app):
    app = config_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        ana = await client.post(
            "/v1/admin/users",
            json={"email": "Ana.Novak@Example.com", "role": "admin", "display_name": "Ana"},
            headers=auth(),
        )
        assert ana.status_code == 201, ana.text
        ana = ana.json()
        bob = (
            await client.post(
                "/v1/admin/users",
                json={"email": "bob@example.com", "role": "reviewer"},
                headers=auth(),
            )
        ).json()
        duplicate = await client.post(
            "/v1/admin/users",
            json={"email": "ANA.NOVAK@example.com", "role": "expert"},
            headers=auth(),
        )
        invalid = await client.post(
            "/v1/admin/users", json={"email": "not-an-email", "role": "admin"}, headers=auth()
        )
        bad_role = await client.post(
            "/v1/admin/users", json={"email": "x@example.com", "role": "god"}, headers=auth()
        )
        with_password = await client.post(
            "/v1/admin/users",
            json={"email": "y@example.com", "role": "admin", "password": "hunter2"},
            headers=auth(),
        )
        listed = (await client.get("/v1/admin/users", headers=auth())).json()["items"]

        session_token = await issue_session(
            app.state.session_factory, municipality_id="podgorica", email="ana.novak@example.com"
        )
        as_ana = await client.get("/v1/admin/users", headers=auth(session_token))
        before = (await client.get(f"/v1/admin/users/{ana['id']}", headers=auth())).json()
        deactivated = await client.patch(
            f"/v1/admin/users/{ana['id']}", json={"is_active": False}, headers=auth()
        )
        as_ana_after = await client.get("/v1/admin/users", headers=auth(session_token))
        reactivated = await client.patch(
            f"/v1/admin/users/{ana['id']}", json={"is_active": True}, headers=auth()
        )
        empty = await client.patch(f"/v1/admin/users/{ana['id']}", json={}, headers=auth())
        fresh_token = await issue_session(
            app.state.session_factory, municipality_id="podgorica", email="ana.novak@example.com"
        )
        self_deactivate = await client.patch(
            f"/v1/admin/users/{ana['id']}", json={"is_active": False}, headers=auth(fresh_token)
        )
        self_demote = await client.patch(
            f"/v1/admin/users/{ana['id']}", json={"role": "expert"}, headers=auth(fresh_token)
        )
        promote_bob = await client.patch(
            f"/v1/admin/users/{bob['id']}",
            json={"role": "admin", "display_name": "Bob"},
            headers=auth(fresh_token),
        )
        missing = await client.patch(
            "/v1/admin/users/999999", json={"role": "admin"}, headers=auth()
        )

    assert ana["email"] == "ana.novak@example.com" and ana["role"] == "admin"
    assert ana["display_name"] == "Ana" and ana["is_active"] is True and ana["open_sessions"] == 0
    assert "password" not in ana
    assert duplicate.status_code == 409
    assert (
        invalid.status_code == 422
        and bad_role.status_code == 422
        and with_password.status_code == 422
    )
    assert [u["email"] for u in listed] == ["ana.novak@example.com", "bob@example.com"]
    assert as_ana.status_code == 200
    assert before["open_sessions"] == 1
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False and deactivated.json()["open_sessions"] == 0
    assert as_ana_after.status_code == 401  # sessions revoked with the account
    assert reactivated.json()["is_active"] is True
    assert empty.status_code == 422
    assert self_deactivate.status_code == 409 and self_demote.status_code == 409
    assert promote_bob.status_code == 200 and promote_bob.json()["role"] == "admin"
    assert missing.status_code == 404
    actions = [a["action"] for a in await audit_actions(app, "staff_user", ana["id"])]
    assert actions == ["user.create", "user.deactivate", "user.reactivate"]
    bob_actions = await audit_actions(app, "staff_user", bob["id"])
    assert bob_actions[-1]["action"] == "user.update"
    assert bob_actions[-1]["actor"] == "ana.novak@example.com"
    assert bob_actions[-1]["details"]["changed"] == ["display_name", "role"]
