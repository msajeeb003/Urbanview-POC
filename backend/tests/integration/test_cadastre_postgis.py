"""The cadastral base loader on PostGIS: ogr2ogr import of the sample extract, validation, a
versioned staged dataset with its diff, the publish job applying it (stable ids, retired parcels,
the KO table, ownership layers marked unavailable), the KO + number lookup, refusals.

The import runs through the CLI (``core.cadastre.__main__``) with a test profile whose UZN source
is confirmed; the sample KOs ("Test KO Alpha" / "Test KO Beta") are not in the seeded sample, so
retiring stays inside them. Needs ogr2ogr (skipped otherwise).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.cadastre import __main__ as cli
from core.cadastre.config import CadastreProfile
from core.cadastre.ogr import find_ogr2ogr
from core.gis.inspect_gis import gdal_env
from tests.helpers import make_client
from tests.integration.test_publish_postgis import (  # noqa: F401 - fixtures
    publish,
    publish_env,
    reject_seeded_pending_item,
    reset_publish_state,
    rows,
    storage,
    tiles,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(find_ogr2ogr() is None, reason="ogr2ogr not available"),
]

SAMPLES = Path(__file__).resolve().parent.parent / "cadastre"
PROFILE = CadastreProfile.model_validate(
    {
        "source": "uzn_geoportal",
        "ownership_source": "ekatastar",
        "coverage_cell_m": 2000,
        "sources": {
            "uzn_geoportal": {
                "name": "Geoportal UZN",
                "url": "https://webmap.uzn.me/geoportal01",
                "kind": "parcels",
                "access": "confirmed",
                "access_basis": "test agreement",
                "licence_note": "test licence",
                "fields": {
                    "ko_code": "KO_SIFRA",
                    "ko_name": "KO_NAZIV",
                    "parcel_number": "BROJ_PARC",
                    "sub_number": "PODBROJ",
                    "street_address": ["ULICA", "KUCNI_BROJ"],
                    "fid": "FID_UZN",
                },
                "ko_fields": {"ko_code": "KO_SIFRA", "ko_name": "KO_NAZIV"},
            },
            "ekatastar": {
                "name": "eKatastar",
                "kind": "ownership",
                "ownership_fields": {
                    "ko_name": "KO",
                    "parcel_number": "BROJ",
                    "public_ownership": "JAVNA",
                },
            },
        },
    }
)
CLEANUP = (
    "DELETE FROM cadastral_datasets WHERE municipality_id = 'podgorica'",
    "DELETE FROM cadastral_parcels WHERE ko_name LIKE 'Test KO %'",
    "DELETE FROM cadastral_municipalities WHERE ko_name LIKE 'Test KO %'",
    "UPDATE cadastral_parcels SET retired_at = NULL, retired_dataset_version = NULL "
    "WHERE retired_at IS NOT NULL",
    # the seeded sample states its flags (as a loaded eKatastar extract would)
    "UPDATE cadastral_parcels SET public_ownership = (id = 1004), "
    "restitution_or_legal_burden = (id = 1005) WHERE ko_name NOT LIKE 'Test KO %'",
)


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


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
    await _cleanup(postgis_url)
    yield
    await _cleanup(postgis_url)


@pytest.fixture
def run(postgis_url, monkeypatch):
    """The CLI against the test database and the test profile, in a worker thread (it runs its
    own event loop)."""
    monkeypatch.setattr(cli, "load_cadastre_profile", lambda m: PROFILE)

    def engine():
        e = create_async_engine(postgis_url, poolclass=NullPool)
        return e, async_sessionmaker(e, expire_on_commit=False)

    monkeypatch.setattr(cli, "_engine", engine)

    async def call(*args: str) -> int:
        return await asyncio.to_thread(cli.main, ["--municipality", "podgorica", *args])

    return call


def gpkg_export(tmp_path: Path) -> Path:
    """The v1 sample as UZN might deliver it: one GeoPackage with a parcel and a KO layer."""
    exe = find_ogr2ogr()
    assert exe is not None
    target = tmp_path / "uzn-export.gpkg"
    for source, layer, extra in (
        (SAMPLES / "parcels_v1.geojson", "parcels", []),
        (SAMPLES / "ko_boundaries.geojson", "ko", ["-update"]),
    ):
        subprocess.run(  # noqa: S603
            [exe, "-f", "GPKG", *extra, str(target), str(source), "-nln", layer],
            check=True,
            env=gdal_env(exe),
            capture_output=True,
        )
    return target


def report(folder: Path) -> dict:
    return json.loads((folder / "report.json").read_text(encoding="utf-8"))


async def expected_point(app, x: float, y: float) -> tuple[float, float]:
    """Where PostGIS puts an ETRS89 / UTM 34N point in EPSG:4326."""
    (row,) = await rows(
        app,
        "SELECT ST_X(p) AS lng, ST_Y(p) AS lat FROM (SELECT ST_Transform(ST_SetSRID("
        "ST_MakePoint(:x, :y), 25834), 4326) AS p) t",
        x=x,
        y=y,
    )
    return row["lng"], row["lat"]


async def test_import_publish_lookup_reimport_and_retire(run, publish_env, tiles, tmp_path):  # noqa: F811 - imported fixtures
    export = gpkg_export(tmp_path)
    code = await run(
        "import",
        "--file",
        str(export),
        "--layer",
        "parcels",
        "--ko-layer",
        "ko",
        "--retrieved-on",
        "2026-10-02",
        "--label",
        "cadtest-1",
        "--out",
        str(tmp_path / "r1"),
        "--by",
        "tester",
    )
    assert code == 0
    first = report(tmp_path / "r1")
    assert first["status"] == "staged"
    assert first["provenance"]["file_sha256"] == hashlib.sha256(export.read_bytes()).hexdigest()
    assert first["provenance"]["retrieved_at"] == "2026-10-02T00:00:00+00:00"
    assert first["provenance"]["licence_note"] == "test licence"
    assert first["provenance"]["source_crs"] == "EPSG:25834"
    assert first["ownership"]["status"] == "not_available"
    v = first["validation"]
    assert v["errors"] == [] and v["stats"]["records"] == 6 and v["stats"]["repaired"] == 1
    assert {"repaired", "coverage", "missing_kos"} <= {w["code"] for w in v["warnings"]}
    assert first["diff"]["previous_version"] is None
    assert first["diff"]["totals"]["added"] == 6 and first["diff"]["csv_rows"] == 6
    assert (
        (tmp_path / "r1" / "report.md")
        .read_text(encoding="utf-8")
        .startswith("# Cadastral dataset cadtest-1: staged")
    )

    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        (dataset,) = await rows(app, "SELECT * FROM cadastral_datasets")
        assert dataset["status"] == "staged" and dataset["parcel_count"] == 6
        assert dataset["ko_count"] == 2 and dataset["source_id"] == "uzn_geoportal"
        staged = await rows(
            app,
            "SELECT feature_key, properties FROM staging_geometry WHERE batch_id = :b "
            "ORDER BY feature_key",
            b=dataset["parcels_batch_id"],
        )
        by_key = {r["feature_key"]: r["properties"] for r in staged}
        assert by_key["test ko alpha|1234|5"]["area_m2"] == 600.0  # computed in EPSG:25834
        assert by_key["test ko alpha|1235|"]["area_m2"] == 300.0  # the repaired bow tie
        assert by_key["test ko alpha|1236|1"]["repaired"] is False  # "1236/1" split
        assert by_key["test ko alpha|1234|5"]["public_ownership"] is None
        kos = await rows(
            app,
            "SELECT feature_key, properties->>'boundary_source' AS source FROM staging_geometry "
            "WHERE batch_id = :b ORDER BY 1",
            b=dataset["ko_batch_id"],
        )
        assert [(k["feature_key"], k["source"]) for k in kos] == [
            ("test ko alpha", "delivered"),
            ("test ko beta", "delivered"),
        ]

        await reject_seeded_pending_item(app)
        async with app.state.session_factory() as session:  # no parcel with a loaded flag
            await session.execute(
                text(
                    "UPDATE cadastral_parcels SET public_ownership = NULL, "
                    "restitution_or_legal_burden = NULL"
                )
            )
            await session.commit()
        job = await publish(client, "cad-test-1")
        counts = job["result"]["counts"]
        assert counts["geometry"]["cadastral_parcels"] == 6
        assert counts["geometry"]["cadastral_municipalities"] == 2
        assert counts["cadastre"]["cadastral_datasets"] == 1
        assert counts["cadastre"]["parcels_retired"] == 0
        layers = {layer["id"]: layer for layer in job["result"]["layers"]}
        for layer_id in ("public_ownership", "legal_burdens"):
            assert layers[layer_id]["available"] is False
            assert layers[layer_id]["unavailable_reason"] == "ownership_data_not_loaded"
        assert layers["cadastral_parcels"]["available"] is True
        pointer = (await client.get("/v1/tiles/current")).json()
        assert {x["id"]: x["available"] for x in pointer["layers"]}["public_ownership"] is False

        # the acceptance lookup: KO + number finds the parcel where the export drew it
        r = await client.get(
            "/v1/locate/parcel", params={"ko": "Test KO Alpha", "number": "1234/5"}
        )
        assert r.status_code == 200
        cad = r.json()["cadastral_parcel"]
        assert (cad["ko_name"], cad["parcel_number"], cad["sub_number"]) == (
            "Test KO Alpha",
            "1234",
            "5",
        )
        assert cad["area_m2"] == 600.0 and cad["street_address"] == "Njegoševa 12"
        assert cad["public_ownership"] is None and cad["restitution_or_legal_burden"] is None
        lng, lat = await expected_point(app, 356810, 4700315)
        assert cad["centroid"]["lng"] == pytest.approx(lng, abs=1e-6)
        assert cad["centroid"]["lat"] == pytest.approx(lat, abs=1e-6)
        beta = (
            await client.get("/v1/locate/parcel", params={"ko": "test ko beta", "number": "1234/5"})
        ).json()["cadastral_parcel"]
        assert beta["area_m2"] == 400.0 and beta["parcel_id"] != cad["parcel_id"]
        (served,) = await rows(
            app,
            "SELECT ko_code, dataset_version FROM cadastral_parcels WHERE id = :id",
            id=cad["parcel_id"],
        )
        assert served == {"ko_code": "901", "dataset_version": "cadtest-1"}
        ko_list = (await client.get("/v1/cadastral-municipalities")).json()
        test_kos = {k["ko_name"]: k for k in ko_list["items"] if k["ko_name"].startswith("Test")}
        assert test_kos["Test KO Alpha"]["parcel_count"] == 4
        assert test_kos["Test KO Alpha"]["ko_code"] == "901"
        assert test_kos["Test KO Beta"]["boundary_source"] == "delivered"
        ids_v1 = {
            r["parcel_number"] + "/" + (r["sub_number"] or ""): r["id"]
            for r in await rows(
                app,
                "SELECT id, parcel_number, sub_number FROM cadastral_parcels "
                "WHERE ko_name = 'Test KO Alpha'",
            )
        }

        # the next export: diffed against the published version, applied by the next publish
        code = await run(
            "import",
            "--file",
            str(SAMPLES / "parcels_v2.geojson"),
            "--label",
            "cadtest-2",
            "--out",
            str(tmp_path / "r2"),
        )
        assert code == 0
        second = report(tmp_path / "r2")
        assert second["diff"]["previous_version"] == "cadtest-1"
        assert {k: n for k, n in second["diff"]["totals"].items() if n} == {
            "added": 1,
            "removed": 1,
            "geometry_changed": 1,
            "attributes_changed": 1,
            "unchanged": 3,
        }
        diff_csv = (tmp_path / "r2" / "diff.csv").read_text(encoding="utf-8").splitlines()
        assert len(diff_csv) == 5 and diff_csv[0].startswith("change,ko_name,parcel_number")
        job = await publish(client, "cad-test-2")
        assert job["result"]["counts"]["cadastre"]["parcels_retired"] == 1
        after = {
            r["parcel_number"] + "/" + (r["sub_number"] or ""): r
            for r in await rows(
                app,
                "SELECT id, parcel_number, sub_number, street_address, area_m2, retired_at, "
                "retired_dataset_version FROM cadastral_parcels WHERE ko_name = 'Test KO Alpha'",
            )
        }
        assert after["1234/5"]["id"] == ids_v1["1234/5"]  # unchanged: same Parcel ID
        assert after["1235/"]["id"] == ids_v1["1235/"] and after["1235/"]["area_m2"] == 600.0
        assert after["1236/1"]["street_address"] == "Slobode 5"
        assert after["1234/6"]["retired_dataset_version"] == "cadtest-2"  # kept, not served
        assert after["1237/"]["retired_at"] is None
        gone = await client.get(
            "/v1/locate/parcel", params={"ko": "Test KO Alpha", "number": "1234/6"}
        )
        assert gone.status_code == 200 and gone.json()["cadastral_parcel"] is None
        tiled = {f["properties"]["id"] for f in tiles.layers["cadastral_parcels"]}
        assert ids_v1["1234/6"] not in tiled and ids_v1["1234/5"] in tiled
        datasets = {
            d["dataset_version"]: d["status"]
            for d in await rows(app, "SELECT * FROM cadastral_datasets")
        }
        assert datasets == {"cadtest-1": "superseded", "cadtest-2": "published"}
        ko_list = (await client.get("/v1/cadastral-municipalities")).json()
        alpha = next(k for k in ko_list["items"] if k["ko_name"] == "Test KO Alpha")
        assert alpha["parcel_count"] == 4  # 1234/5, 1235, 1236/1, 1237


async def test_duplicates_and_large_changes_are_refused_and_recorded(run, tmp_path, publish_env):  # noqa: F811 - imported fixtures
    code = await run(
        "import", "--file", str(SAMPLES / "parcels_duplicate.geojson"), "--out", str(tmp_path / "d")
    )
    assert code == 1
    dup = report(tmp_path / "d")
    assert dup["status"] == "invalid"
    (error,) = dup["validation"]["errors"]
    assert error["code"] == "duplicates" and error["count"] == 1
    assert error["samples"][0]["parcel_number"] == "1234" and error["samples"][0]["records"] == 2

    assert (
        await run(
            "import",
            "--file",
            str(SAMPLES / "parcels_v1.geojson"),
            "--label",
            "cadtest-a",
            "--out",
            str(tmp_path / "a"),
        )
        == 0
    )
    # Alpha keeps 1 of its 4 parcels; Beta is not in this export and stays out of scope
    code = await run(
        "import",
        "--file",
        str(SAMPLES / "parcels_v3.geojson"),
        "--label",
        "cadtest-b",
        "--out",
        str(tmp_path / "b"),
    )
    assert code == 1
    refused = report(tmp_path / "b")
    assert "removes 3 of the 4 parcels" in refused["refused"]
    assert refused["diff"]["totals"]["out_of_scope"] == 2
    code = await run(
        "import",
        "--file",
        str(SAMPLES / "parcels_v3.geojson"),
        "--label",
        "cadtest-c",
        "--out",
        str(tmp_path / "c"),
        "--accept-large-change",
    )
    assert code == 0
    app = publish_env()
    async with app.router.lifespan_context(app):
        datasets = {
            d["dataset_version"]: (d["status"], d["parcels_batch_id"] is not None)
            for d in await rows(app, "SELECT * FROM cadastral_datasets")
        }
        batches = await rows(app, "SELECT status FROM geometry_batches ORDER BY id")
    assert datasets.pop("cadtest-a") == ("superseded", True)
    assert datasets.pop("cadtest-b") == ("invalid", False)
    assert datasets.pop("cadtest-c") == ("staged", True)
    (invalid,) = datasets.values()
    assert invalid == ("invalid", False)  # the duplicate import staged nothing
    assert [b["status"] for b in batches] == ["superseded", "superseded", "staged", "staged"]


async def test_no_import_without_confirmed_access(run, tmp_path, monkeypatch, publish_env, capsys):  # noqa: F811 - imported fixtures
    blocked = PROFILE.model_copy(
        update={
            "sources": {
                **PROFILE.sources,
                "uzn_geoportal": PROFILE.sources["uzn_geoportal"].model_copy(
                    update={"access": "not_confirmed"}
                ),
            }
        }
    )
    monkeypatch.setattr(cli, "load_cadastre_profile", lambda m: blocked)
    code = await run("import", "--file", str(SAMPLES / "parcels_v1.geojson"))
    assert code == 2
    assert "no scraping fallback" in capsys.readouterr().err
    # the ownership export needs its own confirmed access too
    monkeypatch.setattr(cli, "load_cadastre_profile", lambda m: PROFILE)
    flags = tmp_path / "flags.csv"
    flags.write_text("KO,BROJ,JAVNA\nTest KO Alpha,1234/5,DA\n", encoding="utf-8")
    code = await run(
        "import", "--file", str(SAMPLES / "parcels_v1.geojson"), "--ownership", str(flags)
    )
    assert code == 2
    app = publish_env()
    async with app.router.lifespan_context(app):
        assert await rows(app, "SELECT id FROM cadastral_datasets") == []
