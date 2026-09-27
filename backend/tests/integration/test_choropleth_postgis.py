"""The heatmap surfaces on PostGIS (``core.choropleth``) through the publish job.

Fixture: two zones (north, south) under one adopted, live document, one urban block in each:
block N holds two planned parcels (1000 m²: FAR 1.0, coverage 40 %, P+2; 3000 m²: FAR 2.0,
coverage 20 %, P+4), block S one parcel that states nothing. Zone north has a market set
(1500 €/m²), zone south none until the test saves one (1250 €/m²).

Checked: the block values by their rules (area-weighted means, the tallest notation, the GFA sum),
no cell for block S and zone south (the tiles carry them without a value: not covered), the sale
price exactly as the assumptions state it in the profile's bands, the pointer's classes are the
stored ones and every tile band follows from them, a market set saved for today rebuilds the
sale-price heatmap and the tiles (``refresh_heatmaps``), the QA command's min / max / mean.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import core.choropleth as choropleth_module
from core.choropleth import band
from tests.helpers import make_client
from tests.integration.test_publish_postgis import (  # noqa: F401 - fixtures
    ADMIN,
    auth,
    publish,
    publish_env,
    reject_seeded_pending_item,
    reset_publish_state,
    rows,
    storage,
    tiles,
)

pytestmark = pytest.mark.integration

PREFIX = "GT heat"
BOX = "ST_Multi(ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326))"
ZONES = {"north": (19.140, 42.500, 19.160, 42.520), "south": (19.140, 42.480, 19.160, 42.500)}
BLOCKS = {
    "N": ("north", (19.145, 42.505, 19.155, 42.515)),
    "S": ("south", (19.145, 42.485, 19.155, 42.495)),
}
PARCELS = {
    "GT P1": (
        "N",
        (19.146, 42.506, 19.148, 42.508),
        1000,
        {"max_far": 1.0, "max_site_coverage_pct": 40, "max_floors": "P+2"},
    ),
    "GT P2": (
        "N",
        (19.149, 42.506, 19.153, 42.510),
        3000,
        {"max_far": 2.0, "max_site_coverage_pct": 20, "max_floors": "P+4"},
    ),
    "GT P3": ("S", (19.146, 42.486, 19.148, 42.488), 800, {}),
}
CLEANUP = (
    "DELETE FROM planning_parameter_values WHERE urban_parcel_id IN "
    "(SELECT id FROM urban_parcels WHERE urban_parcel_number LIKE 'GT P%')",
    "DELETE FROM urban_parcels WHERE urban_parcel_number LIKE 'GT P%'",
    "DELETE FROM urban_blocks WHERE block_ref LIKE 'GT heat %'",
    "DELETE FROM financial_assumptions WHERE zone_id IN "
    "(SELECT id FROM zones WHERE name LIKE 'GT heat %')",
    "DELETE FROM planning_documents WHERE name LIKE 'GT heat %'",
    "DELETE FROM zones WHERE name LIKE 'GT heat %'",
)


def _box(rect) -> dict[str, float]:
    x0, y0, x1, y1 = rect
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


async def _cleanup(postgis_url: str) -> None:
    await reset_publish_state(postgis_url)
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            for statement in CLEANUP:
                await session.execute(text(statement))
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture
async def fixture(postgis_url):
    await _cleanup(postgis_url)
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    ids: dict[str, int] = {}
    try:
        async with async_sessionmaker(engine)() as session:
            for name, rect in ZONES.items():
                ids[name] = (
                    await session.execute(
                        text(
                            "INSERT INTO zones (municipality_id, name, geom) "
                            f"VALUES ('podgorica', :n, {BOX}) RETURNING id"
                        ),
                        {"n": f"{PREFIX} {name}", **_box(rect)},
                    )
                ).scalar_one()
            doc = (
                await session.execute(
                    text(
                        "INSERT INTO planning_documents (municipality_id, name, type, status, "
                        f"coverage_geom, coverage_live, zone_id) VALUES ('podgorica', :n, 'DUP', "
                        f"'adopted', {BOX}, true, :z) RETURNING id"
                    ),
                    {
                        "n": f"{PREFIX} DUP",
                        "z": ids["north"],
                        **_box((19.139, 42.479, 19.161, 42.521)),
                    },
                )
            ).scalar_one()
            ids["document"] = doc
            for ref, (zone, rect) in BLOCKS.items():
                ids[ref] = (
                    await session.execute(
                        text(
                            "INSERT INTO urban_blocks (municipality_id, block_ref, geom, zone_id) "
                            f"VALUES ('podgorica', :r, {BOX}, :z) RETURNING id"
                        ),
                        {"r": f"{PREFIX} {ref}", "z": ids[zone], **_box(rect)},
                    )
                ).scalar_one()
            version = (
                await session.execute(text("SELECT id FROM publish_versions WHERE is_current"))
            ).scalar_one()
            for number, (block, rect, area, values) in PARCELS.items():
                parcel = (
                    await session.execute(
                        text(
                            "INSERT INTO urban_parcels (municipality_id, urban_parcel_number, "
                            f"geom, area_m2, block_id, document_id) VALUES ('podgorica', :n, "
                            f"{BOX}, :a, :b, :d) RETURNING id"
                        ),
                        {"n": number, "a": area, "b": ids[block], "d": doc, **_box(rect)},
                    )
                ).scalar_one()
                for key, value in values.items():
                    await session.execute(
                        text(
                            "INSERT INTO planning_parameter_values (municipality_id, document_id, "
                            "urban_parcel_id, field_key, value_number, value_text, source_page, "
                            "publish_version_id) VALUES ('podgorica', :d, :u, :k, :num, :txt, 1, "
                            ":v)"
                        ),
                        {
                            "d": doc,
                            "u": parcel,
                            "k": key,
                            "num": value if isinstance(value, float | int) else None,
                            "txt": value if isinstance(value, str) else None,
                            "v": version,
                        },
                    )
            await session.commit()
        yield ids
    finally:
        await engine.dispose()
        await _cleanup(postgis_url)


def market(zone_id: int, sale: float) -> dict:
    return {
        "zone_id": zone_id,
        "land_rate": {"expected": 300},
        "build_rate": {"expected": 800},
        "design_rate": {"expected": 60},
        "sale_rate": {"expected": sale},
        "source": "Realitica, Estitor (test)",
    }


async def cells_of(app, version_id: int) -> dict[tuple[str, int], dict]:
    return {
        (r["layer"], r["cell_id"]): r
        for r in await rows(
            app, "SELECT * FROM choropleth_cells WHERE publish_version_id = :v", v=version_id
        )
    }


async def test_heatmaps_are_computed_banded_and_follow_the_assumptions(
    fixture,
    publish_env,  # noqa: F811 - imported fixture
    tiles,  # noqa: F811 - imported fixture
    storage,  # noqa: F811 - imported fixture
    postgis_url,
    monkeypatch,
    capsys,
):
    ids = fixture
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await client.post(
            "/v1/admin/assumptions", json=market(ids["north"], 1500), headers=auth(ADMIN)
        )
        assert r.status_code == 201, r.text
        north_set = r.json()["id"]
        await reject_seeded_pending_item(app)
        job = await publish(client, "heat-1")
        version = job["result"]["version_id"]
        assert job["result"]["counts"]["choropleth_cells"]["far"] >= 3  # the sample's and N

        cells = await cells_of(app, version)
        n = ids["N"]
        assert cells[("far", n)]["value"] == pytest.approx(1.75)  # (1.0 x 1000 + 2.0 x 3000) / 4000
        assert cells[("coverage", n)]["value"] == pytest.approx(
            25.0
        )  # (40 x 1000 + 20 x 3000) / 4000
        assert cells[("gfa", n)]["value"] == pytest.approx(7000.0)  # 1.0 x 1000 + 2.0 x 3000
        assert (cells[("height", n)]["value"], cells[("height", n)]["label"]) == (5, "P+4")
        assert cells[("far", n)]["parcel_count"] == 2 and cells[("far", n)]["unit"] == ""
        assert not any(cell_id == ids["S"] for _, cell_id in cells)  # nothing stated: no cell
        north = cells[("sale_price", ids["north"])]
        assert (north["value"], north["value_band"], north["assumptions_id"]) == (
            1500,
            2,
            north_set,
        )
        (stated,) = await rows(
            app, "SELECT sale_rate_eur_m2 FROM financial_assumptions WHERE id = :i", i=north_set
        )
        assert north["value"] == stated["sale_rate_eur_m2"]  # exactly the market table's figure
        assert ("sale_price", ids["south"]) not in cells

        # the pointer serves the stored classes; every band in the tiles follows from them
        pointer = (await client.get("/v1/tiles/current")).json()
        classes = pointer["cell_classes"]
        stored = {
            r["layer"]: r
            for r in await rows(
                app, "SELECT * FROM choropleth_classes WHERE publish_version_id = :v", v=version
            )
        }
        assert set(stored) == {"coverage", "far", "height", "gfa", "sale_price"}
        for layer, row in stored.items():
            assert (
                classes[layer]["breaks"] == row["breaks"]
                and classes[layer]["method"] == row["method"]
            )
            assert classes[layer]["source_layer"] == f"heat_{layer}"
            features = tiles.layers[f"heat_{layer}"]
            for feature in features:
                props = feature["properties"]
                if "value" in props:
                    assert props["band"] == band(
                        props["value"], row["breaks"], zero_class=layer == "sale_price"
                    ), (layer, props)
        assert classes["sale_price"]["breaks"] == [1300, 1700, 2100]
        assert classes["far"]["null_count"] >= 1 and pointer["heatmaps_refreshing"] is False
        heat_far = {f["id"]: f["properties"] for f in tiles.layers["heat_far"]}
        assert heat_far[n]["value"] == pytest.approx(1.75)
        assert "value" not in heat_far[ids["S"]]  # drawn as not covered, never as zero
        sale = {f["id"]: f["properties"] for f in tiles.layers["heat_sale_price"]}
        assert "value" not in sale[ids["south"]] and sale[ids["north"]]["band"] == 2

        # a market set saved for today: the sale-price heatmap and the tiles follow at once
        runs = len(tiles.runs)
        r = await client.post(
            "/v1/admin/assumptions", json=market(ids["south"], 1250), headers=auth(ADMIN)
        )
        assert r.status_code == 201, r.text
        (refresh,) = await rows(
            app,
            "SELECT status, result FROM pipeline_jobs WHERE type = 'refresh_heatmaps' "
            "AND target_id = :v",
            v=version,
        )
        assert refresh["status"] == "succeeded", refresh
        assert refresh["result"]["archive_rebuilt"] is True
        assert len(tiles.runs) == runs + 1
        cells = await cells_of(app, version)
        south = cells[("sale_price", ids["south"])]
        assert (south["value"], south["value_band"]) == (1250, 1)
        sale = {f["id"]: f["properties"] for f in tiles.layers["heat_sale_price"]}
        assert sale[ids["south"]]["value"] == 1250 and sale[ids["south"]]["band"] == 1
        pointer = (await client.get("/v1/tiles/current")).json()
        assert pointer["heatmaps_refreshing"] is False and pointer["version_id"] == version

    # the QA command: min / max / mean per layer from the stored classes
    monkeypatch.setattr(
        choropleth_module, "_engine", lambda: create_async_engine(postgis_url, poolclass=NullPool)
    )
    capsys.readouterr()  # only the command's own output below
    assert await asyncio.to_thread(choropleth_module.main, ["summary", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["far"]["max"] >= 1.75 and summary["far"]["min"] <= 1.75
    assert summary["sale_price"]["min"] == 1250 and summary["sale_price"]["count"] >= 3
    assert summary["height"]["unit"] == "floors" and summary["gfa"]["mean"] is not None
