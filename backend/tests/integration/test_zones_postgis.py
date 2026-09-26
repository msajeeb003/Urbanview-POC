"""The zone dataset through PostGIS: import into staging (reprojection, document matching), the
report's parcel counts, and the publish job's zones upsert + document apply. Everything runs in
one transaction that is rolled back, so the sample data stays as the other tests expect it."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.municipality import load_profile
from core.zones.config import ValidationConfig
from core.zones.report import build_report, render_markdown
from core.zones.schema import DOCUMENT_FIELD_NAMES
from core.zones.staging import apply_zone_datasets, stage_dataset
from core.zones.validate import read_dataset, validate
from jobs.publish_pipeline import UPSERT_SQL

pytestmark = pytest.mark.integration
M = "podgorica"


def _write_inputs(tmp_path: Path, xmin: float, ymin: float, xmax: float, ymax: float) -> tuple:
    mid = (xmin + xmax) / 2

    def ring(x0: float, x1: float) -> list:
        return [[[x0, ymin], [x1, ymin], [x1, ymax], [x0, ymax], [x0, ymin]]]

    zones = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "zone_id": "zapad",
                    "name": "Zapad",
                    "zone_type": "residential",
                    "general_planning_summary": "",
                    "notes": "",
                    "no_adopted_plan": False,
                },
                "geometry": {"type": "Polygon", "coordinates": ring(xmin, mid)},
            },
            {
                "type": "Feature",
                "properties": {
                    "zone_id": "istok",
                    "name": "Istok",
                    "zone_type": "mixed",
                    "general_planning_summary": "Istočni dio",
                    "notes": "",
                    "no_adopted_plan": False,
                },
                "geometry": {"type": "Polygon", "coordinates": ring(mid, xmax)},
            },
        ],
    }
    zones_path = tmp_path / "zones.geojson"
    zones_path.write_text(json.dumps(zones), encoding="utf-8")
    docs = [
        {
            "zone_id": "zapad",
            "document_name": "DUP Stari Aerodrom",
            "document_type": "DUP",
            "status": "adopted",
            "confirmed": "true",
        },
        {
            "zone_id": "istok",
            "document_name": "PUP Glavni grad (izvod)",
            "document_type": "PUP",
            "status": "adopted",
            "confirmed": "true",
        },
        {
            "zone_id": "istok",
            "document_name": "DUP Test novi plan",
            "document_type": "DUP",
            "status": "in_progress",
            "eregistri_reference": "999999",
            "source_url": "https://lamp.gov.me/PlanningDocument/Details/999999",
            "confirmed": "true",
            "poc_coverage": "false",
        },
    ]
    docs_path = tmp_path / "zone_documents.csv"
    with docs_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(DOCUMENT_FIELD_NAMES))
        writer.writeheader()
        for row in docs:
            writer.writerow({k: row.get(k, "") for k in DOCUMENT_FIELD_NAMES})
    return zones_path, docs_path


async def test_import_report_and_publish_apply(postgis_url: str, tmp_path: Path) -> None:
    engine = create_async_engine(postgis_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            box = (
                await session.execute(
                    text(
                        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM"
                        " (SELECT ST_Extent(geom) AS e FROM cadastral_parcels"
                        "  WHERE municipality_id = :m) x"
                    ),
                    {"m": M},
                )
            ).one()
            pad = 0.001
            zones_path, docs_path = _write_inputs(
                tmp_path, box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
            )
            dataset = read_dataset(zones_path, docs_path)
            assert dataset.srs_id == 4326 and len(dataset.zones) == 2
            report = validate(
                dataset,
                document_types=load_profile(M).terminology.document_types,
                config=ValidationConfig(),
            )
            assert report.ok, [p.message for p in report.errors]

            staged = await stage_dataset(
                session,
                municipality_id=M,
                dataset=dataset,
                validation=report,
                label="test-zones-1",
                imported_by="pytest",
                sources={"zones": {"path": "zones.geojson"}},
            )
            assert staged.zones == 2 and staged.documents == 3
            assert staged.matches == {"name": 2, "new": 1}
            srid = (
                await session.execute(
                    text("SELECT DISTINCT ST_SRID(geom) FROM staging_geometry WHERE batch_id = :b"),
                    {"b": staged.batch_id},
                )
            ).scalar_one()
            assert srid == 4326

            zone_report = await build_report(session, M, dataset_version="test-zones-1")
            t = zone_report.totals
            inside = sum(z.cadastral_parcels for z in zone_report.zones)
            assert inside + t["cadastral_outside"] == t["cadastral_total"]
            assert inside > 0  # the padded halves hold (nearly) every parcel
            by_zone = {z.zone_id: z for z in zone_report.zones}
            assert by_zone["istok"].documents == 2 and by_zone["istok"].in_progress == 1
            assert by_zone["zapad"].adopted == 1 and by_zone["zapad"].zone_type == "Residential"
            md = render_markdown(zone_report, title="Test zones")
            assert "Sign-off" in md and "DUP Test novi plan" in md

            # the publish job's geometry step for this batch, then the document apply
            for statement in UPSERT_SQL["zones"]:
                await session.execute(
                    text(statement), {"batch": staged.batch_id, "label": "test-zones-1", "v": None}
                )
            version_id = (
                await session.execute(
                    text("SELECT id FROM publish_versions WHERE municipality_id = :m LIMIT 1"),
                    {"m": M},
                )
            ).scalar_one()
            counts = await apply_zone_datasets(
                session,
                municipality_id=M,
                version_id=version_id,
                published_batch_ids=[staged.batch_id],
            )
            assert counts == {"zone_datasets": 1, "documents_updated": 2, "documents_created": 1}
            zones = dict(
                (
                    await session.execute(
                        text("SELECT zone_key, zone_type FROM zones WHERE zone_key IS NOT NULL")
                    )
                ).all()
            )
            assert zones == {"zapad": "res", "istok": "mix"}
            new = (
                await session.execute(
                    text(
                        "SELECT d.status, d.eregistri_reference, z.zone_key, d.coverage_live"
                        " FROM planning_documents d JOIN zones z ON z.id = d.zone_id"
                        " WHERE d.name = 'DUP Test novi plan'"
                    )
                )
            ).one()
            assert tuple(new) == ("in_progress", "999999", "istok", False)
            status = (
                await session.execute(
                    text("SELECT status FROM zone_datasets WHERE dataset_version = 'test-zones-1'")
                )
            ).scalar_one()
            assert status == "published"
            await session.rollback()
    finally:
        await engine.dispose()
