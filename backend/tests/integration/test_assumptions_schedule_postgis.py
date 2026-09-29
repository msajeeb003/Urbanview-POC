"""Effective-dated financial assumptions on PostGIS (the admin console's Financial assumptions
screen): a set saved for today reaches the public panels on the next load, a set dated later waits
for its date (the panel cache key follows; each version says until when it applies), the audit row
holds the old and the new figures, the dates and figures are validated all or nothing, and the
formula versions name what the engine runs."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from core.engine.shared import FORMULA_VERSION
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

ADMIN = "admin-token-1234"
REVIEWER = "reviewer-token-5678"
CLEANUP = (
    "DELETE FROM financial_assumptions WHERE created_by <> 'seed'",
    "UPDATE financial_assumptions SET is_current = true, retired_at = NULL, retired_by = NULL, "
    "effective_from = (created_at AT TIME ZONE 'UTC')::date WHERE created_by = 'seed'",
)
PARCEL = 1001  # cadastral parcel in zone 1 (Centar), planned parcel UP 12 on top of it


@pytest.fixture
def app(postgis_url):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{ADMIN}:admin:ops,{REVIEWER}:reviewer:rev",
    )
    return make_app(settings)


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
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


def auth(token: str = ADMIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def zone_set(zone_id: int = 1, build: float = 950, sale: float = 2700, **extra) -> dict:
    return {
        "zone_id": zone_id,
        "land_rate": {"expected": 1350},
        "build_rate": {"expected": build},
        "design_rate": {"expected": 90},
        "sale_rate": {"expected": sale, "low": sale - 200, "high": sale + 200},
        "source": "Realitica, Estitor (test)",
        **extra,
    }


async def today_of(client) -> date:
    listing = (await client.get("/v1/admin/assumptions", headers=auth())).json()
    return date.fromisoformat(listing["today"])


def assumption(panel: dict, key: str) -> dict:
    return next(item for item in panel["assumptions"]["items"] if item["key"] == key)


def figure(panel: dict, key: str) -> dict:
    return next(item for item in panel["group2"]["fields"] if item["key"] == key)


async def rows(app, sql: str, **params) -> list[dict]:
    async with app.state.session_factory() as session:
        result = await session.execute(text(sql), params)
        return [dict(r) for r in result.mappings()]


async def test_a_set_saved_for_today_reaches_the_panels_on_the_next_load(app):
    async with app.router.lifespan_context(app), make_client(app) as client:
        today = await today_of(client)
        before = (await client.get(f"/v1/parcels/{PARCEL}/panel")).json()
        saved = await client.post(
            "/v1/admin/assumptions/batch",
            json={"effective_from": today.isoformat(), "sets": [zone_set(saleable_share=0.75)]},
            headers=auth(),
        )
        assert saved.status_code == 201, saved.text
        created = saved.json()["items"][0]
        after = (await client.get(f"/v1/parcels/{PARCEL}/panel")).json()
        urban = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        listing = (await client.get("/v1/admin/assumptions", headers=auth())).json()
        history = (
            await client.get(
                "/v1/admin/assumptions",
                params={"zone_id": 1, "include_history": "true"},
                headers=auth(),
            )
        ).json()["items"]

    assert (created["version"], created["status"], created["effective_from"]) == (
        2,
        "live",
        today.isoformat(),
    )
    assert created["saleable_share"] == 0.75 and created["supersedes_id"] == 1
    assert before["market"]["version"]["id"] == 1
    assert assumption(before, "construction_cost_eur_m2")["value"] == 860
    assert assumption(before, "saleable_share") == {
        **assumption(before, "saleable_share"),
        "value": 0.7,
        "source": "product_default",
    }

    assert after["market"]["version"]["id"] == created["id"]
    assert assumption(after, "construction_cost_eur_m2")["value"] == 950
    assert assumption(after, "saleable_share")["value"] == 0.75
    assert assumption(after, "saleable_share")["source"] == "market"
    assert after["engine"]["inputs"]["assumptions"]["saleable_share"] == 0.75
    saleable = figure(after, "saleable_area_m2")["expected"]
    gfa = figure(before, "saleable_area_m2")["expected"] / 0.7
    assert saleable == pytest.approx(gfa * 0.75, abs=0.05)
    assert figure(after, "revenue_eur")["expected"] == pytest.approx(saleable * 2700, rel=1e-5)
    assert urban["assumptions"]["saleable_share"] == 0.75
    assert urban["assumptions"]["construction_cost_eur_m2"] == 950

    statuses = {(item["zone_id"], item["version"]): item["status"] for item in listing["items"]}
    assert statuses == {(1, 2): "live", (2, 1): "live"}
    assert [(h["version"], h["status"], h["is_current"]) for h in history] == [
        (2, "live", True),
        (1, "superseded", False),
    ]

    audit = await rows(
        app,
        "SELECT action, before, after, details FROM audit_log WHERE entity_type = "
        "'financial_assumptions' AND entity_id = :id",
        id=created["id"],
    )
    assert [a["action"] for a in audit] == ["assumptions.create"]
    before, after_row = audit[0]["before"], audit[0]["after"]
    assert before["build_rate_eur_m2"] == 860 and after_row["build_rate_eur_m2"] == 950
    assert (before["saleable_share"], after_row["saleable_share"]) == (None, 0.75)
    # the same shape on both sides: only what changed differs
    assert set(before) <= set(after_row)
    changed = {k for k in before if before[k] != after_row[k]}
    assert changed == {
        "id",
        "version",
        "build_rate_eur_m2",
        "sale_rate_eur_m2",
        "sale_rate_low_eur_m2",
        "sale_rate_high_eur_m2",
        "saleable_share",
        "source",
        "source_date",
        "notes",
    }
    assert audit[0]["details"]["effective_from"] == today.isoformat()


async def test_a_future_set_waits_for_its_date(app):
    async with app.router.lifespan_context(app), make_client(app) as client:
        today = await today_of(client)
        first = await client.get(f"/v1/parcels/{PARCEL}/panel")
        scheduled = await client.post(
            "/v1/admin/assumptions",
            json={
                **zone_set(build=1000),
                "effective_from": (today + timedelta(days=3)).isoformat(),
            },
            headers=auth(),
        )
        assert scheduled.status_code == 201, scheduled.text
        scheduled = scheduled.json()
        # a same-day correction saved after the schedule: it applies now, the schedule stays
        correction = (
            await client.post("/v1/admin/assumptions", json=zone_set(build=870), headers=auth())
        ).json()
        waiting = await client.get(f"/v1/parcels/{PARCEL}/panel")
        feasibility = (
            await client.post("/v1/feasibility", json={"parcel_id": 1, "type": "urban"})
        ).json()
        listing = (await client.get("/v1/admin/assumptions", headers=auth())).json()["items"]

        # three days pass: the database's clock cannot move, so this test's versions move back
        # (their dates and the day they were saved; the seeded version stays as it is)
        async with app.state.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE financial_assumptions SET effective_from = effective_from - 3, "
                    "created_at = created_at - interval '3 days' "
                    "WHERE municipality_id = 'podgorica' AND zone_id = 1 AND created_by <> 'seed'"
                )
            )
            await session.commit()
        arrived = await client.get(f"/v1/parcels/{PARCEL}/panel")
        history = (
            await client.get(
                "/v1/admin/assumptions",
                params={"zone_id": 1, "include_history": "true"},
                headers=auth(),
            )
        ).json()["items"]

    assert scheduled["status"] == "scheduled" and scheduled["version"] == 2
    assert (correction["version"], correction["status"]) == (3, "live")
    assert waiting.json()["market"]["version"]["id"] == correction["id"]
    assert assumption(waiting.json(), "construction_cost_eur_m2")["value"] == 870
    assert feasibility["assumptions"]["construction_cost_eur_m2"] == 870
    assert {(i["version"], i["status"]) for i in listing if i["zone_id"] == 1} == {
        (2, "scheduled"),
        (3, "live"),
    }
    # each version says until when it applies: the live one until the scheduled date
    until = {i["version"]: i["effective_to"] for i in listing if i["zone_id"] == 1}
    assert until == {3: (today + timedelta(days=3)).isoformat(), 2: None}
    # the cache key follows what applies: a new set, then the date arriving
    etags = [r.headers["etag"] for r in (first, waiting, arrived)]
    assert len(set(etags)) == 3
    # a later date beats a newer version with an earlier date
    assert arrived.json()["market"]["version"]["id"] == scheduled["id"]
    assert assumption(arrived.json(), "construction_cost_eur_m2")["value"] == 1000
    assert [(h["version"], h["status"]) for h in history] == [
        (3, "superseded"),
        (2, "live"),
        (1, "superseded"),
    ]
    assert history[0]["effective_to"] == history[1]["applies_from"]  # v2 took over from v3
    assert history[1]["effective_to"] is None


async def test_dates_and_figures_are_validated_all_or_nothing(app):
    async with app.router.lifespan_context(app), make_client(app) as client:
        today = await today_of(client)
        past = await client.post(
            "/v1/admin/assumptions",
            json={**zone_set(), "effective_from": (today - timedelta(days=1)).isoformat()},
            headers=auth(),
        )
        far = await client.post(
            "/v1/admin/assumptions",
            json={**zone_set(), "effective_from": (today + timedelta(days=4000)).isoformat()},
            headers=auth(),
        )
        bounds = await client.post(
            "/v1/admin/assumptions",
            json={**zone_set(), "build_rate": {"expected": 900, "low": 950, "high": 1000}},
            headers=auth(),
        )
        share = await client.post(
            "/v1/admin/assumptions", json=zone_set(saleable_share=1.2), headers=auth()
        )
        negative = await client.post(
            "/v1/admin/assumptions",
            json={**zone_set(), "land_rate": {"expected": -5}},
            headers=auth(),
        )
        mixed = await client.post(
            "/v1/admin/assumptions/batch",
            json={
                "sets": [
                    zone_set(zone_id=2),
                    {**zone_set(), "effective_from": (today - timedelta(days=2)).isoformat()},
                ]
            },
            headers=auth(),
        )
        twice = await client.post(
            "/v1/admin/assumptions/batch",
            json={"sets": [zone_set(), zone_set(sale=2800)]},
            headers=auth(),
        )
        reviewer = await client.post(
            "/v1/admin/assumptions/batch", json={"sets": [zone_set()]}, headers=auth(REVIEWER)
        )
        scheduled = (
            await client.post(
                "/v1/admin/assumptions",
                json={**zone_set(), "effective_from": (today + timedelta(days=2)).isoformat()},
                headers=auth(),
            )
        ).json()
        retire_scheduled = await client.delete(
            f"/v1/admin/assumptions/{scheduled['id']}", headers=auth()
        )

    assert past.status_code == 422 and "today" in past.text
    assert far.status_code == 422 and "five years" in far.text
    assert bounds.status_code == 422 and "low" in bounds.text
    assert share.status_code == 422 and negative.status_code == 422
    assert mixed.status_code == 422
    assert mixed.json()["error"]["details"][0]["loc"] == ["body", "sets", 1, "effective_from"]
    assert twice.status_code == 422 and "twice" in twice.text
    assert reviewer.status_code == 403
    assert retire_scheduled.status_code == 409
    assert retire_scheduled.json()["error"]["details"]["reason"] == "not_live"
    written = await rows(app, "SELECT id FROM financial_assumptions WHERE created_by <> 'seed'")
    assert [r["id"] for r in written] == [scheduled["id"]]  # nothing of the refused batches


async def test_the_formula_versions_name_what_the_engine_runs(app):
    async with app.router.lifespan_context(app), make_client(app) as client:
        listed = await client.get("/v1/admin/formulas", headers=auth())
        as_reviewer = await client.get("/v1/admin/formulas", headers=auth(REVIEWER))
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()

    assert listed.status_code == 200, listed.text
    (current,) = [f for f in listed.json() if f["is_current"]]
    assert current["label"] == FORMULA_VERSION == panel["formula_version"]
    assert current["engine_package"] == "@urbanview/feasibility-engine"
    assert current["effective_from"] == "2026-09-23" and current["approval_note"]
    assert as_reviewer.status_code == 403  # the A5 screen is the admins'
