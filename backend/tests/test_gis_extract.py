"""Vector PDF geometry extraction (core.gis.extract, build plan ticket 07) on synthetic sheets.

``tests/gis_synthetic.py`` draws a layered 1:1000 sheet: a plan boundary, parcels UP 1-4 (the
UP 3 / UP 4 edge only on the cadastral fallback layer, a stray sliver line in UP 1, a legend
outside the boundary), block A as a dotted line of filled circles, a dashed road centreline and a
land-use fill; and a sheet whose labels are filled glyph outlines.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("shapely")
pytest.importorskip("yaml")

import yaml  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from shapely.geometry import LineString, MultiPolygon, Point, Polygon  # noqa: E402

from core.gis.extract import geometry as G  # noqa: E402
from core.gis.extract.__main__ import _vertices, main  # noqa: E402
from core.gis.extract.extract import extract_document, read_labels  # noqa: E402
from core.gis.extract.glyphs import read_glyph_labels  # noqa: E402
from core.gis.extract.gpkg import (  # noqa: E402
    APPLICATION_ID,
    feature_rows,
    parse_blob,
    read_gpkg,
    write_gpkg,
)
from core.gis.extract.overlay import render_preview, render_sheet  # noqa: E402
from core.gis.extract.rules import (  # noqa: E402
    DocumentRules,
    LabelRule,
    LayerRule,
    Selector,
    SheetRule,
    find_rules,
    load_rules,
)
from core.gis.extract.sheet import load_sheet, style_clusters  # noqa: E402
from core.gis.inspect_gis import find_ogrinfo, gdal_env  # noqa: E402
from tests import gis_synthetic as syn  # noqa: E402

M2 = syn.K**2  # ground m² per pt²


def _rules(**changes: object) -> DocumentRules:
    data = yaml.safe_load(syn.RULES_YAML)
    for path, value in changes.items():
        node = data
        *parents, leaf = path.split(".")
        for p in parents:
            node = node[p]
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return DocumentRules.model_validate(data)


@pytest.fixture(scope="module")
def sheet_a() -> bytes:
    return syn.plan_sheet()


@pytest.fixture(scope="module")
def extraction(sheet_a: bytes):
    return extract_document(_rules(), lambda s: sheet_a, document_id=42)


def _by_key(extraction, layer: str) -> dict:
    return {f.key: f for f in extraction.layers[layer]}


# --- rules ------------------------------------------------------------------------------------


def test_rules_validate_methods_and_references() -> None:
    with pytest.raises(ValidationError, match="needs `select`"):
        LayerRule(method="polygonize")
    with pytest.raises(ValidationError, match="classify_from"):
        LayerRule(method="classify")
    with pytest.raises(ValidationError, match="one capture group"):
        LabelRule(select=[Selector(layer="X")], pattern="UP \\d+", attribute="urban_parcel_number")
    with pytest.raises(ValidationError, match="#rrggbb"):
        Selector(stroke="red")
    with pytest.raises(ValidationError, match="unknown target layers"):
        SheetRule(id="s", file="f.pdf", scale=1000, layers=["cadastral_parcels"])
    data = yaml.safe_load(syn.RULES_YAML)
    data["layers"]["urban_parcels"]["closing"] = ["zones"]
    with pytest.raises(ValidationError, match="refers to 'zones'"):
        DocumentRules.model_validate(data)
    data = yaml.safe_load(syn.RULES_YAML)
    data["sheets"][0]["layers"] = ["plan_boundary"]
    with pytest.raises(ValidationError, match="no sheet supplies it"):
        DocumentRules.model_validate(data)


def test_find_rules_by_sheet_checksum(tmp_path: Path, sheet_a: bytes) -> None:
    digest = hashlib.sha256(sheet_a).hexdigest()
    data = yaml.safe_load(syn.RULES_YAML)
    data["sheets"][0]["sha256"] = digest
    (tmp_path / "synthetic.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a rules file", encoding="utf-8")
    found = find_rules("podgorica", digest, directory=tmp_path)
    assert found is not None
    path, rules = found
    assert path.name == "synthetic.yaml"
    assert rules.sheet_for_sha256(digest).id == "a"
    assert find_rules("podgorica", "0" * 64, directory=tmp_path) is None
    assert load_rules(path) == rules


# --- sheet reading ------------------------------------------------------------------------------


def test_sheet_keeps_layers_styles_and_frames(sheet_a: bytes) -> None:
    rule = SheetRule(id="a", file="a.pdf", scale=1000, offset_m=(10.0, 20.0), layers=[])
    sheet = load_sheet(sheet_a, rule)
    layers = {p.layer for p in sheet.paths}
    assert {"GRANICA", "PARCELE", "KATASTAR", "BLOKOVI", "SAOBRACAJ", "NAMJENA_SS"} <= layers
    assert {t.text for t in sheet.texts if t.layer == "OZNAKE"} >= {"UP 1", "UP 2", "UP 9"}
    # page (100, 100) -> local: x = 100 k + 10, y = (420 - 100) k + 20
    p = sheet.to_local(Point(100, 100))
    assert p.x == pytest.approx(100 * syn.K + 10) and p.y == pytest.approx(320 * syn.K + 20)
    back = sheet.to_page(p)
    assert back.x == pytest.approx(100) and back.y == pytest.approx(100)
    assert sheet.sheet_bbox((p.x, p.y, p.x, p.y)) == [100.0, 320.0, 100.0, 320.0]
    only = load_sheet(sheet_a, rule, keep=[Selector(layer="PARCELE")])
    assert {p.layer for p in only.paths} == {"PARCELE"}
    assert all(p.id.startswith("p1:") for p in only.paths)


def test_style_clusters_count_paths_per_style(sheet_a: bytes) -> None:
    sheet = load_sheet(sheet_a, SheetRule(id="a", file="a.pdf", scale=1000, layers=[]))
    rows = {(r["layer"], r["dash"]): r for r in style_clusters(sheet)}
    dots = rows[("BLOKOVI", "solid")]
    assert dots["paths"] == 98 and dots["fill"] == "#666666" and dots["max_size_mm"] <= 1.0
    assert rows[("SAOBRACAJ", "solid")]["paths"] == 28
    assert rows[("OZNAKE", "text")]["texts"][:1]  # a text-only layer is listed with samples


def test_styles_command_prints_clusters(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sheet_a: bytes
) -> None:
    pdf = tmp_path / "sheet-a.pdf"
    pdf.write_bytes(sheet_a)
    assert main(["styles", str(pdf), "--layer", "BLOK"]) == 0
    out = capsys.readouterr().out
    assert "BLOKOVI" in out and "PARCELE" not in out
    assert "3.53" in out  # the dots' spacing in mm (10 pt)


# --- the extraction -----------------------------------------------------------------------------


def test_parcels_polygonize_with_numbers(extraction) -> None:
    parcels = _by_key(extraction, "urban_parcels")
    assert sorted(parcels) == ["1", "2", "3", "4"]
    assert parcels["1"].attrs["urban_parcel_number"] == "1"
    # UP 1 lost the 0.2 pt sliver along its edge; the snap grid moves vertices by < 2.5 cm
    assert parcels["1"].geom.area == pytest.approx(150 * 149.8 * M2, rel=2e-3)
    assert parcels["2"].geom.area == pytest.approx(150 * 150 * M2, rel=2e-3)
    assert "sliver_removed" not in parcels["1"].qa_flags  # the sliver was a face of its own
    # UP 2 has one label: the cadastral line across it is ignored
    assert "fallback_face" not in parcels["2"].qa_flags
    # UP 3 / UP 4 share a face on the parcel layer: the cadastral fallback separates them and
    # the unlabelled piece north of y = 210 joins UP 4 (longest shared edge)
    for key in ("3", "4"):
        assert parcels[key].geom.area == pytest.approx(50 * 150 * M2, rel=3e-3)
        assert "fallback_face" in parcels[key].qa_flags
    assert parcels["1"].attrs["block_ref"] == "A" and parcels["2"].attrs["block_ref"] == "A"
    assert "block_ref" not in parcels["3"].attrs  # 20 % inside block A only


def test_blocks_roads_land_use_and_boundary(extraction) -> None:
    (boundary,) = extraction.layers["plan_boundary"]
    assert boundary.key == "coverage"
    assert boundary.geom.area == pytest.approx(500 * 320 * M2, rel=1e-3)
    (block,) = extraction.layers["urban_blocks"]
    assert block.key == "A" and block.attrs == {"block_ref": "A"}
    assert block.geom.area == pytest.approx(320 * 170 * M2, rel=2e-3)  # dots bridged on centres
    assert len(block.source_paths) == 98
    (road,) = extraction.layers["planned_traffic"]
    assert road.geom.geom_type == "MultiLineString" and len(road.geom.geoms) == 1
    assert road.geom.length == pytest.approx(498 * syn.K, rel=1e-3)  # 28 dashes, gaps bridged
    assert road.attrs["road_class"] == "street"
    (use,) = extraction.layers["planned_land_use"]
    assert use.attrs["code"] == "SS" and use.attrs["name"] == "Stanovanje"
    assert use.geom.area == pytest.approx(150 * 150 * M2, rel=1e-3)  # two triangles unioned


def test_qa_summary_counts_and_zero_slivers(extraction) -> None:
    qa = extraction.qa
    parcels = qa["layers"]["urban_parcels"]
    assert parcels["features"] == 4 and parcels["labelled"] == 4 and parcels["unlabelled"] == 0
    assert parcels["slivers_removed"] == 1
    assert parcels["outside_dropped"] == 1  # the legend box
    assert parcels["labels_outside"] == 1  # "UP 9" in the legend
    assert parcels["unlabelled_dropped"] == 1  # the ring between the parcels and the boundary
    assert parcels["flags"] == {"fallback_face": 2}
    assert parcels["area_m2"] == pytest.approx(
        sum(f.geom.area for f in extraction.layers["urban_parcels"]), abs=0.5
    )
    assert qa["layers"]["planned_traffic"]["length_m"] == pytest.approx(498 * syn.K, abs=0.5)
    assert qa["totals"] == {"features": 8, "invalid_after_cleanup": 0, "slivers_after_cleanup": 0}
    assert qa["sheets"]["a"]["invalid_after_cleanup"] == 0
    assert qa["sheets"]["a"]["slivers_after_cleanup"] == 0
    assert qa["sheets"]["a"]["features"]["urban_parcels"] == 4
    assert qa["document_id"] == 42 and qa["extractor_version"] == "vector-pdf-1"
    json.dumps(qa)  # plain JSON for the job record


def test_every_feature_carries_its_source(extraction) -> None:
    for feats in extraction.layers.values():
        for f in feats:
            props = f.properties(42, "synthetic")
            assert props["document_id"] == 42 and props["document_ref"] == "synthetic"
            assert props["sheet"] == "a" and props["page"] == 1
            assert props["source_paths"] and all(p.startswith("p1:") for p in props["source_paths"])
            x0, y0, x1, y1 = props["source_bbox"]
            assert 0 <= x0 <= x1 <= syn.W and 0 <= y0 <= y1 <= syn.H
    up1 = _by_key(extraction, "urban_parcels")["1"]
    # sheet PDF points, origin bottom-left: page y 100.2-250 -> 170-319.8
    assert up1.source_bbox == pytest.approx([100.0, 170.0, 250.0, 319.8], abs=0.15)
    props = up1.properties(42, "synthetic")
    assert props["label_text"] == "UP 1" and props["label_bbox"][0] == pytest.approx(160, abs=1)
    assert props["qa_flags"] == []
    assert props["area_m2"] == round(up1.geom.area, 1)


def test_rerun_gives_the_same_polygons(sheet_a: bytes, extraction) -> None:
    again = extract_document(_rules(), lambda s: sheet_a, document_id=42)
    assert again.qa["digest"] == extraction.qa["digest"]
    for layer, feats in extraction.layers.items():
        assert [f.key for f in again.layers[layer]] == [f.key for f in feats]
        assert [f.geom.wkb for f in again.layers[layer]] == [f.geom.wkb for f in feats]


def test_without_fallback_the_pair_is_one_multi_label_face(sheet_a: bytes) -> None:
    ex = extract_document(_rules(**{"layers.urban_parcels.fallback": None}), lambda s: sheet_a)
    parcels = _by_key(ex, "urban_parcels")
    assert sorted(parcels) == ["1", "2", "4"]  # the deepest label names the pair
    assert parcels["4"].qa_flags == {"multi_label"}
    assert parcels["4"].label_text == "UP 3 | UP 4"
    assert ex.qa["layers"]["urban_parcels"]["multi_label"] == 1


def test_keep_all_flags_unlabelled_faces(sheet_a: bytes) -> None:
    ex = extract_document(_rules(**{"layers.urban_parcels.keep": "all"}), lambda s: sheet_a)
    faces = [f for f in ex.layers["urban_parcels"] if "unlabelled" in f.qa_flags]
    assert [f.key for f in faces] == ["face-0001"]
    assert ex.qa["layers"]["urban_parcels"]["unlabelled"] == 1


def test_label_overrides_fix_and_drop_labels(sheet_a: bytes) -> None:
    rules = _rules(label_overrides={"a:UP 2": "UP 2a", "UP 1": ""})
    ex = extract_document(rules, lambda s: sheet_a)
    parcels = _by_key(ex, "urban_parcels")
    assert "2a" in parcels and "1" not in parcels and "2" not in parcels
    notes = ex.qa["layers"]["urban_parcels"]["label_notes"]
    assert [n["kind"] for n in notes] == ["dropped_by_override"] and notes[0]["text"] == "UP 1"


def test_second_sheet_offset_estimate_and_merge(sheet_a: bytes) -> None:
    shift = (13.0, -7.0)  # the same drawing plotted 13 pt right, 7 pt up
    sheet_b = syn.plan_sheet(shift=shift)
    rule_a = SheetRule(id="a", file="a.pdf", scale=1000, layers=[])
    sel = [Selector(layer="PARCELE")]
    dx, dy, votes, share = G.estimate_offset(
        _vertices(load_sheet(sheet_a, rule_a, keep=sel), sel),
        _vertices(load_sheet(sheet_b, rule_a, keep=sel), sel),
    )
    assert (dx, dy) == pytest.approx((-13 * syn.K, -7 * syn.K), abs=2e-3)
    assert votes > 0 and 0 < share <= 1
    data = yaml.safe_load(syn.RULES_YAML)
    second = dict(data["sheets"][0], id="b", file="sheet-b.pdf", offset_m=[dx, dy])
    data["sheets"].append(second)
    rules = DocumentRules.model_validate(data)
    pdfs = {"a": sheet_a, "b": sheet_b}
    ex = extract_document(rules, lambda s: pdfs[s.id])
    assert sorted(_by_key(ex, "urban_parcels")) == ["1", "2", "3", "4"]
    assert len(ex.layers["urban_blocks"]) == 1
    assert len(ex.layers["planned_land_use"]) == 1
    assert len(ex.layers["planned_traffic"]) == 1
    # the four parcels and the unlabelled ring around them (dropped afterwards: keep labelled)
    assert ex.qa["layers"]["urban_parcels"]["duplicates_removed"] == 5
    assert ex.qa["totals"]["slivers_after_cleanup"] == 0


# --- glyph labels ------------------------------------------------------------------------------


def test_glyph_outlines_are_read_as_labels() -> None:
    pdf = syn.glyph_sheet({"UP 12": (100, 100), "UP 7a": (300, 200), "B4/1": (100, 300)})
    sheet = load_sheet(pdf, SheetRule(id="g", file="g.pdf", scale=1000, layers=[]))
    rule = LabelRule(
        source="glyphs",
        select=[Selector(layer="BROJEVI")],
        pattern=r"UP\s*(\d+[a-z]?)",
        attribute="urban_parcel_number",
    )
    read = {gl.text: gl for gl in read_glyph_labels(sheet, rule)}
    assert set(read) == {"UP 12", "UP 7a", "B4/1"}
    assert all(gl.score >= rule.min_score for gl in read.values())
    assert read["UP 12"].bbox == pytest.approx((100, 91.5, 130, 100), abs=0.6)
    assert len(read["UP 12"].path_ids) == 4  # one filled path per glyph
    labels, notes = read_labels(sheet, rule, {})
    assert [(lab.value, lab.text) for lab in labels] == [("12", "UP 12"), ("7a", "UP 7a")]
    assert notes == []
    assert labels[0].bbox == pytest.approx((100, 320, 130, 328.5), abs=0.6)  # sheet frame


# --- geometry helpers ---------------------------------------------------------------------------


def test_reduce_piece_dots_and_dashes() -> None:
    dot = Point(5, 5).buffer(0.4)
    assert G.reduce_piece(dot).geom_type == "Point"
    dash = Polygon([(0, 0), (3, 0), (3, 0.4), (0, 0.4)])
    axis = G.reduce_piece(dash)
    assert axis.geom_type == "LineString" and axis.length == pytest.approx(3)
    assert axis.centroid.y == pytest.approx(0.2)


def test_bridge_closes_gaps_and_undershoots() -> None:
    dots = [Point(x, 0) for x in range(0, 10, 2)]  # 2 m apart
    links = G.bridge(dots, 2.5)
    assert len(links) == 4 and all(ln.length == pytest.approx(2) for ln in links)
    # a line ending 0.3 m short of another (a T undershoot) gets a connector to it
    links = G.bridge([LineString([(0, 0), (10, 0)]), LineString([(5, 0.3), (5, 5)])], 0.5)
    assert any(ln.length == pytest.approx(0.3) for ln in links)


def test_clean_polygon_repairs_and_drops_slivers() -> None:
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    geom, flags, counts = G.clean_polygon(bowtie, 2.0)
    assert geom is not None and geom.is_valid and "invalid_fixed" in flags
    sliver = Polygon([(0, 0), (40, 0), (40, 0.1), (0, 0.1)])
    assert G.clean_polygon(sliver, 2.0)[0] is None
    square = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    both = MultiPolygon([square, Polygon([(30, 0), (70, 0), (70, 0.1), (30, 0.1)])])
    geom, flags, counts = G.clean_polygon(both, 2.0)
    assert geom.equals(square) and counts["slivers_removed"] == 1 and "sliver_removed" in flags
    assert not G.is_sliver(geom, 2.0)


def test_hole_faces_of_ribbons_sit_on_the_centre_lines() -> None:
    # a 10 x 10 square drawn as four 0.4 m wide filled ribbons
    w = 0.2
    ribbons = [
        Polygon([(-w, -w), (10 + w, -w), (10 + w, w), (-w, w)]),
        Polygon([(-w, 10 - w), (10 + w, 10 - w), (10 + w, 10 + w), (-w, 10 + w)]),
        Polygon([(-w, -w), (w, -w), (w, 10 + w), (-w, 10 + w)]),
        Polygon([(10 - w, -w), (10 + w, -w), (10 + w, 10 + w), (10 - w, 10 + w)]),
    ]
    (face,) = [f for f in G.hole_faces(ribbons, 0.1) if f.area > 1]
    assert face.area == pytest.approx(100, rel=1e-3)
    assert face.bounds == pytest.approx((0, 0, 10, 10), abs=1e-3)


# --- output -----------------------------------------------------------------------------------


def test_geopackage_round_trip(tmp_path: Path, extraction) -> None:
    path = tmp_path / "synthetic.gpkg"
    counts = write_gpkg(path, feature_rows(extraction), description="Synthetic plan")
    assert counts == {
        "plan_boundary": 1,
        "urban_parcels": 4,
        "urban_blocks": 1,
        "planned_land_use": 1,
        "planned_traffic": 1,
    }
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    types = dict(con.execute("SELECT table_name, geometry_type_name FROM gpkg_geometry_columns"))
    assert (
        types["urban_parcels"] == "MULTIPOLYGON" and types["planned_traffic"] == "MULTILINESTRING"
    )
    blob = con.execute("SELECT geom FROM urban_parcels ORDER BY fid LIMIT 1").fetchone()[0]
    con.close()
    assert blob[:2] == b"GP" and parse_blob(blob).geom_type == "MultiPolygon"
    back = read_gpkg(path)
    parcels = {props["feature_key"]: (geom, props) for geom, props in back["urban_parcels"]}
    assert sorted(parcels) == ["1", "2", "3", "4"]
    geom, props = parcels["3"]
    original = _by_key(extraction, "urban_parcels")["3"]
    assert geom.equals(original.geom)
    assert props["qa_flags"] == ["fallback_face"] and props["document_id"] == 42
    assert props["source_paths"] == sorted(set(original.source_paths), key=lambda p: int(p[3:]))
    assert props["source_bbox"] == original.source_bbox
    assert props["urban_parcel_number"] == "3" and props["block_ref"] is None


def test_geopackage_is_readable_by_gdal(tmp_path: Path, extraction) -> None:
    ogrinfo = find_ogrinfo()
    if ogrinfo is None:
        pytest.skip("ogrinfo not available")
    path = tmp_path / "synthetic.gpkg"
    write_gpkg(path, feature_rows(extraction))
    run = subprocess.run(
        [ogrinfo, "-ro", "-so", "-al", str(path)],
        capture_output=True,
        text=True,
        env=gdal_env(ogrinfo),
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "Layer name: urban_parcels" in run.stdout and "Feature Count: 4" in run.stdout
    assert "Geometry: Multi Polygon" in run.stdout


def test_overlay_world_file_maps_pixels_to_the_local_frame(
    tmp_path: Path, sheet_a: bytes, extraction
) -> None:
    sheet = extraction.sheets["a"]
    png = render_sheet(sheet_a, sheet, tmp_path / "a.png", dpi=72)
    a, d, b, e, c, f = (float(v) for v in png.with_suffix(".pgw").read_text().split())
    assert (d, b) == (0.0, 0.0)
    assert a == pytest.approx(syn.K, rel=1e-6) and e == pytest.approx(-syn.K, rel=1e-6)
    # the centre of the top-left pixel is page (0.5, 0.5)
    assert c == pytest.approx(0.5 * syn.K) and f == pytest.approx((syn.H - 0.5) * syn.K)
    preview = render_preview(sheet_a, sheet, extraction.layers["urban_parcels"], tmp_path / "p.png")
    assert preview.stat().st_size > 1000


def test_run_command_writes_gpkg_qa_and_overlays(
    tmp_path: Path, sheet_a: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "sheet-a.pdf").write_bytes(sheet_a)
    rules = tmp_path / "synthetic.yaml"
    rules.write_text(syn.RULES_YAML, encoding="utf-8")
    out = tmp_path / "out"
    code = main(
        ["run", str(rules), "--source", str(source), "--out", str(out), "--overlay", "--preview"]
    )
    assert code == 0
    assert (out / "synthetic.gpkg").exists() and (out / "synthetic-a.pgw").exists()
    assert (out / "synthetic-a-preview.png").exists()
    qa = json.loads((out / "synthetic.qa.json").read_text(encoding="utf-8"))
    assert qa["totals"]["slivers_after_cleanup"] == 0
    assert "urban_parcels" in capsys.readouterr().out
