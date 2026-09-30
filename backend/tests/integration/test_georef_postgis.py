"""Georeferencing on PostGIS (``core.gis.georef``): a synthetic document whose ground truth sits on
the seeded cadastral parcel 1001 (KO Podgorica I 1042) goes through the CLI (control points ->
fit -> apply --stage) with the plan drawn in ETRS89 / UTM 34N (EPSG:25834, the cadastre's metric
CRS). Checked: vertices within 0.5 m moved onto the cadastral vertices, a vertex 1 m off logged as
a near miss and left, the re-parcelled corner left where the plan put it, the snapped ratio per
feature, the parcel-overlap warning, the dataset record with its residual report, the admin
document's summary, the publish job serving the geometry and publishing the dataset, a re-run with
the stored transform reproducing the digest, the carry-forward of other documents' land use, and
the refusals (outside the extent, no cadastral overlap). Needs ogr2ogr (skipped otherwise).
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
from pathlib import Path

import pytest

pytest.importorskip("shapely")

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from shapely import transform as shapely_transform  # noqa: E402
from shapely import wkt  # noqa: E402
from shapely.geometry import MultiPolygon, Polygon  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from core.cadastre.ogr import find_ogr2ogr  # noqa: E402
from core.gis.extract.gpkg import write_gpkg  # noqa: E402
from core.gis.georef import __main__ as cli  # noqa: E402
from core.gis.georef.points import ControlPoint, write_points  # noqa: E402
from core.gis.georef.stage import stage_document  # noqa: E402
from tests import gis_synthetic as syn  # noqa: E402
from tests.helpers import make_client  # noqa: E402
from tests.integration.test_publish_postgis import (  # noqa: E402, F401 - fixtures
    approve_geometry,
    auth,
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

CRS = "EPSG:25834"
THETA = math.radians(0.2)
SHEET_POINTS = [(60, 60), (540, 60), (540, 360), (60, 360), (300, 210), (200, 300)]
BOUNDS = (19.10, 42.33, 19.45, 42.55)
CLEANUP = (
    "DELETE FROM georef_datasets WHERE municipality_id = 'podgorica'",
    "DELETE FROM urban_parcels WHERE document_id IN "
    "(SELECT id FROM planning_documents WHERE name LIKE 'GT %')",
    "DELETE FROM urban_blocks WHERE block_ref LIKE 'GT-%'",
    "DELETE FROM planning_documents WHERE name LIKE 'GT %'",
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
def db(postgis_url):
    """``async with db() as session`` on the test database."""
    engine = create_async_engine(postgis_url, poolclass=NullPool)  # nothing pooled to dispose
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def run(postgis_url, monkeypatch):
    """The CLI against the test database, in a worker thread (it runs its own event loop)."""

    def engine():
        e = create_async_engine(postgis_url, poolclass=NullPool)
        return e, async_sessionmaker(e, expire_on_commit=False)

    monkeypatch.setattr(cli, "_engine", engine)

    async def call(*args: str) -> int:
        return await asyncio.to_thread(cli.main, ["--municipality", "podgorica", *args])

    return call


async def register(db, name: str) -> int:
    async with db() as session:
        doc_id = (
            await session.execute(
                text(
                    "INSERT INTO planning_documents (municipality_id, name, type, status) "
                    "VALUES ('podgorica', :name, 'DUP', 'adopted') RETURNING id"
                ),
                {"name": name},
            )
        ).scalar_one()
        await session.commit()
    return doc_id


async def cadastral_corners(db, parcel_id: int = 1001) -> list[tuple[float, float]]:
    """The seeded parcel's corners in EPSG:25834 (SW, SE, NE, NW)."""
    async with db() as session:
        text_wkt = (
            await session.execute(
                text(
                    "SELECT ST_AsText(ST_Transform(geom, 25834)) FROM cadastral_parcels "
                    "WHERE id = :id"
                ),
                {"id": parcel_id},
            )
        ).scalar_one()
    ring = list(wkt.loads(text_wkt).geoms[0].exterior.coords)
    return [tuple(c) for c in ring[:4]]


