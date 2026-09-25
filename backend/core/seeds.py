"""Load sample datasets (``database/seeds/<name>/``) into the location and panel tables.

    python -m core.seeds podgorica_sample                 # DATABASE_URL from settings / .env
    python -m core.seeds podgorica_sample --url postgresql+asyncpg://...

Geometry tables are one GeoJSON FeatureCollection per table (``zones``, ``planning_documents``,
``urban_blocks``, ``urban_parcels``, ``cadastral_parcels``); the panel tables are plain JSON
arrays of row objects (``publish_versions``, ``financial_assumptions``,
``planning_parameter_values``, ``planning_parameter_extractions``). Rows carry explicit ``id``
values so cross-references (``zone_id``, ``document_id``, ``block_id``, ``urban_parcel_id``,
``publish_version_id``) are stable; sequences are re-synced after loading. ``area_m2`` is
computed on the spheroid when a feature does not provide it. The field dictionary
(``planning_fields``) is seeded by migration 0003, never here. Used by the integration tests and
by ``make seed``. With ``--upload-files`` a placeholder PDF (``placeholder_pdf``) is uploaded to
the private bucket for every sample document that declares a ``file_key``, so the source viewer
(``GET /v1/source``) has objects to sign; real PDFs come from the ingestion job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.parcel_links import recompute_current_links
from core.storage import ObjectStorage

SEEDS_DIR = Path(__file__).resolve().parents[2] / "database" / "seeds"

# (table, geometry column or None for a JSON rows file, allowed columns) in insert order (FK
# dependencies first). Every table here has municipality_id and dataset_version.
TABLES: list[tuple[str, str | None, frozenset[str]]] = [
    (
        "zones",
        "geom",
        frozenset(
            {
                "id",
                "municipality_id",
                "name",
                "general_planning_summary",
                "zone_type",
                "dataset_version",
            }
        ),
    ),
    (
        "planning_documents",
        "coverage_geom",
        frozenset(
            {
                "id",
                "municipality_id",
                "name",
                "type",
                "status",
                "source",
                "source_url",
                "zone_id",
                "amends_document_id",
                "file_key",
                "page_count",
                "page_images_rendered",
                "coverage_live",
                "dataset_version",
            }
        ),
    ),
    (
        "urban_blocks",
        "geom",
        frozenset({"id", "municipality_id", "block_ref", "zone_id", "dataset_version"}),
    ),
    (
        "urban_parcels",
        "geom",
        frozenset(
            {
                "id",
                "municipality_id",
                "urban_parcel_number",
                "area_m2",
                "block_id",
                "document_id",
                "dataset_version",
            }
        ),
    ),
    (
        "cadastral_parcels",
        "geom",
        frozenset(
            {
                "id",
                "municipality_id",
                "parcel_number",
                "sub_number",
                "ko_name",
                "street_address",
                "area_m2",
                "public_ownership",
                "restitution_or_legal_burden",
                "dataset_version",
            }
        ),
    ),
    (
        "publish_versions",
        None,
        frozenset(
            {
                "id",
                "municipality_id",
                "label",
                "published_at",
                "published_by",
                "formula_version",
                "notes",
                "is_current",
                "dataset_version",
            }
        ),
    ),
    (
        "financial_assumptions",
        None,
        frozenset(
            {
                "id",
                "municipality_id",
                "zone_id",
                "land_rate_eur_m2",
                "build_rate_eur_m2",
                "design_rate_eur_m2",
                "sale_rate_eur_m2",
                "range_low_factor",
                "range_high_factor",
                "source",
                "source_date",
                "notes",
                "is_current",
                "version",
                "supersedes_id",
                "land_rate_low_eur_m2",
                "land_rate_high_eur_m2",
                "build_rate_low_eur_m2",
                "build_rate_high_eur_m2",
                "design_rate_low_eur_m2",
                "design_rate_high_eur_m2",
                "sale_rate_low_eur_m2",
                "sale_rate_high_eur_m2",
                "created_by",
                "dataset_version",
            }
        ),
    ),
    (
        "zone_parameter_sets",
        None,
        frozenset(
            {
                "id",
                "municipality_id",
                "zone_id",
                "version",
                "is_current",
                "land_use",
                "max_far",
                "max_site_coverage_pct",
                "max_height_m",
                "max_floors",
                "notes",
                "source_document_id",
                "source_page",
                "source_note",
                "verified_on",
                "verified_by",
                "created_by",
                "dataset_version",
            }
        ),
    ),
    (
        "planning_parameter_values",
        None,
        frozenset(
            {
                "id",
                "municipality_id",
                "document_id",
                "urban_parcel_id",
                "field_key",
                "value_text",
                "value_number",
                "unit",
                "source_page",
                "source_bbox",
                "source_note",
                "publish_version_id",
                "dataset_version",
            }
        ),
    ),
    (
        "planning_parameter_extractions",
        None,
        frozenset(
            {
                "id",
                "municipality_id",
                "document_id",
                "urban_parcel_id",
                "field_key",
                "value_text",
                "value_number",
                "unit",
                "source_page",
                "source_bbox",
                "source_note",
                "extracted_by",
                "extracted_at",
                "review_state",
                "reviewer",
                "reviewed_at",
                "review_note",
                "published_value_id",
                "entity_type",
                "zone_id",
                "block_id",
                "parameter_key",
                "raw_text",
                "confidence",
                "amended_value_text",
                "amended_value_number",
                "amended_unit",
                "reviewed_by_user_id",
                "dataset_version",
            }
        ),
    ),
]
# Columns whose parameters are bound as CAST(:col AS <type>) (enums, dates, timestamps, jsonb):
# asyncpg cannot infer them from a text/JSON value. Untyped columns stay :col.
CASTS: dict[str, str] = {
    "source_date": "date",
    "verified_on": "date",
    "published_at": "timestamptz",
    "extracted_at": "timestamptz",
    "reviewed_at": "timestamptz",
    "source_bbox": "jsonb",
    "review_state": "review_state",
    "status": "planning_document_status",
}
AREA_TABLES = {"urban_parcels", "cadastral_parcels"}
GEOM_EXPR = "ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:__geom), 4326))"
AREA_EXPR = (
    "ROUND(CAST(ST_Area(CAST(ST_SetSRID(ST_GeomFromGeoJSON(:__geom), 4326) AS geography)) "
    "AS numeric), 1)"
)


def _bind_value(column: str, value: Any) -> Any:
    """JSON value -> the Python value asyncpg needs for the column's (cast) type."""
    if value is None:
        return None
    if isinstance(value, list | dict):
        return json.dumps(value)
    cast = CASTS.get(column)
    if cast == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if cast == "timestamptz" and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _placeholder(column: str) -> str:
    cast = CASTS.get(column)
    return f"CAST(:{column} AS {cast})" if cast else f":{column}"


