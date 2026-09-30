"""Cadastral <-> planned parcel links on PostGIS (``core.parcel_links``, BRD §2.2).

Fixtures drawn in EPSG:25834 (the metric CRS, so the areas are exact) under a test document
(adopted, live) south-west of the sample, one per relation:

- same: 20 x 20 m, the planned parcel 1 % larger (inside the 2 % tolerance);
- reduced, the client's example: cadastral 100 m², planned 75 m² (25 % taken for the road);
- enlarged: cadastral 100 m², planned 120 m² over it and a road strip without a cadastral parcel;
- split: cadastral 200 m² into 120 + 80 (60 % / 40 %), shares summing to 100 %;
- merged: two cadastral parcels of 100 m² in one planned parcel of 200 m²;
- none: no planned parcel; and a 0.5 m² sliver that is ignored (none too);
- a minor second link (5 %): linked, but not a split.

The links of the current version are recomputed with the fixtures in place; the panels
(``/v1/panel`` cadastral and urban, ``/v1/parcels/{id}/panel``) state them; the QA command
summarises them; a full recompute is logged and stored on the version.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import core.parcel_links as links_module
from core.parcel_links import LinkRules, recompute_parcel_links
from tests.helpers import make_app, make_client, make_settings

pytestmark = pytest.mark.integration

ORIGIN = (19.20, 42.36)  # inside the municipality, away from the sample and the synthetic grid
DOC_NAME = "Test DUP links"
KO = "Test KO Links"
# name -> (x0, y0, x1, y1) in metres from the origin, EPSG:25834
CADASTRAL = {
    "same": (0, 0, 20, 20),
    "reduced": (100, 0, 110, 10),
    "enlarged": (200, 0, 210, 10),
    "split": (300, 0, 320, 10),
    "merged_a": (400, 0, 410, 10),
    "merged_b": (410, 0, 420, 10),
    "none": (500, 0, 510, 10),
    "sliver": (600, 0, 610, 10),
    "minor": (700, 0, 720, 10),
}
PLANNED = {
    "UP same": (0, 0, 20, 20.2),
    "UP reduced": (102.5, 0, 110, 10),
    "UP enlarged": (200, 0, 210, 12),
    "UP split 1": (300, 0, 312, 10),
    "UP split 2": (312, 0, 320, 10),
    "UP merged": (400, 0, 420, 10),
    "UP sliver": (609.95, 0, 620, 10),
    "UP minor big": (700, 0, 719, 10),
    "UP minor other": (719, 0, 730, 10),
}
BOX = "ST_Transform(ST_MakeEnvelope(o.e + :x0, o.n + :y0, o.e + :x1, o.n + :y1, 25834), 4326)"
ORIGIN_SQL = (
    "(SELECT ST_X(p) AS e, ST_Y(p) AS n FROM (SELECT ST_Transform(ST_SetSRID("
    f"ST_MakePoint({ORIGIN[0]}, {ORIGIN[1]}), 4326), 25834) AS p) t) o"
)


def _box(rect: tuple[float, float, float, float]) -> dict[str, float]:
    x0, y0, x1, y1 = rect
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


async def _cleanup(session) -> None:
    await session.execute(
        text(
            "DELETE FROM urban_parcels WHERE document_id IN "
            "(SELECT id FROM planning_documents WHERE name = :d)"
        ),
        {"d": DOC_NAME},
    )
    await session.execute(text("DELETE FROM cadastral_parcels WHERE ko_name = :ko"), {"ko": KO})
    await session.execute(text("DELETE FROM planning_documents WHERE name = :d"), {"d": DOC_NAME})


@pytest.fixture
async def fixtures(postgis_url):
    """The fixtures in place and the current version's links recomputed; afterwards removed and
    the links recomputed without them."""
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, int] = {}
    async with factory() as session:
        await _cleanup(session)
        doc = (
            await session.execute(
                text(
                    "INSERT INTO planning_documents (municipality_id, name, type, status, "
                    "coverage_geom, coverage_live) "
                    f"SELECT 'podgorica', :d, 'DUP', 'adopted', ST_Multi({BOX}), true "
                    f"FROM {ORIGIN_SQL} RETURNING id"
                ),
                {"d": DOC_NAME, **_box((-50, -50, 800, 60))},
            )
        ).scalar_one()
        ids["document"] = doc
        for name, rect in CADASTRAL.items():
            ids[name] = (
                await session.execute(
                    text(
                        "INSERT INTO cadastral_parcels (municipality_id, parcel_number, ko_name, "
                        "geom, area_m2) "
                        f"SELECT 'podgorica', :n, :ko, ST_Multi({BOX}), "
                        "(:x1 - :x0) * (:y1 - :y0) "
                        f"FROM {ORIGIN_SQL} RETURNING id"
                    ),
                    {"n": f"L-{name}", "ko": KO, **_box(rect)},
                )
            ).scalar_one()
        for number, rect in PLANNED.items():
            ids[number] = (
                await session.execute(
                    text(
                        "INSERT INTO urban_parcels (municipality_id, urban_parcel_number, geom, "
                        "area_m2, document_id) "
                        f"SELECT 'podgorica', :n, ST_Multi({BOX}), (:x1 - :x0) * (:y1 - :y0), "
                        f":doc FROM {ORIGIN_SQL} RETURNING id"
                    ),
                    {"n": number, "doc": doc, **_box(rect)},
                )
            ).scalar_one()
        version = (
            await session.execute(
                text(
                    "SELECT id FROM publish_versions WHERE municipality_id = 'podgorica' "
                    "AND is_current"
                )
            )
        ).scalar_one()
        ids["version"] = version
        ids["summary"] = await recompute_parcel_links(
            session, municipality_id="podgorica", version_id=version, max_seconds=60
        )
        await session.commit()
    try:
        yield ids, factory
    finally:
        async with factory() as session:
            await _cleanup(session)
            await recompute_parcel_links(
                session, municipality_id="podgorica", version_id=ids["version"]
            )
            await session.commit()
        await engine.dispose()


async def _links(factory, version: int, cadastral_id: int) -> list[dict]:
    async with factory() as session:
        return [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT l.*, u.urban_parcel_number FROM parcel_links l "
                        "LEFT JOIN urban_parcels u ON u.id = l.urban_parcel_id "
                        "WHERE l.publish_version_id = :v AND l.cadastral_parcel_id = :c "
                        "ORDER BY l.rank"
                    ),
                    {"v": version, "c": cadastral_id},
                )
            ).mappings()
        ]


async def test_every_relation_is_classified(fixtures):
    ids, factory = fixtures
    v = ids["version"]

    def one(rows):
        assert len(rows) == 1, rows
        return rows[0]

    same = one(await _links(factory, v, ids["same"]))
    assert same["relation"] == "same" and same["urban_parcel_number"] == "UP same"
    assert same["overlap_ratio_of_cadastral"] == pytest.approx(1.0, abs=1e-6)
    assert same["overlap_ratio_of_urban"] == pytest.approx(400 / 404, abs=1e-6)

    # the client's example: cadastral 100, planned 75 -> reduced, 25 % taken for the road
    reduced = one(await _links(factory, v, ids["reduced"]))
    assert reduced["relation"] == "reduced" and reduced["reduction_pct"] == pytest.approx(25.0)
    assert (reduced["cadastral_area_m2"], reduced["urban_area_m2"]) == (100.0, 75.0)
    assert reduced["overlap_area_m2"] == pytest.approx(75.0, abs=1e-3)
    assert reduced["overlap_ratio_of_cadastral"] == pytest.approx(0.75, abs=1e-6)
    assert reduced["overlap_ratio_of_urban"] == pytest.approx(1.0, abs=1e-6)
    assert reduced["area_delta_m2"] == -25.0 and reduced["rank"] == 1
    assert reduced["dataset_version"]  # the version's label

    enlarged = one(await _links(factory, v, ids["enlarged"]))
    assert enlarged["relation"] == "enlarged" and enlarged["reduction_pct"] == pytest.approx(0)
    assert enlarged["overlap_ratio_of_urban"] == pytest.approx(100 / 120, abs=1e-6)

    split = await _links(factory, v, ids["split"])
    assert [r["urban_parcel_number"] for r in split] == ["UP split 1", "UP split 2"]
    assert {r["relation"] for r in split} == {"split"}
    assert [r["rank"] for r in split] == [1, 2]
    shares = [r["overlap_ratio_of_cadastral"] for r in split]
    assert shares == [pytest.approx(0.6, abs=1e-6), pytest.approx(0.4, abs=1e-6)]
    assert sum(shares) == pytest.approx(1.0, abs=0.01)  # fully covered: 100 % +/- 1 %

    for name in ("merged_a", "merged_b"):
        merged = one(await _links(factory, v, ids[name]))
        assert merged["relation"] == "merged" and merged["urban_parcel_number"] == "UP merged"
        assert merged["overlap_ratio_of_cadastral"] == pytest.approx(1.0, abs=1e-6)
        assert merged["overlap_ratio_of_urban"] == pytest.approx(0.5, abs=1e-6)

    for name in ("none", "sliver"):  # a 0.5 m² sliver is below the 1 m² threshold
        none = one(await _links(factory, v, ids[name]))
        assert none["relation"] == "none" and none["urban_parcel_id"] is None
        assert (none["overlap_area_m2"], none["rank"], none["reduction_pct"]) == (0, 1, None)

    minor = await _links(factory, v, ids["minor"])
    assert [r["urban_parcel_number"] for r in minor] == ["UP minor big", "UP minor other"]
    assert minor[1]["overlap_ratio_of_cadastral"] == pytest.approx(0.05, abs=1e-6)
    # one planned parcel above 10 %: not a split; nothing goes to roads
    assert {r["relation"] for r in minor} == {"reduced"}
    assert minor[0]["reduction_pct"] == pytest.approx(0, abs=0.01)

    summary = ids["summary"]
    assert summary["relations"]["split"] >= 1 and summary["relations"]["merged"] >= 2
    assert summary["no_urban_parcel"] == summary["relations"]["none"] >= 2
    assert summary["merged_urban_parcels"] >= 1 and summary["overcovered_parcels"] == 0
    assert summary["cadastral_unmatched"] == summary["relations"]["none"]


async def test_the_panels_state_the_links(fixtures, postgis_url):
    ids, _ = fixtures
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        panel_cache_ttl_seconds=0,
        rate_limit_requests=100_000,
    )
    app = make_app(settings)
    async with app.router.lifespan_context(app), make_client(app) as client:

        async def cadastral(name: str) -> dict:
            r = await client.get("/v1/panel", params={"type": "cadastral", "id": ids[name]})
            assert r.status_code == 200, r.text
            return r.json()

        split = await cadastral("split")
        assert split["covered"] and split["split"] is True and split["relation"] == "split"
        assert [u["urban_parcel_number"] for u in split["urban_parcels"]] == [
            "UP split 1",
            "UP split 2",
        ]
        assert [u["share_of_cadastral_pct"] for u in split["urban_parcels"]] == [60.0, 40.0]
        assert split["calculation_basis"] == "urban" and split["basis_area_m2"] == 120.0

        reduced = await cadastral("reduced")
        assert (reduced["relation"], reduced["reduction_pct"], reduced["split"]) == (
            "reduced",
            25.0,
            False,
        )
        link = reduced["urban_parcel"]
        assert (link["share_of_cadastral_pct"], link["share_of_urban_pct"]) == (75.0, 100.0)
        areas = reduced["areas"]
        assert (areas["cadastral_area_m2"], areas["urban_parcel_area_m2"], areas["delta_pct"]) == (
            100.0,
            75.0,
            -25.0,
        )

        none = await cadastral("none")
        assert none["covered"] and none["urban_parcel_defined"] is False
        assert (none["relation"], none["no_urban_parcel"]) == ("none", True)
        assert none["calculation_basis"] == "cadastral" and none["urban_parcels"] == []

        r = await client.get("/v1/panel", params={"type": "urban", "id": ids["UP merged"]})
        assert r.status_code == 200, r.text
        urban = r.json()
        linked = urban["identification"]["linked_cadastral_parcels"]
        assert sorted(c["parcel_id"] for c in linked) == sorted([ids["merged_a"], ids["merged_b"]])
        assert {c["relation"] for c in linked} == {"merged"} and urban["relation"] == "merged"
        assert [c["share_of_urban_pct"] for c in linked] == [50.0, 50.0]

        r = await client.get(f"/v1/parcels/{ids['reduced']}/panel")
        assert r.status_code == 200, r.text
        basis = r.json()["header"]["calculation_basis"]
        assert (basis["relation"], basis["reduction_pct"], basis["no_urban_parcel"]) == (
            "reduced",
            25.0,
            False,
        )
        (link,) = basis["links"]
        assert (link["overlap_pct"], link["share_of_urban_pct"], link["relation"]) == (
            75.0,
            100.0,
            "reduced",
        )
        r = await client.get(f"/v1/parcels/{ids['split']}/panel")
        basis = r.json()["header"]["calculation_basis"]
        assert basis["split"] is True and basis["reason"] == "split"
        assert len(basis["links"]) == 2


async def test_qa_command_summarises_and_recompute_is_logged(
    fixtures, postgis_url, monkeypatch, caplog, capsys
):
    ids, factory = fixtures

    def engine():
        return create_async_engine(postgis_url, poolclass=NullPool)

    monkeypatch.setattr(links_module, "_engine", engine)
    assert await asyncio.to_thread(links_module.main, ["summary", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["relations"] == ids["summary"]["relations"]
    assert summary["links"] == ids["summary"]["links"]
    assert summary["average_reduction_pct"] is not None and summary["duration_ms"] >= 0

    # the full recompute (sample and fixtures): logged and stored on the version
    caplog.set_level(logging.INFO, logger="urbanview.parcel_links")
    async with factory() as session:
        result = await recompute_parcel_links(
            session,
            municipality_id="podgorica",
            version_id=ids["version"],
            rules=LinkRules(),
            max_seconds=60,
        )
        await session.commit()
        stored = (
            await session.execute(
                text("SELECT links_summary FROM publish_versions WHERE id = :v"),
                {"v": ids["version"]},
            )
        ).scalar_one()
    assert result["cadastral_parcels"] > 7  # the sample's 7 and the fixtures'
    assert stored["duration_ms"] == result["duration_ms"] and stored["metric_srid"] == 25834
    assert stored["rules"]["split_min_fraction"] == 0.1
    (record,) = [r for r in caplog.records if r.name == "urbanview.parcel_links"]
    assert record.levelno == logging.INFO
    assert f"in {result['duration_ms']} ms" in record.getMessage()
