"""Market-data imports on PostGIS, through the API with eager Celery and fake storage: a client
range sheet uploaded, imported and normalised into pending inputs (nothing on the panel yet);
approve / amend / reject writing assumption versions with provenance and effective dates; a
zone without assumptions waiting for all four metrics; the range refusals; pasted listings; the
coverage table; the audit trail. The LLM stays off (``MARKET_NORMALISE_LLM=never``): the
rules path is what runs here, the LLM step is covered with a scripted model in
``tests/test_market_import.py``."""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from jobs.base import SqlJobStore, configure_job_store
from jobs.tasks.market import configure_market
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import CLEANUP as PIPELINE_CLEANUP
from tests.integration.test_admin_pipeline_postgis import FakeStorage

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
REVIEWER = "reviewer-token-1234"
SOURCE = "Monmaks range sheet (test)"
CLEANUP = (
    "DELETE FROM market_data",
    "DELETE FROM market_imports",
    "DELETE FROM financial_assumptions WHERE created_by <> 'seed'",
    "UPDATE financial_assumptions SET is_current = true, retired_at = NULL, retired_by = NULL "
    "WHERE created_by = 'seed'",
    *PIPELINE_CLEANUP,
)
MONSTAT = (
    "Tabela 1. Prosječne cijene novoizgrađenih stanova, IV kvartal 2025 (sintetički)\n"
    "Opština;Prosječna cijena (€/m2);Troškovi gradnje (€/m2)\n"
    "Podgorica;1.905,50;1.180\n"
    "Nikšić;1.050;760\n"
).encode("cp1250")


class MarketStorage(FakeStorage):
    def get_bytes(self, key: str) -> bytes:
        return self.objects[key][0]


def range_sheet() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Tržišni rasponi po zonama — stanje avgust 2026 (test)"])
    ws.merge_cells("A1:K1")
    ws.append(
        [
            "Zona",
            "Zemljište €/m²",
            None,
            None,
            "Gradnja €/m²",
            None,
            None,
            "Projektovanje €/m²",
            "Prodaja €/m²",
            None,
            None,
        ]
    )
    for merged in ("B2:D2", "E2:G2", "I2:K2"):
        ws.merge_cells(merged)
    ws.append(
        [
            None,
            "Min",
            "Očekivano",
            "Max",
            "Min",
            "Očekivano",
            "Max",
            None,
            "Min",
            "Očekivano",
            "Max",
        ]
    )
    ws.append(["Centar", 1150, 1380, 1650, 760, 870, 990, 92, 2200, 2550, 2900])
    ws.append(["Stari Aerodrom", 760, 910, 1060, 710, 790, 910, 86, 1480, 1680, 1920])
    ws.append(["Zagorič", 300, 400, 500, 650, 720, 800, 80, 1100, 1250, 1400])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


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


@pytest.fixture
def market_app(postgis_url, monkeypatch):
    from jobs.celery_app import celery_app
    from jobs.enqueue import CeleryDispatcher

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    configure_job_store(SqlJobStore(database_url=postgis_url))
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops,{REVIEWER}:reviewer:rev",
        market_normalise_llm="never",
        market_min_listings=3,
    )
    configure_market(database_url=postgis_url, settings=settings)
    storage = MarketStorage()
    yield make_app(settings, storage=storage, admin_dispatcher=CeleryDispatcher())
    configure_job_store(None)
    configure_market(database_url=None, settings=None)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def upload(client, name: str, data: bytes, mime: str) -> int:
    response = await client.post(
        "/v1/admin/files",
        files={"file": (name, data, mime)},
        data={"kind": "market_data"},
        headers=auth(),
    )
    assert response.status_code == 201, response.text
    return response.json()["file"]["id"]


