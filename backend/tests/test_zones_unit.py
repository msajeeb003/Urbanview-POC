"""Zone tooling pieces that need no database: vocabulary vs the map palette, configuration,
matching registered documents, the report's markdown and files, the CLI parser."""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.municipality import load_profile
from core.zones.__main__ import build_parser
from core.zones.config import REPO_ROOT, available_municipalities, load_zone_config
from core.zones.report import ZoneReport, ZoneRow, render_markdown, write_report
from core.zones.schema import (
    ZONE_TYPES,
    document_identity,
    name_key,
    parse_bool,
    slugify,
    split_year,
)
from core.zones.staging import ExistingDocument, eregistri_id_from_url, match_existing


def test_zone_type_colours_are_the_map_palette():
    css = (REPO_ROOT / "frontend" / "src" / "styles" / "wireframe.css").read_text(encoding="utf-8")
    palette = dict(re.findall(r"--z-(\w+):\s*(#[0-9A-Fa-f]{6})", css))
    assert {t.code: t.colour.upper() for t in ZONE_TYPES} == {
        k: v.upper() for k, v in palette.items() if k in {t.code for t in ZONE_TYPES}
    }


def test_slugs_names_and_identity():
    assert slugify("Dajbabe – Ćemovsko polje") == "dajbabe-cemovsko-polje"
    assert slugify("Đurđevdanska ulica") == "djurdjevdanska-ulica"
    assert split_year("UP Nova Varoš Blok A (2010)") == ("UP Nova Varoš Blok A", 2010)
    assert split_year("DUP Titeks") == ("DUP Titeks", None)
    assert name_key("DUP Novi Grad 1 i 2 – izmjene i dopune") == name_key(
        "DUP - Novi grad 1 i 2 - Izmjene i dopune"
    )
    two = [
        {"document_name": "DUP Nova Varoš 2 – izmjene i dopune", "listed_year": 2008},
        {"document_name": "DUP Nova Varoš 2 – izmjene i dopune", "listed_year": 2010},
    ]
    assert document_identity(two[0]) != document_identity(two[1])
    assert document_identity({"eregistri_reference": " 4182 "}) == ("eregistri", "4182")
    assert parse_bool("Da") is True and parse_bool("") is None
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_podgorica_configuration():
    cfg = load_zone_config("podgorica")
    assert "podgorica" in available_municipalities()
    assert cfg.editing_crs == 25834 and cfg.root.name == "podgorica"
    assert cfg.reference.capture.is_file() and cfg.eregistri.snapshot.is_file()
    assert cfg.eregistri.details(4182).endswith("/PlanningDocument/Details/4182")
    types = load_profile("podgorica").terminology.document_types
    assert set(cfg.eregistri.types.values()) <= set(types)
    assert len(set(cfg.reference.zone_ids.values())) == len(cfg.reference.zone_ids)


def test_matching_registered_documents():
    existing = [
        ExistingDocument(
            1, "PUP Glavni grad (izvod)", None, "https://lamp.gov.me/PlanningDocument?m=PG"
        ),
        ExistingDocument(
            2,
            "Izmjena i dopuna DUP-a Novi Grad",
            None,
            "https://lamp.gov.me/PlanningDocument/Details/4182",
        ),
        ExistingDocument(3, "UP Stara Varoš", "4207", None),
        ExistingDocument(4, "DUP Konik", None, None),
        ExistingDocument(5, "DUP Konik", None, None),
    ]
    rows = [
        {"document_name": "UP Stara Varoš – izmjene i dopune", "eregistri_reference": "4207"},
        {"document_name": "DUP Novi Grad 1 i 2", "eregistri_reference": "4182"},
        {"document_name": "PUP  glavni GRAD (izvod)"},
        {"document_name": "DUP Konik"},
        {"document_name": "DUP Konik"},
        {"document_name": "DUP Konik"},
        {"document_name": "DUP Nepoznat", "eregistri_reference": "1"},
    ]
    assert match_existing(rows, existing) == [
        (3, "eregistri"),
        (2, "source_url"),
        (1, "name"),
        (4, "name"),
        (5, "name"),
        (None, "new"),  # each registered document is taken once
        (None, "new"),
    ]
    assert eregistri_id_from_url("https://lamp.gov.me/PlanningDocument/Details/9699") == "9699"
    assert eregistri_id_from_url("https://lamp.gov.me/PlanningDocument?m=PG") is None