def _read_rows(path: Path, geom_column: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    """Rows of a seed file as (dataset_version, [{properties..., "__geom": geojson?}])."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if geom_column is None:
        if not isinstance(data, list):
            raise ValueError(f"{path.name}: expected a JSON array of row objects")
        return None, [dict(row) for row in data]
    rows = []
    for feature in data.get("features", []):
        props = dict(feature.get("properties") or {})
        props["__geom"] = feature["geometry"]
        rows.append(props)
    return data.get("dataset_version"), rows


async def load_sample(
    session: AsyncSession,
    name: str = "podgorica_sample",
    *,
    municipality_id: str = "podgorica",
    replace: bool = True,
    min_overlap_m2: float = 1.0,
    min_overlap_fraction: float = 0.02,
) -> dict[str, int]:
    base = SEEDS_DIR / name
    if not base.is_dir():
        raise FileNotFoundError(f"no seed dataset '{name}' in {SEEDS_DIR}")

    if replace:
        for table, _, _ in reversed(TABLES):
            await session.execute(
                text(f"DELETE FROM {table} WHERE municipality_id = :municipality_id"),
                {"municipality_id": municipality_id},
            )

    counts: dict[str, int] = {}
    for table, geom_column, allowed in TABLES:
        path = base / (f"{table}.geojson" if geom_column else f"{table}.json")
        if not path.is_file():
            counts[table] = 0
            continue
        version, rows = _read_rows(path, geom_column)
        version = version or name
        loaded = 0
        for row in rows:
            geometry = row.pop("__geom", None)
            unknown = set(row) - allowed
            if unknown:
                raise ValueError(f"{path.name}: unknown properties {sorted(unknown)}")
            row.setdefault("municipality_id", municipality_id)
            row.setdefault("dataset_version", version)
            if table == "planning_parameter_extractions":
                row.setdefault("parameter_key", row.get("field_key"))

            columns = list(row)
            values = [_placeholder(column) for column in columns]
            params = {column: _bind_value(column, row[column]) for column in columns}
            if geom_column:
                columns.append(geom_column)
                values.append(GEOM_EXPR)
                params["__geom"] = json.dumps(geometry)
                if table in AREA_TABLES and "area_m2" not in row:
                    columns.append("area_m2")
                    values.append(AREA_EXPR)

            await session.execute(
                text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)})"),
                params,
            )
            loaded += 1
        counts[table] = loaded

    for table, _, _ in TABLES:
        await session.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )
        )
    # The parcel panel reads the calculation basis from parcel_links of the current version;
    # the publish job writes them for every new version, the seeded one gets them here.
    links = await recompute_current_links(
        session,
        municipality_id=municipality_id,
        min_overlap_m2=min_overlap_m2,
        min_overlap_fraction=min_overlap_fraction,
    )
    counts["parcel_links"] = links["parcel_links"] if links else 0
    await session.commit()
    return counts


def _ascii(text: str) -> str:
    """Latin-1-safe text for the built-in Helvetica (dashes simplified, accents dropped)."""
    return (
        unicodedata.normalize("NFKD", text.replace("\u2013", "-").replace("\u2014", "-"))
        .encode("ascii", "ignore")
        .decode()
    )


def _pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


Annotations = Mapping[int, Sequence[tuple[Sequence[float], str]]]


def placeholder_pdf(title: str, pages: int, annotations: Annotations | None = None) -> bytes:
    """A small, valid, uncompressed PDF with one line of text per page (A4, Helvetica).

    ``annotations`` maps a 1-based page to ``(bbox, text)`` pairs: each text is printed inside a
    thin frame at its bbox (PDF points, origin bottom-left), so a sample value's source citation
    points at the value itself and the source viewer's highlight lands on it.
    """
    pages = max(1, int(pages))
    ascii_title = _ascii(title)
    annotations = annotations or {}
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the pages tree, filled in once the page object numbers are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_numbers: list[int] = []
    for index in range(pages):
        line = _pdf_text(f"{ascii_title} - sample placeholder, page {index + 1} of {pages}")
        parts = [f"BT /F1 14 Tf 72 720 Td ({line}) Tj ET"]
        for bbox, label in annotations.get(index + 1, ()):
            x0, y0, x1, y1 = (float(v) for v in bbox)
            parts.append(f"0.7 0.66 0.58 RG 0.6 w {x0} {y0} {x1 - x0} {y1 - y0} re S")
            size = max(6.0, min(10.0, (y1 - y0) - 6))
            parts.append(
                f"BT /F1 {size:g} Tf {x0 + 4:g} {y0 + ((y1 - y0) - size) / 2 + 1:g} Td "
                f"({_pdf_text(_ascii(label))}) Tj ET"
            )
        stream = "\n".join(parts).encode("latin-1")
        content_number = len(objects) + 2
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>".encode()
        )
        page_numbers.append(len(objects))
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


def upload_sample_files(
    storage: ObjectStorage, name: str = "podgorica_sample", municipality_id: str = "podgorica"
) -> int:
    """Upload a placeholder PDF for every sample document with a ``file_key`` (boto3, sync).
    Returns the number of objects written."""
    _, rows = _read_rows(SEEDS_DIR / name / "planning_documents.geojson", "coverage_geom")
    storage.ensure_bucket()
    citations = sample_citations(name)
    uploaded = 0
    for row in rows:
        file_key = row.get("file_key")
        if not file_key:
            continue
        title = str(row.get("name") or f"{municipality_id} document {row.get('id')}")
        pdf = placeholder_pdf(
            title, int(row.get("page_count") or 1), citations.get(int(row["id"]), {})
        )
        storage.put_bytes(file_key, pdf, "application/pdf")
        uploaded += 1
    return uploaded


def sample_citations(name: str = "podgorica_sample") -> dict[int, dict[int, list]]:
    """document id -> page -> [(bbox, "max far: 3.2 - table 3 - UP 12"), ...] from the sample's
    published values, so the placeholder PDFs carry each cited value at its bbox."""
    path = SEEDS_DIR / name / "planning_parameter_values.json"
    if not path.is_file():
        return {}
    out: dict[int, dict[int, list]] = {}
    for row in json.loads(path.read_text(encoding="utf-8")):
        bbox, page = row.get("source_bbox"), row.get("source_page")
        if not bbox or not page:
            continue
        value = row.get("value_number")
        if value is None:
            value = row.get("value_text")
        elif float(value).is_integer():
            value = int(value)
        label = f"{str(row['field_key']).replace('_', ' ')}: {value}"
        if row.get("source_note"):
            label += f" - {row['source_note']}"
        out.setdefault(int(row["document_id"]), {}).setdefault(int(page), []).append((bbox, label))
    return out


SYNTHETIC_VERSION = "synthetic-bulk"


async def load_synthetic_bulk(
    session: AsyncSession,
    *,
    municipality_id: str = "podgorica",
    bounds: tuple[float, float, float, float] = (19.33, 42.34, 19.44, 42.54),
    document_grid: tuple[int, int] = (15, 20),
    parcel_grid: tuple[int, int] = (100, 100),
    dataset_version: str = SYNTHETIC_VERSION,
    min_overlap_m2: float = 1.0,
    min_overlap_fraction: float = 0.02,
) -> dict[str, int]:
    """Synthetic volume for query-plan and latency tests (not a real dataset).

    Fills ``bounds`` (min_lng, min_lat, max_lng, max_lat; keep it away from the hand-made sample)
    with a grid of adopted documents, one block per document, and grids of cadastral and planned
    urban parcels (the planned ones offset so cadastral and planned areas differ). Everything is
    tagged with ``dataset_version`` so ``delete_dataset`` can remove it.
    """
    min_lng, min_lat, max_lng, max_lat = bounds
    cols, rows = document_grid
    pcols, prows = parcel_grid
    params = {
        "m": municipality_id,
        "v": dataset_version,
        "x0": min_lng,
        "y0": min_lat,
        "x1": max_lng,
        "y1": max_lat,
        "cols": cols,
        "n_docs": cols * rows,
        "dw": (max_lng - min_lng) / cols,
        "dh": (max_lat - min_lat) / rows,
        "pcols": pcols,
        "n_parcels": pcols * prows,
        "pw": (max_lng - min_lng) / pcols,
        "ph": (max_lat - min_lat) / prows,
    }
    zone_id = (
        await session.execute(
            text(
                "INSERT INTO zones (municipality_id, name, geom, dataset_version) VALUES "
                "(:m, 'Synthetic zone', ST_Multi(ST_MakeEnvelope(CAST(:x0 AS float), "
                "CAST(:y0 AS float), CAST(:x1 AS float), CAST(:y1 AS float), 4326)), :v) "
                "RETURNING id"
            ),
            params,
        )
    ).scalar_one()
    params["zone_id"] = zone_id

    doc_cell = (
        "ST_MakeEnvelope("
        "CAST(:x0 AS float) + (i % CAST(:cols AS int)) * CAST(:dw AS float), "
        "CAST(:y0 AS float) + (i / CAST(:cols AS int)) * CAST(:dh AS float), "
        "CAST(:x0 AS float) + (i % CAST(:cols AS int) + 1) * CAST(:dw AS float), "
        "CAST(:y0 AS float) + (i / CAST(:cols AS int) + 1) * CAST(:dh AS float), 4326)"
    )
    await session.execute(
        text(
            "INSERT INTO planning_documents (municipality_id, name, type, status, source, "
            "coverage_geom, zone_id, coverage_live, dataset_version) "
            "SELECT :m, 'Synthetic DUP ' || i, 'DUP', 'adopted', 'synthetic', "
            f"ST_Multi({doc_cell}), :zone_id, true, :v "
            "FROM generate_series(0, CAST(:n_docs AS int) - 1) AS i"
        ),
        params,
    )
    await session.execute(
        text(
            "INSERT INTO urban_blocks (municipality_id, block_ref, geom, zone_id, dataset_version) "
            "SELECT :m, 'SB-' || d.id, d.coverage_geom, :zone_id, :v "
            "FROM planning_documents d WHERE d.municipality_id = :m AND d.dataset_version = :v"
        ),
        params,
    )

    cell = (
        "LATERAL (SELECT CAST(:x0 AS float) + (i % CAST(:pcols AS int)) * CAST(:pw AS float) AS x, "
        "CAST(:y0 AS float) + (i / CAST(:pcols AS int)) * CAST(:ph AS float) AS y) AS g"
    )
    cad_geom = (
        "ST_MakeEnvelope(g.x, g.y, g.x + CAST(:pw AS float) * 0.45, "
        "g.y + CAST(:ph AS float) * 0.4, 4326)"
    )
    await session.execute(
        text(
            "INSERT INTO cadastral_parcels (municipality_id, parcel_number, ko_name, geom, "
            "area_m2, dataset_version) "
            f"SELECT :m, 'S' || i, 'Podgorica III', ST_Multi({cad_geom}), "
            f"ROUND(CAST(ST_Area(CAST({cad_geom} AS geography)) AS numeric), 1), :v "
            f"FROM generate_series(0, CAST(:n_parcels AS int) - 1) AS i, {cell}"
        ),
        params,
    )
    up_geom = (
        "ST_MakeEnvelope(g.x + CAST(:pw AS float) * 0.1, g.y, g.x + CAST(:pw AS float) * 0.45, "
        "g.y + CAST(:ph AS float) * 0.4, 4326)"
    )
    centre = (
        "ST_SetSRID(ST_MakePoint(g.x + CAST(:pw AS float) * 0.25, "
        "g.y + CAST(:ph AS float) * 0.2), 4326)"
    )
    await session.execute(
        text(
            "INSERT INTO urban_parcels (municipality_id, urban_parcel_number, geom, area_m2, "
            "block_id, document_id, dataset_version) "
            f"SELECT :m, 'SUP ' || p.i, ST_Multi(p.geom), "
            "ROUND(CAST(ST_Area(CAST(p.geom AS geography)) AS numeric), 1), b.id, d.id, :v "
            f"FROM (SELECT i, {up_geom} AS geom, {centre} AS c "
            f"      FROM generate_series(0, CAST(:n_parcels AS int) - 1) AS i, {cell}) AS p "
            "JOIN planning_documents d ON d.municipality_id = :m AND d.dataset_version = :v "
            "     AND ST_Intersects(d.coverage_geom, p.c) "
            "LEFT JOIN urban_blocks b ON b.municipality_id = :m AND b.dataset_version = :v "
            "     AND ST_Intersects(b.geom, p.c)"
        ),
        params,
    )
    links = await recompute_current_links(
        session,
        municipality_id=municipality_id,
        min_overlap_m2=min_overlap_m2,
        min_overlap_fraction=min_overlap_fraction,
    )
    for table, _, _ in TABLES:
        await session.execute(text(f"ANALYZE {table}"))
    await session.execute(text("ANALYZE parcel_links"))
    await session.commit()

    counts: dict[str, int] = {}
    for table, _, _ in TABLES:
        counts[table] = (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE dataset_version = :v"), params
            )
        ).scalar_one()
    counts["parcel_links"] = links["parcel_links"] if links else 0
    return counts


async def delete_dataset(session: AsyncSession, municipality_id: str, dataset_version: str) -> None:
    for table, _, _ in reversed(TABLES):
        await session.execute(
            text(f"DELETE FROM {table} WHERE municipality_id = :m AND dataset_version = :v"),
            {"m": municipality_id, "v": dataset_version},
        )
    await session.commit()


async def _main(
    name: str, url: str | None, municipality_id: str, synthetic: bool, upload_files: bool
) -> None:
    if url is None:
        from core.config import get_settings

        url = get_settings().database_url
    engine = create_async_engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            from core.config import get_settings

            thresholds = {
                "min_overlap_m2": get_settings().locate_min_overlap_m2,
                "min_overlap_fraction": get_settings().locate_min_overlap_fraction,
            }
            counts = await load_sample(session, name, municipality_id=municipality_id, **thresholds)
            if synthetic:
                bulk = await load_synthetic_bulk(
                    session, municipality_id=municipality_id, **thresholds
                )
                counts = {t: counts.get(t, 0) + bulk.get(t, 0) for t in counts}
        for table, count in counts.items():
            print(f"{table}: {count}")
        if upload_files:
            from core.config import get_settings

            storage = ObjectStorage(get_settings())
            uploaded = await asyncio.to_thread(upload_sample_files, storage, name, municipality_id)
            print(f"planning document files uploaded: {uploaded}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load a seed dataset into the location tables.")
    parser.add_argument("name", nargs="?", default="podgorica_sample")
    parser.add_argument("--url", default=None, help="SQLAlchemy URL (default: settings)")
    parser.add_argument("--municipality", default="podgorica")
    parser.add_argument(
        "--synthetic-bulk",
        action="store_true",
        help="also add ~300 synthetic documents and 10k cadastral/planned parcels for load tests",
    )
    parser.add_argument(
        "--upload-files",
        action="store_true",
        help="also upload placeholder PDFs for the sample documents to the S3 bucket "
        "(needs the S3_* settings)",
    )
    args = parser.parse_args()
    asyncio.run(
        _main(args.name, args.url, args.municipality, args.synthetic_bulk, args.upload_files)
    )