async def import_sheet(client) -> dict:
    file_id = await upload(
        client,
        "rasponi.xlsx",
        range_sheet(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response = await client.post(
        "/v1/admin/market/imports",
        json={
            "file_id": file_id,
            "kind": "client_ranges",
            "source": SOURCE,
            "retrieved_on": "2026-09-24",
        },
        headers=auth(),
    )
    assert response.status_code == 202, response.text
    return response.json()


async def items(client, **params) -> dict:
    response = await client.get(
        "/v1/admin/review/market-inputs", params=params, headers=auth(REVIEWER)
    )
    assert response.status_code == 200, response.text
    return response.json()


def item_for(page: dict, zone_id: int, metric: str) -> dict:
    return next(i for i in page["items"] if i["zone_id"] == zone_id and i["metric"] == metric)


async def query(app, sql: str, **params) -> list[dict]:
    async with app.state.session_factory() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


async def test_a_range_sheet_is_imported_for_review_and_approval_writes_a_version(market_app):
    app = market_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        accepted = await import_sheet(client)
        imported = accepted["market_import"]
        assert accepted["created"] is True and accepted["job"]["status_url"]
        job = (await client.get(accepted["job"]["status_url"], headers=auth())).json()
        assert job["type"] == "import_market_data" and job["status"] == "succeeded", job
        detail = (
            await client.get(f"/v1/admin/market/imports/{imported['id']}", headers=auth())
        ).json()
        assert detail["status"] == "normalised" and detail["normaliser"] == "rules"
        assert detail["items"]["pending"] == 8  # 2 zones x 4 metrics
        assert detail["report"]["skipped_by_reason"]["no_zone_match"] == 1  # Zagorič
        assert detail["raw"]["format"] == "xlsx" and detail["kind"] == "client_ranges"
        again = await client.post(
            "/v1/admin/market/imports",
            json={
                "file_id": imported["file_id"],
                "kind": "client_ranges",
                "source": SOURCE,
                "retrieved_on": "2026-09-24",
            },
            headers=auth(),
        )
        assert again.status_code == 200 and again.json()["created"] is False
        assert again.json()["job"] is None

        queue = await items(client, status="pending")
        assert queue["total"] == 8 and queue["counts"]["pending"] == 8
        sale = item_for(queue, 1, "sale_rate")
        assert sale["item_type"] == "market_input" and sale["import_kind"] == "client_ranges"
        assert sale["imported"] == {"low": 2200.0, "expected": 2550.0, "high": 2900.0}
        assert sale["source"] == SOURCE and sale["source_date"] == "2026-08-31"
        assert sale["current"]["expected"] == 2450  # the zone's current figure, to compare
        assert sale["raw"]["cells"][0] == "Centar" and sale["status"] == "pending"

        # nothing unreviewed reaches the panel
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        assert panel["market_inputs"]["sale_rate_eur_m2"] == 2450

        approved = await client.post(
            f"/v1/admin/review/market-inputs/{sale['id']}/approve",
            json={"effective_from": "2026-09-25", "note": "matches the sheet"},
            headers=auth(REVIEWER),
        )
        assert approved.status_code == 200, approved.text
        approved = approved.json()
        assert approved["status"] == "approved" and approved["applied_assumption_id"]
        assert approved["effective_from"] == "2026-09-25"
        version = (
            await client.get(
                f"/v1/admin/assumptions/{approved['applied_assumption_id']}", headers=auth()
            )
        ).json()
        assert (version["zone_id"], version["version"], version["supersedes_id"]) == (1, 2, 1)
        assert version["sale_rate"] == {
            "expected": 2550.0,
            "low": 2200.0,
            "high": 2900.0,
            "kind": "absolute",
        }
        assert version["land_rate"]["expected"] == 1350.0  # carried over from the seed row
        assert version["effective_from"] == "2026-09-25"
        provenance = version["rate_sources"]["sale_rate"]
        assert provenance["market_data_id"] == sale["id"] and provenance["source"] == SOURCE
        assert version["rate_sources"]["land_rate"]["set_by"] == "admin"

        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 1})).json()
        market = panel["market_inputs"]
        assert market["sale_rate_eur_m2"] == 2550
        assert market["ranges"]["sale_rate"] == {
            "expected": 2550.0,
            "low": 2200.0,
            "high": 2900.0,
            "kind": "absolute",
        }
        assert market["version"]["effective_from"].startswith("2026-09-25")
        parcel = (await client.get("/v1/parcels/1001/panel")).json()
        assert parcel["market"]["sale_price_eur_m2"]["expected"] == 2550
        assert parcel["market"]["source"] == SOURCE
        assert parcel["market"]["source_date"] == "2026-08-31"

        closed = await client.post(
            f"/v1/admin/review/market-inputs/{sale['id']}/reject",
            json={"note": "too late"},
            headers=auth(REVIEWER),
        )
        assert closed.status_code == 409
        assert closed.json()["error"]["details"]["reason"] == "applied"

        land = item_for(queue, 1, "land_rate")
        amended = await client.post(
            f"/v1/admin/review/market-inputs/{land['id']}/amend",
            json={"expected": 1400, "low": 1200, "high": 1600, "note": "client correction"},
            headers=auth(REVIEWER),
        )
        assert amended.status_code == 200, amended.text
        amended = amended.json()
        assert amended["status"] == "amended"
        assert amended["imported"]["expected"] == 1380  # the imported figure stays
        assert amended["effective"] == {"low": 1200.0, "expected": 1400.0, "high": 1600.0}
        v3 = (
            await client.get(
                f"/v1/admin/assumptions/{amended['applied_assumption_id']}", headers=auth()
            )
        ).json()
        assert v3["version"] == 3 and v3["land_rate"]["expected"] == 1400.0
        assert v3["sale_rate"]["expected"] == 2550.0  # the approved sale price carried over

        build = item_for(queue, 1, "build_rate")
        rejected = await client.post(
            f"/v1/admin/review/market-inputs/{build['id']}/reject",
            json={"note": "construction costs come from Monstat"},
            headers=auth(REVIEWER),
        )
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        assert rejected.json()["applied_assumption_id"] is None

        counts = (await items(client, zone_id=1))["counts"]
        assert counts == {"pending": 1, "approved": 1, "amended": 1, "rejected": 1, "applied": 2}
        forbidden = await client.post("/v1/admin/market/imports", json={}, headers=auth(REVIEWER))
        assert forbidden.status_code == 403

    audit = await query(
        app,
        "SELECT action, entity_type, entity_id, before, after FROM audit_log "
        "WHERE municipality_id = 'podgorica' AND ("
        "(entity_type = 'market_import' AND entity_id = :import_id) "
        "OR (entity_type = 'market_data' AND entity_id = ANY(:item_ids)) "
        "OR (entity_type = 'financial_assumptions' AND entity_id = ANY(:versions))) ORDER BY id",
        import_id=imported["id"],
        item_ids=[i["id"] for i in queue["items"]],
        versions=[approved["applied_assumption_id"], amended["applied_assumption_id"]],
    )
    actions = [row["action"] for row in audit]
    assert actions[0] == "market.import"
    assert actions.count("assumptions.market_input") == 2
    assert {"market.approve", "market.amend", "market.reject"} <= set(actions)
    reject_row = next(r for r in audit if r["action"] == "market.reject")
    assert reject_row["before"]["status"] == "pending"
    assert reject_row["after"]["status"] == "rejected"


