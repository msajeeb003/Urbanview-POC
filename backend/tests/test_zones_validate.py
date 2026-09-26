"""core.zones.validate: reading the QGIS outputs and the checks the import relies on."""

from __future__ import annotations

import itertools
import json
from datetime import date

import pytest
from shapely.geometry import Polygon, box, mapping

from core.zones import gpkg, schema
from core.zones.config import ValidationConfig
from core.zones.validate import (
    DocumentRecord,
    ZoneDataset,
    ZoneRecord,
    area_m2,
    read_dataset,
    validate,
)

TYPES = ("DUP", "PUP", "PGR", "UP")
CONFIG = ValidationConfig(overlap_tolerance_m2=1.0, gap_tolerance_m2=1.0)
TODAY = date(2026, 9, 26)
X0, Y0 = 500_000.0, 4_700_000.0  # EPSG:25834, near Podgorica
_FIDS = itertools.count(1)


def _box(x1, y1, x2, y2):
    return box(X0 + x1, Y0 + y1, X0 + x2, Y0 + y2)


def _zone(zone_id, geometry, *, fid=None, name=None, zone_type="residential", no_plan=False):
    return ZoneRecord(
        fid=fid or next(_FIDS),
        zone_id=zone_id,
        name=name if name is not None else zone_id.replace("-", " ").title(),
        zone_type=zone_type,
        general_planning_summary="",
        notes="",
        no_adopted_plan=no_plan,
        geometry=geometry,
    )


def _doc(row, zone_id, name, **overrides):
    values = {name: "" for name in schema.DOCUMENT_FIELD_NAMES}
    values.update(
        zone_id=zone_id,
        document_name=name,
        document_type="DUP",
        status="adopted",
        eregistri_reference=f"ref-{row}",
        source_url="https://lamp.gov.me/PlanningDocument/Details/1",
        confirmed=True,
        poc_coverage=False,
        listed_year=None,
        adoption_date=None,
    )
    values.update(overrides)
    return DocumentRecord(row=row, values=values)


def _grid():
    """Four 100 m squares sharing their edges: a clean partition."""
    return [
        _zone("a", _box(0, 0, 100, 100)),
        _zone("b", _box(100, 0, 200, 100), zone_type="commercial"),
        _zone("c", _box(0, 100, 100, 200), zone_type="mixed"),
        _zone("d", _box(100, 100, 200, 200), zone_type="green_recreation"),
    ]


def _check(zones, documents=None, **kwargs):
    if documents is None:
        documents = [_doc(i, z.zone_id, f"DUP {z.zone_id}") for i, z in enumerate(zones, 1)]
    dataset = ZoneDataset(zones, documents, 25834, None, None)
    kwargs.setdefault("config", CONFIG)
    return validate(dataset, document_types=TYPES, today=TODAY, **kwargs)


def test_clean_partition_passes():
    report = _check(_grid())
    assert report.ok and report.warnings == [], report.to_markdown()
    assert report.stats["total_area_m2"] == 40_000.0
    assert report.stats["adopted_per_zone"] == {"a": 1, "b": 1, "c": 1, "d": 1}
    assert report.stats["documents_by_status"] == {"adopted": 4, "in_progress": 0, "superseded": 0}
    assert (report.stats["overlap_pairs"], report.stats["gaps"]) == (0, 0)
    assert "No problems found" in report.to_markdown()
    assert json.dumps(report.to_json())  # plain data


def test_overlap_above_and_within_tolerance():
    big = _check([_zone("a", _box(0, 0, 100, 100)), _zone("b", _box(90, 0, 200, 100))])
    (overlap,) = big.errors
    assert overlap.code == "zones_overlap" and overlap.zone_ids == ("a", "b")
    assert overlap.area_m2 == pytest.approx(1000.0)
    x, y = overlap.location
    assert X0 + 90 <= x <= X0 + 100 and Y0 <= y <= Y0 + 100

    sliver = _check([_zone("a", _box(0, 0, 100, 100)), _zone("b", _box(99.995, 0, 200, 100))])
    assert sliver.ok
    assert [(p.code, p.area_m2) for p in sliver.warnings] == [("zones_overlap", 0.5)]