async def to_wgs84(db, e: float, n: float) -> tuple[float, float]:
    async with db() as session:
        row = (
            await session.execute(
                text(
                    "SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID("
                    "ST_MakePoint(:e, :n), 25834), 4326) AS p) t"
                ),
                {"e": e, "n": n},
            )
        ).one()
    return float(row[0]), float(row[1])


class Plan:
    """The synthetic plan in EPSG:25834 around cadastral parcel 1001, and its truth transform
    from the document's local frame."""

    def __init__(self, corners: list[tuple[float, float]], shift: tuple[float, float] = (0, 0)):
        sw, se, ne, nw = [(x + shift[0], y + shift[1]) for x, y in corners]
        self.corners = [sw, se, ne, nw]
        self.origin = (sw[0] - 150.0, sw[1] - 120.0)
        # UP 1 follows the cadastral parcel: two corners within 0.5 m (snapped), the north-east
        # corner 1.0 m off (a near miss, left), the north-west corner re-parcelled 5 m inside
        self.up1 = [
            (sw[0] + 0.2, sw[1] + 0.1),
            (se[0] - 0.15, se[1] + 0.2),
            (ne[0] + 0.8, ne[1] + 0.6),
            (nw[0] + 4.0, nw[1] - 3.0),
        ]
        cx, cy = (sw[0] + ne[0]) / 2, (sw[1] + ne[1]) / 2
        self.up2 = [(cx - 3, cy - 3), (cx + 3, cy - 3), (cx + 3, cy + 3), (cx - 3, cy + 3)]
        self.block = [(x + 0.1, y - 0.1) for x, y in (sw, se, ne, nw)]
        self.boundary = [
            (sw[0] - 30, sw[1] - 30),
            (se[0] + 30, se[1] - 30),
            (ne[0] + 30, ne[1] + 30),
            (nw[0] - 30, nw[1] + 30),
        ]

    def truth(self, x: float, y: float) -> tuple[float, float]:
        c, s = math.cos(THETA), math.sin(THETA)
        return (self.origin[0] + c * x - s * y, self.origin[1] + s * x + c * y)

    def inverse(self, e: float, n: float) -> tuple[float, float]:
        c, s = math.cos(THETA), math.sin(THETA)
        de, dn = e - self.origin[0], n - self.origin[1]
        return (c * de + s * dn, -s * de + c * dn)

    def world(self) -> dict[str, list]:
        def poly(pts):
            return MultiPolygon([Polygon(pts)])

        base = {"document_ref": "synthetic", "sheet": "a", "page": 1}
        return {
            "plan_boundary": [(poly(self.boundary), {**base, "feature_key": "coverage"})],
            "urban_parcels": [
                (
                    poly(self.up1),
                    {
                        **base,
                        "feature_key": "1",
                        "urban_parcel_number": "1",
                        "block_ref": "GT-A",
                        "source_bbox": [1.0, 2.0, 3.0, 4.0],
                    },
                ),
                (
                    poly(self.up2),
                    {**base, "feature_key": "2", "urban_parcel_number": "2", "block_ref": "GT-A"},
                ),
            ],
            "urban_blocks": [
                (poly(self.block), {**base, "feature_key": "GT-A", "block_ref": "GT-A"})
            ],
            "planned_land_use": [
                (
                    poly(self.up1),
                    {
                        **base,
                        "feature_key": "UP 1",
                        "code": "SS",
                        "name": "Stanovanje",
                        "urban_parcel_number": "1",
                    },
                )
            ],
        }

    def write(self, folder: Path, *, point_shift: tuple[float, float] = (0.0, 0.0)) -> Path:
        """The rules, the control points and the extraction's GeoPackage in the local frame."""
        folder.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_load(syn.RULES_YAML)
        data["georef"] = {"crs": CRS, "max_rmse_m": 0.5, "snap_tolerance_m": 0.5}
        rules_file = folder / "synthetic.yaml"
        rules_file.write_text(yaml.safe_dump(data), encoding="utf-8")
        points = []
        for i, (x, y) in enumerate(SHEET_POINTS, start=1):
            e, n = self.truth(x * syn.K, y * syn.K)
            points.append(
                ControlPoint(
                    f"cp{i}",
                    "a",
                    float(x),
                    float(y),
                    e + point_shift[0],
                    n + point_shift[1],
                    "manual",
                )
            )
        write_points(folder / "synthetic.points.csv", points)

        def inverse(coords: np.ndarray) -> np.ndarray:
            return np.array([self.inverse(x, y) for x, y in coords])

        local = {
            layer: [(shapely_transform(g, inverse), props) for g, props in items]
            for layer, items in self.world().items()
        }
        write_gpkg(folder / "synthetic.gpkg", local)
        return rules_file