async def test_a_zone_without_assumptions_waits_for_all_four_metrics(market_app):
    app = market_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await import_sheet(client)
        # the zone loses its market data: no figures, never the municipality-wide row's
        retired = await client.delete("/v1/admin/assumptions/2", headers=auth())
        assert retired.status_code == 200
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 3})).json()
        assert panel["market_inputs"]["available"] is False
        assert panel["market_inputs"]["reason_code"] == "no_market_data"
        parcel = (await client.get("/v1/parcels/1002/panel")).json()
        assert parcel["market"] is None

        queue = await items(client, zone_id=2)
        metrics = ["sale_rate", "land_rate", "build_rate", "design_rate"]
        for n, metric in enumerate(metrics, start=1):
            item = item_for(queue, 2, metric)
            response = await client.post(
                f"/v1/admin/review/market-inputs/{item['id']}/approve", headers=auth(REVIEWER)
            )
            assert response.status_code == 200, response.text
            body = response.json()
            if n < 4:
                assert body["applied_assumption_id"] is None
                assert sorted(body["waiting_for"]) == sorted(metrics[n:])
            else:
                assert body["applied_assumption_id"] is not None and body["waiting_for"] is None
        coverage = (await client.get("/v1/admin/market/coverage", headers=auth(REVIEWER))).json()
        zone2 = next(z for z in coverage["zones"] if z["zone_id"] == 2)
        assert zone2["market_data"] is True and zone2["sale_price_reviewed"] is True
        assert zone2["sale_price_eur_m2"] == {"low": 1480.0, "expected": 1680.0, "high": 1920.0}
        assert zone2["sale_price_source"] == SOURCE
        assert zone2["sale_price_source_date"] == "2026-08-31"
        assert all(rate["status"] == "current" for rate in zone2["rates"])
        zone1 = next(z for z in coverage["zones"] if z["zone_id"] == 1)
        assert zone1["sale_price_reviewed"] is False  # still the seeded figures
        assert {r["status"] for r in zone1["rates"]} == {"current"}
        assert all(r["pending_item_ids"] for r in zone1["rates"])
        assert coverage["zones_with_market_data"] == 2 and coverage["pending_items"] == 4
        panel = (await client.get("/v1/panel", params={"type": "urban", "id": 3})).json()
        assert panel["market_inputs"]["sale_rate_eur_m2"] == 1680