def test_holes_inside_the_zones_and_the_extent():
    frame = [
        _zone("left", _box(0, 0, 10, 30)),
        _zone("right", _box(20, 0, 30, 30)),
        _zone("bottom", _box(10, 0, 20, 10)),
        _zone("top", _box(10, 20, 20, 29.95)),  # leaves a 10 x 0.05 m sliver open to the top
    ]
    report = _check(frame)
    gaps = [p for p in report.errors if p.code == "zones_gap"]
    assert len(gaps) == 1 and gaps[0].area_m2 == pytest.approx(100.0)
    assert set(gaps[0].zone_ids) == {"left", "right", "bottom", "top"}
    # the open sliver is not a hole; with an extent it is part of the extent in no zone
    extent = _box(0, 0, 30, 30)
    with_extent = _check(frame, extent=extent)
    extra = [p for p in with_extent.problems if p.code == "zones_gap" and p.severity == "warning"]
    assert [p.area_m2 for p in extra] == [0.5]
    with pytest.raises(ValueError, match="EPSG:4326"):
        _check(frame, extent=extent, extent_srs_id=4326)


def test_invalid_empty_and_missing_geometries():
    bow_tie = Polygon([(X0, Y0), (X0 + 10, Y0 + 10), (X0 + 10, Y0), (X0, Y0 + 10)])
    report = _check(
        [_zone("bow", bow_tie), _zone("none", None), _zone("empty", Polygon())], documents=[]
    )
    codes = {p.code: p for p in report.errors}
    assert "Self-intersection" in codes["zone_invalid_geometry"].message
    assert codes["zone_invalid_geometry"].location == (X0 + 5, Y0 + 5)
    assert "zone_missing_geometry" in codes and "zone_empty" in codes


def test_zone_attributes():
    zones = [
        _zone("a", _box(0, 0, 100, 100)),
        _zone("a", _box(100, 0, 200, 100), name="Other"),
        _zone("Bad Id", _box(0, 100, 100, 200), name="A", zone_type="industrial"),
        _zone("e", _box(100, 100, 200, 200), zone_type=""),
    ]
    report = _check(zones, documents=[_doc(1, "a", "DUP a"), _doc(2, "e", "DUP e")])
    errors = report.codes("error")
    assert errors["zone_id_duplicate"] == 1
    assert errors["zone_id_format"] == 1
    assert errors["zone_name_duplicate"] == 1  # "A" and "a" fold to the same name
    assert errors["zone_type_invalid"] == 1
    assert report.codes("warning")["zone_type_missing"] == 1


def test_adopted_document_rule_and_no_adopted_plan():
    zones = [
        _zone("a", _box(0, 0, 100, 100)),
        _zone("b", _box(100, 0, 200, 100)),  # only an in-progress plan
        _zone("c", _box(0, 100, 100, 200), no_plan=True),  # knowingly none: fine
        _zone("d", _box(100, 100, 200, 200), no_plan=True),  # flag contradicts its list
        _zone("e", _box(200, 0, 300, 100)),  # nothing listed at all
    ]
    documents = [
        _doc(1, "a", "DUP A"),
        _doc(2, "b", "DUP B", status="in_progress", eregistri_reference=""),
        _doc(3, "d", "DUP D"),
    ]
    report = _check(zones, documents)
    missing = [p.zone_id for p in report.errors if p.code == "zone_without_adopted_document"]
    assert missing == ["b", "e"]
    warnings = {(p.code, p.zone_id) for p in report.warnings}
    assert ("zone_no_adopted_plan_contradiction", "d") in warnings
    assert ("zone_without_documents", "e") in warnings
    assert not any(p.zone_id == "c" for p in report.problems)
    assert report.stats["adopted_per_zone"] == {"a": 1, "b": 0, "c": 0, "d": 1, "e": 0}


def test_document_checks():
    documents = [
        _doc(1, "a", "DUP A"),
        _doc(2, "b", "DUP A copy", eregistri_reference="ref-1"),  # same registry id, zone b
        _doc(3, "zz", "DUP Unknown"),
        _doc(4, "c", "XYZ C", document_type="XYZ", status="approved"),
        _doc(5, "d", "DUP D", adoption_date=date(2030, 1, 1), source_url="lamp.gov.me"),
        _doc(6, "d", "DUP Old", eregistri_note="PLANKI DOKUMENT JE NEVAZECI."),
        _doc(7, "", "DUP Nowhere", confirmed=False, eregistri_reference=""),
    ]
    report = _check(_grid(), documents)
    errors = {p.code: p for p in report.errors}
    duplicate = errors["document_duplicate"]
    assert duplicate.zone_ids == ("a", "b") and "row 1" in duplicate.message
    assert errors["document_zone_unknown"].document == "row 3: DUP Unknown"
    assert "document_type_invalid" in errors and "document_status_invalid" in errors
    assert "document_adoption_in_future" in errors and "document_zone_missing" in errors
    warnings = report.codes("warning")
    assert warnings["document_source_url_invalid"] == 1
    assert warnings["document_eregistri_invalid_but_adopted"] == 1
    assert warnings["document_not_confirmed"] == 1
    assert report.stats["documents_by_status"]["other"] == 1

    strict = _check(_grid(), documents, config=ValidationConfig(require_confirmed=True))
    assert strict.codes("error")["document_not_confirmed"] == 1