def apply_args(folder: Path, doc_id: int, label: str) -> list[str]:
    return [
        "apply",
        str(folder / "synthetic.yaml"),
        "--gpkg",
        str(folder / "synthetic.gpkg"),
        "--out",
        str(folder / "out"),
        "--document-id",
        str(doc_id),
        "--stage",
        "--label",
        label,
        "--by",
        "tester",
        "--work",
        str(folder / "work"),
    ]


def report(folder: Path) -> dict:
    return json.loads((folder / "out" / "synthetic.georef.json").read_text(encoding="utf-8"))


async def test_cli_snaps_stages_and_publishes_a_georeferenced_document(
    run,
    db,
    publish_env,  # noqa: F811 - imported fixture
    tiles,  # noqa: F811 - imported fixture
    tmp_path,
):
    doc_id = await register(db, "GT georef plan")
    plan = Plan(await cadastral_corners(db))
    rules_file = plan.write(tmp_path)
    assert await run("fit", str(rules_file)) == 0
    stored = json.loads((tmp_path / "synthetic.transform.json").read_text(encoding="utf-8"))
    assert stored["rmse_m"] < 1e-6 and stored["crs"] == CRS and stored["points_used"] == 6
    assert await run(*apply_args(tmp_path, doc_id, "geo-test-1")) == 0

    first = report(tmp_path)
    assert first["status"] == "staged" and first["errors"] == []
    assert first["warnings"] == []  # UP 2 lies inside UP 1: overlaps are not checked
    snap = first["snap"]
    # UP 1: 2 of 4 corners moved, UP 2: none (mid-parcel), the block: all 4
    assert (snap["vertices"], snap["snapped_vertices"], snap["near_misses"]) == (12, 6, 1)
    assert snap["features_snapped"] == 2 and snap["tolerance_m"] == 0.5
    # the overlay check: 7 vertices follow the cadastre (UP 1's three, the block's four), their
    # mean vector to it is small: no systematic offset
    assert snap["offset_samples"] == 7 and snap["systematic_offset_m"] < 0.25
    assert first["overlap"]["planned_parcels"] == 2 and first["overlap"]["overlap_m2"] > 0
    with (tmp_path / "out" / "synthetic.snap-log.csv").open(encoding="utf-8") as fh:
        log = list(csv.DictReader(fh))
    assert sorted(r["kind"] for r in log) == ["near_miss"] + ["snapped"] * 6
    (near,) = [r for r in log if r["kind"] == "near_miss"]
    assert near["layer"] == "urban_parcels" and near["feature_key"] == "1"
    assert float(near["distance_m"]) == pytest.approx(1.0, abs=0.01)

    (dataset,) = await _rows(db, "SELECT * FROM georef_datasets WHERE document_id = :d", d=doc_id)
    assert dataset["status"] == "staged" and dataset["dataset_version"] == "geo-test-1"
    assert (dataset["crs"], dataset["method"], dataset["points_used"]) == (CRS, "helmert", 6)
    assert dataset["rmse_m"] < 1e-6 and dataset["source"] == "extraction"
    assert dataset["sheets"] == [
        {"sheet": "a", "page": 1, "points": 6, "rmse_m": dataset["sheets"][0]["rmse_m"]}
    ]
    assert dataset["transform"]["points_sha256"] == stored["points_sha256"]
    assert dataset["output_sha256"] == first["output_sha256"]
    assert set(dataset["batches"]) == {
        "document_coverage",
        "urban_parcels",
        "urban_blocks",
        "land_use",
    }

    staged = {
        r["feature_key"]: r
        for r in await _rows(
            db,
            "SELECT s.feature_key, s.properties, ST_AsText(ST_Transform(s.geom, 25834)) AS utm, "
            "ST_AsText(s.geom) AS wgs FROM staging_geometry s WHERE s.batch_id = :b",
            b=dataset["batches"]["urban_parcels"],
        )
    }
    assert set(staged) == {f"{doc_id}|UP 1", f"{doc_id}|UP 2"}
    up1 = staged[f"{doc_id}|UP 1"]
    props = up1["properties"]
    assert props["urban_parcel_number"] == "UP 1" and props["block_ref"] == "GT-A"
    assert props["document_id"] == doc_id and props["dataset_version"] == "geo-test-1"
    assert (props["vertices"], props["snapped_vertices"], props["snapped_ratio"]) == (4, 2, 0.5)
    assert props["source_bbox"] == [1.0, 2.0, 3.0, 4.0] and props["area_m2"] > 1000
    assert staged[f"{doc_id}|UP 2"]["properties"]["snapped_ratio"] == 0
    # snapped corners sit on the cadastral corners; the others where the plan put them
    utm = list(wkt.loads(up1["utm"]).geoms[0].exterior.coords)
    sw, se, ne, nw = plan.corners
    assert _near(utm, sw, 1e-4) and _near(utm, se, 1e-4)
    assert _near(utm, plan.up1[2], 1e-4) and _near(utm, plan.up1[3], 1e-4)
    wgs = list(wkt.loads(up1["wgs"]).geoms[0].exterior.coords)
    assert _near(wgs, await to_wgs84(db, *plan.up1[3]), 1e-8)  # ~1 mm
    (block,) = await _rows(
        db,
        "SELECT properties FROM staging_geometry WHERE batch_id = :b",
        b=dataset["batches"]["urban_blocks"],
    )
    assert block["properties"]["snapped_ratio"] == 1 and block["properties"]["block_ref"] == "GT-A"

    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        r = await client.get(f"/v1/admin/documents/{doc_id}", headers=auth())
        assert r.status_code == 200, r.text
        geo = r.json()["georeference"]
        assert geo["status"] == "staged" and geo["dataset_version"] == "geo-test-1"
        assert geo["rmse_m"] < 1e-6 and geo["max_rmse_m"] == 0.5 and geo["points_used"] == 6
        assert geo["sheets"][0]["sheet"] == "a" and geo["sheets"][0]["points"] == 6
        assert geo["snapped_ratio"] == 0.5 and geo["near_misses"] == 1
        assert geo["warnings"] == [] and geo["errors"] == []
        assert geo["cadastral_overlap_share"] > 0.5
        assert geo["systematic_offset_m"] == snap["systematic_offset_m"]

        await reject_seeded_pending_item(app)
        await approve_geometry(client)
        await publish(client, "v-geo-1")
        (published,) = await rows(
            app,
            "SELECT status, published_version_id FROM georef_datasets WHERE document_id = :d",
            d=doc_id,
        )
        assert published["status"] == "published" and published["published_version_id"]
        parcels = await rows(
            app,
            "SELECT u.urban_parcel_number, b.block_ref FROM urban_parcels u "
            "LEFT JOIN urban_blocks b ON b.id = u.block_id WHERE u.document_id = :d "
            "ORDER BY 1",
            d=doc_id,
        )
        assert parcels == [
            {"urban_parcel_number": "UP 1", "block_ref": "GT-A"},
            {"urban_parcel_number": "UP 2", "block_ref": "GT-A"},
        ]
        (document,) = await rows(
            app,
            "SELECT coverage_geom IS NOT NULL AS covered FROM planning_documents WHERE id = :d",
            d=doc_id,
        )
        assert document["covered"]
        served = {
            (r["layer_id"], r["feature_key"])
            for r in await rows(
                app,
                "SELECT f.layer_id, f.feature_key FROM layer_features f "
                "JOIN publish_versions v ON v.id = f.publish_version_id AND v.is_current",
            )
        }
        assert ("land_use", f"{doc_id}|UP 1") in served

        # the stored transform again: the same georeferenced features, a new dataset version
        assert await run(*apply_args(tmp_path, doc_id, "geo-test-2")) == 0
        assert report(tmp_path)["output_sha256"] == first["output_sha256"]
        await approve_geometry(client)
        await publish(client, "v-geo-2")
        states = {
            r["dataset_version"]: r["status"]
            for r in await rows(
                app,
                "SELECT dataset_version, status FROM georef_datasets WHERE document_id = :d",
                d=doc_id,
            )
        }
        assert states == {"geo-test-1": "superseded", "geo-test-2": "published"}


