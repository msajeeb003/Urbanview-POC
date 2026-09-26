"""The QGIS template without a database: the GeoPackage's layers, rows and default styles, the
project XML (palette, forms, relation, relative sources, the atlas layout), the overwrite guard."""

from __future__ import annotations

import asyncio
import csv
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from shapely.geometry import box

from core.zones import gpkg, qgis
from core.zones.config import REPO_ROOT, load_zone_config
from core.zones.schema import DOCUMENT_FIELD_NAMES, ZONE_FIELD_NAMES, ZONE_TYPES
from core.zones.template import build_template, typed


def _config(tmp_path: Path):
    cfg = load_zone_config("podgorica")
    root = tmp_path / "podgorica"
    root.mkdir()
    with (root / "zones.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ZONE_FIELD_NAMES))
        writer.writeheader()
        writer.writerow(
            {
                "zone_id": "konik",
                "name": "Konik",
                "zone_type": "residential",
                "no_adopted_plan": "false",
            }
        )
        writer.writerow({"zone_id": "rogami", "name": "Rogami", "no_adopted_plan": "true"})
    with (root / "zone_documents.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(DOCUMENT_FIELD_NAMES))
        writer.writeheader()
        writer.writerow(
            {
                "zone_id": "konik",
                "document_name": "DUP Konik – Sanacioni plan",
                "document_type": "DUP",
                "status": "adopted",
                "confirmed": "false",
                "listed_year": "2010",
                "adoption_date": "2010-05-04",
            }
        )
    return replace(
        cfg,
        root=root,
        geopackage=root / "podgorica_zones.gpkg",
        project=root / "podgorica_zones.qgz",
        zones_seed=root / "zones.csv",
        documents=root / "zone_documents.csv",
        zones_export=root / "zones.geojson",
        reports=root / "reports",
    )


def test_template_without_database(tmp_path: Path):
    cfg = _config(tmp_path)
    result = asyncio.run(build_template(cfg, session=None))
    assert result.layers == {"zones": 2, "zone_documents": 1}
    assert any("reference layers skipped" in w for w in result.warnings)

    zones = gpkg.read_layer(cfg.geopackage, "zones")
    assert zones.srs_id == 25834 and zones.fields == list(ZONE_FIELD_NAMES)
    assert [f.properties["zone_id"] for f in zones.features] == ["konik", "rogami"]
    assert all(f.geometry is None for f in zones.features)  # listed, to be drawn
    assert zones.features[1].properties["no_adopted_plan"] == 1
    docs = gpkg.read_layer(cfg.geopackage, "zone_documents")
    row = docs.features[0].properties
    assert row["listed_year"] == 2010 and row["adoption_date"] == "2010-05-04"
    assert row["confirmed"] == 0 and row["eregistri_reference"] is None
    with closing(sqlite3.connect(cfg.geopackage)) as conn:
        styles = dict(conn.execute("SELECT f_table_name, styleQML FROM layer_styles"))
    assert set(styles) == {"zones", "zone_documents"}
    ET.fromstring(styles["zones"].split("\n", 1)[1])  # well-formed QML

    with zipfile.ZipFile(cfg.project) as zf:
        assert zf.namelist() == ["podgorica_zones.qgs"]
        xml = zf.read("podgorica_zones.qgs").decode("utf-8")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.find("projectCrs/spatialrefsys/authid").text == "EPSG:25834"
    sources = [el.text for el in root.iter("datasource")]
    assert "./podgorica_zones.gpkg|layername=zones" in sources
    assert any(s.startswith("type=xyz&url=https") for s in sources)
    for zone_type in ZONE_TYPES:  # the map palette, 35 % fill
        assert qgis.rgba(zone_type.colour, 89) in xml
    values = [c.get("value") for c in root.iter("category")]
    assert values == [t.value for t in ZONE_TYPES] + [""]
    relation = root.find("relations/relation")
    assert relation is not None and relation.find("fieldRef").get("referencingField") == "zone_id"
    widgets = {
        f.get("name"): f.find("editWidget").get("type")
        for f in root.iter("field")
        if f.find("editWidget") is not None
    }
    assert widgets["zone_type"] == "ValueMap" and widgets["zone_id"] in (
        "TextEdit",
        "ValueRelation",
    )
    assert "ValueRelation" in widgets.values() and widgets["adoption_date"] == "DateTime"
    layout = root.find("Layouts/Layout")
    assert layout.get("name") == "Zone review" and layout.find("Atlas").get("enabled") == "1"
    items = {int(i.get("type")) for i in layout.iter("LayoutItem")}
    assert {65638, 65639, 65641, 65642, 65646} <= items
    assert root.find("avoidIntersectionsLayers/layer") is not None


def test_template_keeps_drawn_zones(tmp_path: Path):
    cfg = _config(tmp_path)
    asyncio.run(build_template(cfg, session=None))
    conn = sqlite3.connect(cfg.geopackage)
    conn.execute(
        "UPDATE zones SET geom = ? WHERE zone_id = 'konik'",
        (gpkg.encode_geometry(box(360000, 4700000, 361000, 4701000), 25834),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(FileExistsError, match="import"):
        asyncio.run(build_template(cfg, session=None))
    asyncio.run(build_template(cfg, session=None, overwrite=True))
    assert all(f.geometry is None for f in gpkg.read_layer(cfg.geopackage, "zones").features)


def test_typed_values():
    from core.zones.schema import DOCUMENT_FIELDS

    spec = {s.name: s for s in DOCUMENT_FIELDS}
    assert typed(spec["confirmed"], "Da") is True and typed(spec["confirmed"], "") is None
    assert typed(spec["listed_year"], "1995") == 1995 and typed(spec["listed_year"], "x") is None
    assert typed(spec["adoption_date"], "2004-03-01T00:00:00") == "2004-03-01"
    assert typed(spec["adoption_date"], "1.3.2004") == "1.3.2004"  # validation reports it


def test_pyqgis_script_compiles():
    source = (REPO_ROOT / "data" / "zones" / "qgis" / "build_project.py").read_text("utf-8")
    compile(source, "build_project.py", "exec")
    assert "from core" not in source  # runs inside QGIS, without the backend