async def test_single_figures_need_a_range_before_approval(market_app):
    app = market_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.delete("/v1/admin/assumptions/2", headers=auth())  # zone 2: no factors
        file_id = await upload(client, "monstat.csv", MONSTAT, "text/csv")
        accepted = await client.post(
            "/v1/admin/market/imports",
            json={"file_id": file_id, "kind": "statistics", "retrieved_on": "2026-09-24"},
            headers=auth(),
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["market_import"]["source"] == "Monstat"  # the profile's source
        queue = await items(client, metric="sale_rate")
        centar, aerodrom = item_for(queue, 1, "sale_rate"), item_for(queue, 2, "sale_rate")
        assert centar["range_basis"] == "derived" and "municipality_level" in centar["flags"]
        assert (centar["imported"]["low"], centar["imported"]["high"]) == (1638.73, 2191.32)
        assert centar["source_date"] == "2025-12-31"
        assert aerodrom["range_basis"] == "unavailable" and aerodrom["imported"]["low"] is None

        refused = await client.post(
            f"/v1/admin/review/market-inputs/{aerodrom['id']}/approve", headers=auth(REVIEWER)
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["details"]["reason"] == "range_required"
        no_factors = await client.post(
            f"/v1/admin/review/market-inputs/{aerodrom['id']}/amend",
            json={"expected": 1900},
            headers=auth(REVIEWER),
        )
        assert no_factors.status_code == 422
        widened = await client.post(
            f"/v1/admin/review/market-inputs/{aerodrom['id']}/amend",
            json={"expected": 1900, "low": 1700, "high": 2100},
            headers=auth(REVIEWER),
        )
        assert widened.status_code == 200, widened.text
        assert widened.json()["waiting_for"]  # zone 2 has no assumptions: the rest must come
        future = await client.post(
            f"/v1/admin/review/market-inputs/{centar['id']}/approve",
            json={"effective_from": (date.today() + timedelta(days=30)).isoformat()},
            headers=auth(REVIEWER),
        )
        assert future.status_code == 422
        flagged = await items(client, flag="municipality_level")
        zones = (await client.get("/v1/admin/market/coverage", headers=auth())).json()
        # a municipality-wide figure is offered to every zone, each for its own review
        assert flagged["total"] == 2 * zones["zones_total"]


async def test_pasted_listings_become_one_input_per_zone(market_app):
    app = market_app
    listings = "\n".join(
        [
            "Centar; 2.400; 02.09.2026",
            "Centar grada; 2.100; 05.09.2026",
            "Centar, Njegoševa; 2.650; 06.09.2026",
            "Stari Aerodrom; 1.700; 11.09.2026",
            "Negdje; 1.500; 12.09.2026",
        ]
    )
    async with app.router.lifespan_context(app), make_client(app) as client:
        response = await client.post(
            "/v1/admin/market/listings",
            json={"source": "Realitica", "retrieved_on": "2026-09-24", "listings": listings},
            headers=auth(),
        )
        assert response.status_code == 202, response.text
        detail = (
            await client.get(
                f"/v1/admin/market/imports/{response.json()['market_import']['id']}",
                headers=auth(),
            )
        ).json()
        assert detail["kind"] == "listings" and detail["items"]["pending"] == 1
        skipped = detail["report"]["skipped_by_reason"]
        assert skipped == {"too_few_listings": 1, "no_zone_match": 1}
        (item,) = (await items(client, import_id=detail["id"]))["items"]
        assert (item["zone_id"], item["metric"], item["range_basis"]) == (
            1,
            "sale_rate",
            "listings",
        )
        assert item["imported"] == {"low": 2250.0, "expected": 2400.0, "high": 2525.0}
        assert "asking_prices" in item["flags"] and item["source"] == "Realitica"
        empty = await client.post(
            "/v1/admin/market/listings",
            json={"source": "Realitica", "retrieved_on": "2026-09-24", "listings": "\n\n"},
            headers=auth(),
        )
        assert empty.status_code == 422