async def test_cli_refuses_a_transform_that_puts_the_plan_outside_the_city(run, db, tmp_path):
    doc_id = await register(db, "GT shifted plan")
    plan = Plan(await cadastral_corners(db))
    rules_file = plan.write(tmp_path, point_shift=(40_000.0, 0.0))  # a mistyped easting digit
    assert await run("fit", str(rules_file)) == 0  # the fit cannot tell
    assert await run(*apply_args(tmp_path, doc_id, "geo-shifted-1")) == 1
    refused = report(tmp_path)
    assert refused["status"] == "invalid" and refused["batches"] == {}
    assert "outside_extent" in [e["code"] for e in refused["errors"]]
    (dataset,) = await _rows(
        db, "SELECT status, batches FROM georef_datasets WHERE document_id = :d", d=doc_id
    )
    assert dataset == {"status": "invalid", "batches": {}}
    assert await _rows(db, "SELECT id FROM geometry_batches WHERE status = 'staged'") == []


TRANSFORM = {
    "crs": CRS,
    "method": "helmert",
    "rmse_m": 0.01,
    "max_residual_m": 0.02,
    "points_used": 6,
    "sheets": [{"sheet": "a", "page": 1, "points": 6, "rmse_m": 0.01}],
}


def _square(x0: float, y0: float, size: float = 0.0004) -> MultiPolygon:
    return MultiPolygon(
        [Polygon([(x0, y0), (x0 + size, y0), (x0 + size, y0 + size / 2), (x0, y0 + size / 2)])]
    )