def _report() -> ZoneReport:
    return ZoneReport(
        municipality_id="podgorica",
        dataset_version="podgorica-zones-20261003-1",
        status="staged",
        imported_by="tester",
        created_at=datetime(2026, 10, 3, tzinfo=UTC),
        published_at=None,
        zones=[
            ZoneRow(
                "konik",
                "Konik",
                "Residential",
                False,
                7.5,
                documents=2,
                adopted=1,
                in_progress=1,
                cadastral_parcels=120,
                urban_parcels=40,
            ),
            ZoneRow("rogami", "Rogami", "", True, 3.0, cadastral_parcels=9),
        ],
        documents=[
            {
                "zone_key": "konik",
                "zone_name": "Konik",
                "document_type": "DUP",
                "document_name": "DUP Konik – Sanacioni plan",
                "status": "adopted",
                "eregistri_reference": "4301",
                "adoption_date": "",
                "poc_coverage": False,
                "confirmed": True,
                "match_method": "new",
                "registered_name": None,
                "source_url": "",
                "notes": "a | b",
            },
            {
                "zone_key": "konik",
                "zone_name": "Konik",
                "document_type": "DUP",
                "document_name": "DUP Konik – Vrela Ribnička II",
                "status": "in_progress",
                "eregistri_reference": None,
                "adoption_date": "",
                "poc_coverage": False,
                "confirmed": False,
                "match_method": "new",
                "registered_name": None,
                "source_url": "",
                "notes": "",
            },
        ],
        totals={
            "zones": 2,
            "documents": 2,
            "documents_by_status": {"adopted": 1, "in_progress": 1},
            "poc_documents": 0,
            "confirmed_documents": 1,
            "registered_matches": 0,
            "new_documents": 2,
            "area_km2": 10.5,
            "cadastral_total": 130,
            "cadastral_outside": 1,
            "urban_total": 40,
            "urban_outside": 0,
        },
        warnings=["1 cadastral parcel(s) lie outside every zone"],
        validation={
            "problems": [
                {
                    "severity": "warning",
                    "code": "zone_type_missing",
                    "message": "rogami has no zone type",
                }
            ]
        },
    )


def test_report_markdown_and_files(tmp_path: Path):
    report = _report()
    md = render_markdown(report, title="Podgorica zones")
    assert "| Konik (`konik`)" not in md and "### Konik (`konik`)" in md
    assert "No documents (marked: no adopted plan)." in md
    assert "a / b" not in md  # notes are not in the zone table; pipes never break a table
    assert "## Sign-off" in md and "zone_type_missing" in md
    assert "Cadastral parcels: 130 (1 outside every zone)" in md
    paths = write_report(report, tmp_path / "out", title="Podgorica zones")
    assert [p.name for p in paths] == ["zones.csv", "documents.csv", "report.md", "report.json"]
    with (tmp_path / "out" / "zones.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["Zone ID"] == "konik" and rows[1]["No adopted plan"] == "yes"
    assert rows[0]["Cadastral parcels"] == "120"


def test_cli_parser():
    args = build_parser().parse_args(["import", "--dry-run", "--label", "x", "--require-confirmed"])
    assert args.command == "import" and args.dry_run and args.label == "x"
    args = build_parser().parse_args(["--municipality", "podgorica", "template", "--no-db"])
    assert args.municipality == "podgorica" and args.no_db