def _write_gpkg(path):
    conn = gpkg.create(path)
    gpkg.add_feature_table(conn, "zones", fields=schema.ZONE_FIELDS, srs=gpkg.srs_for(25834))
    gpkg.insert_features(
        conn,
        "zones",
        [
            (_box(0, 0, 100, 100), {"zone_id": "a", "name": "A", "zone_type": "Residential"}),
            (_box(100, 0, 200, 100), {"zone_id": "b", "name": "B", "no_adopted_plan": 1}),
        ],
        srs_id=25834,
    )
    gpkg.add_attribute_table(conn, "zone_documents", fields=schema.DOCUMENT_FIELDS)
    gpkg.insert_rows(
        conn,
        "zone_documents",
        [
            {
                "zone_id": "a",
                "document_name": "DUP A",
                "document_type": "DUP",
                "status": "Adopted",
                "adoption_date": "2009-05-12",
                "listed_year": 2009,
                "confirmed": 1,
            },
            {"zone_id": "a", "document_name": "DUP A2", "confirmed": "maybe"},
        ],
    )
    conn.commit()
    conn.close()


def test_read_geopackage(tmp_path):
    path = tmp_path / "zones.gpkg"
    _write_gpkg(path)
    dataset = read_dataset(path, path)
    assert dataset.srs_id == 25834 and [z.zone_id for z in dataset.zones] == ["a", "b"]
    assert dataset.zones[0].zone_type == "residential" and dataset.zones[1].no_adopted_plan
    first, second = dataset.documents
    assert first.values["status"] == "adopted" and first.values["confirmed"] is True
    assert first.values["adoption_date"] == date(2009, 5, 12)
    assert first.values["listed_year"] == 2009 and first.fid == 1
    assert second.values["confirmed"] is False
    assert [p.code for p in second.problems] == ["document_field_invalid"]
    report = validate(dataset, document_types=TYPES, config=CONFIG, today=TODAY)
    assert "document_field_invalid" in report.codes("error")
    with pytest.raises(ValueError, match="no layer 'missing'"):
        read_dataset(path, path, documents_layer="missing")


def test_read_geojson_and_csv(tmp_path):
    # two 0.001 degree squares overlapping by a tenth, at Podgorica's latitude
    lon, lat = 19.26, 42.44
    a = box(lon, lat, lon + 0.001, lat + 0.001)
    b = box(lon + 0.0009, lat, lon + 0.002, lat + 0.001)
    features = [
        {"type": "Feature", "geometry": mapping(g), "properties": {"zone_id": z, "name": z}}
        for z, g in (("a", a), ("b", b))
    ]
    features.append(
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[lon, lat], [lon, lat + 1]]},
            "properties": {"zone_id": "line", "name": "Line"},
        }
    )
    zones_path = tmp_path / "zones.geojson"
    zones_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    csv_path = tmp_path / "zone_documents.csv"
    csv_path.write_text(
        "﻿zone_id;document_name;document_type;status;adoption_date;extra\n"
        "a;DUP Momišići C;DUP;adopted;12.5.2009.;x\n"
        "b;DUP B;DUP;adopted;31.02.2009;\n"
        ";;;;;\n",
        encoding="utf-8",
    )
    dataset = read_dataset(zones_path, csv_path)
    assert dataset.srs_id == 4326
    line = dataset.zones[2]
    assert line.geometry is None and [p.code for p in line.problems] == ["zone_not_polygon"]
    docs = dataset.documents
    assert len(docs) == 2 and docs[0].values["adoption_date"] == date(2009, 5, 12)
    assert docs[0].label == "row 1: DUP Momišići C"
    assert [p.code for p in docs[1].problems] == ["document_adoption_date_invalid"]

    report = validate(dataset, document_types=TYPES, config=CONFIG, today=TODAY)
    (overlap,) = [p for p in report.errors if p.code == "zones_overlap"]
    expected = area_m2(a.intersection(b), geographic=True)
    assert overlap.area_m2 == pytest.approx(expected, rel=1e-3)
    assert 880 < overlap.area_m2 < 940  # ~ 8.2 m x 110.6 m
    assert report.codes("error")["zone_missing_geometry"] == 0  # the line says why

    bad = tmp_path / "bad.csv"
    bad.write_text("zone_id,document_name\n", encoding="utf-8")
    with pytest.raises(ValueError, match="document_type, status"):
        read_dataset(zones_path, bad)