async def _stage(db, doc_id: int, label: str, features: dict) -> object:
    async with db() as session:
        outcome = await stage_document(
            session,
            municipality_id="podgorica",
            document_id=doc_id,
            label=label,
            features=features,
            transform=TRANSFORM,
            source="manual_redraw",
            bounds=BOUNDS,
            snap_tolerance_m=0.5,
            metric_srid=25834,
            parcel_prefix="UP",
            output_sha256="0" * 64,
            gpkg_key=None,
            imported_by="tester",
        )
        await session.commit()
    return outcome


async def test_validation_refuses_no_cadastral_overlap(db):
    doc_id = await register(db, "GT flanking plan")
    # two planned parcels either side of cadastral parcel 1004 (19.278-19.2785), touching nothing
    features = {
        "urban_parcels": [
            (_square(19.2770, 42.4400), {"feature_key": "1", "urban_parcel_number": "1"}),
            (_square(19.2790, 42.4400), {"feature_key": "2", "urban_parcel_number": "2"}),
        ]
    }
    outcome = await _stage(db, doc_id, "geo-flank-1", features)
    assert outcome.status == "invalid" and outcome.batches == {}
    assert [e.code for e in outcome.errors] == ["no_cadastral_overlap"]
    assert outcome.overlap["cadastral_parcels_in_extent"] >= 1
    assert outcome.overlap["overlap_m2"] == 0


async def test_a_plan_shifted_as_a_whole_is_flagged_as_a_systematic_offset(db):
    doc_id = await register(db, "GT offset plan")
    shifted = await _rows(
        db,
        "SELECT id, ST_AsText(ST_Transform(ST_Translate(ST_Transform(geom, 25834), :dx, :dy), "
        "4326)) AS g FROM cadastral_parcels WHERE id IN (1003, 1004) ORDER BY id",
        dx=0.3,
        dy=0.3,
    )
    features = {
        "urban_parcels": [
            (wkt.loads(r["g"]), {"feature_key": str(r["id"]), "urban_parcel_number": str(r["id"])})
            for r in shifted
        ]
    }
    outcome = await _stage(db, doc_id, "geo-offset-1", features)
    assert outcome.status == "staged"  # a warning: the reviewer decides
    snap = outcome.snap
    assert snap["snapped_vertices"] == snap["vertices"] == 8  # every corner within 0.5 m
    assert snap["offset_samples"] == 8
    assert snap["offset_vector_m"] == [-0.3, -0.3]
    assert [w.code for w in outcome.warnings] == ["systematic_offset"]


async def test_land_use_of_other_documents_is_carried_forward(db, publish_env):  # noqa: F811
    first = await register(db, "GT land use one")
    second = await register(db, "GT land use two")

    def land_use(x0: float, key: str) -> dict:
        return {
            "planned_land_use": [(_square(x0, 42.4410), {"feature_key": key, "code": "SS"})],
            "urban_parcels": [
                (_square(x0, 42.4410), {"feature_key": key, "urban_parcel_number": key})
            ],
        }

    one = await _stage(db, first, "geo-lu-1", land_use(19.2612, "1"))
    two = await _stage(db, second, "geo-lu-2", land_use(19.2617, "7"))
    assert one.status == two.status == "staged"
    keys = await _rows(
        db,
        "SELECT feature_key FROM staging_geometry WHERE batch_id = :b ORDER BY 1",
        b=two.batches["land_use"],
    )
    assert [k["feature_key"] for k in keys] == [f"{first}|1", f"{second}|7"]
    # the first document again: its old land use is replaced, the second's carried
    again = await _stage(db, first, "geo-lu-3", land_use(19.2613, "1"))
    assert again.superseded == ["geo-lu-1"]
    keys = await _rows(
        db,
        "SELECT feature_key, properties->>'dataset_version' AS v FROM staging_geometry "
        "WHERE batch_id = :b ORDER BY 1",
        b=again.batches["land_use"],
    )
    assert keys == [
        {"feature_key": f"{first}|1", "v": "geo-lu-3"},
        {"feature_key": f"{second}|7", "v": "geo-lu-2"},
    ]
    staged = await _rows(
        db, "SELECT id FROM geometry_batches WHERE layer_id = 'land_use' AND status = 'staged'"
    )
    assert [r["id"] for r in staged] == [again.batches["land_use"]]

    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        await approve_geometry(client)
        await publish(client, "v-geo-lu")
    served = await _rows(
        db,
        "SELECT f.feature_key FROM layer_features f JOIN publish_versions v "
        "ON v.id = f.publish_version_id AND v.is_current WHERE f.layer_id = 'land_use' ORDER BY 1",
    )
    assert [r["feature_key"] for r in served] == [f"{first}|1", f"{second}|7"]
    states = {
        r["dataset_version"]: r["status"]
        for r in await _rows(db, "SELECT dataset_version, status FROM georef_datasets")
    }
    assert states == {"geo-lu-1": "superseded", "geo-lu-2": "published", "geo-lu-3": "published"}


async def _rows(db, sql: str, **params) -> list[dict]:
    async with db() as session:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


def _near(coords, point, tolerance: float) -> bool:
    return min(math.dist(c[:2], point) for c in coords) < tolerance
