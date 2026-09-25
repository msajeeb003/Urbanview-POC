"""Geometry assessment (core.gis, build plan P0 gate 1) on synthetic sheets.

Each test builds the PDF it needs with pymupdf: a layered vector plan sheet (class A), the same
drawing without layers (B), a scanned raster (C) and a Word-like table page, plus the evidence
the georeferencing item reads (stated scale, viewport measure, grid labels, grid crosses).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("shapely")

from core.gis import assessment as A  # noqa: E402
from core.gis.inspect_gis import find_ogrinfo, gdal_env, inspect_gis  # noqa: E402
from core.gis.inspect_pdf import (  # noqa: E402
    InspectConfig,
    decode_glyph_ids,
    find_grid,
    fold,
    inspect_page,
    layer_key,
    looks_glyph_shifted,
    paper_name,
    parse_viewports,
)
from core.gis.sample import lattice_spacing, round_lattice, sample_faces  # noqa: E402
from core.municipality import GisProfile, load_gis_profile, load_profile  # noqa: E402

MM = 72 / 25.4  # points per millimetre
A1 = (841 * MM, 594 * MM)

PROFILE = GisProfile(
    crs_candidates=[
        {
            "epsg": 3908,
            "name": "MGI 1901 / Balkans zone 6",
            "easting": [6540000, 6680000],
            "northing": [4620000, 4780000],
        }
    ],
    sheet_titles={"urban_parcels": ["plan parcelacije"]},
    plan_layers={
        "plan_boundary": ["granica[ _-]*plana"],
        "urban_parcels": ["urbanisticka[ _-]*parcela"],
        "urban_parcel_labels": ["oznaka[ _-]*urb"],
        "land_use": [r"(^|[ _-])pov\w*[ _-]+za\b"],
        "geodetic_control": ["^mreza$"],
    },
)
CONFIG = InspectConfig.from_profile(PROFILE)


# --- builders -------------------------------------------------------------------------------------


def _plan_page(doc, layered: bool = True, labels_as_text: bool = True):  # noqa: ANN001, ANN202
    """An A1 plan at 1:1000: 3 x 3 parcels of 60 m, a plan boundary, a land-use fill, grid
    crosses every 100 m (100 mm), parcel ids, a title block with the stated scale."""
    page = doc.new_page(width=A1[0], height=A1[1])
    page.draw_rect(page.rect + (20, 20, -20, -20), width=1.0)  # sheet frame, on no layer

    def oc(name: str) -> int:
        return doc.add_ocg(name) if layered else 0

    parcels, boundary, land_use, grid, labels = (
        oc("UP_Urbanisticka parcela"),
        oc("X-GEO_SV$0$UP-GRANICA_PLANA"),
        oc("UP-Povrs za stanovanje-SS"),
        oc("X-GEO_SV$0$MREZA"),
        oc("UP-Oznaka urb parc"),
    )
    x0, y0, cell = 150 * MM, 120 * MM, 60 * MM
    for i in range(4):  # parcel grid lines
        page.draw_line((x0 + i * cell, y0), (x0 + i * cell, y0 + 3 * cell), width=0.5, oc=parcels)
        page.draw_line((x0, y0 + i * cell), (x0 + 3 * cell, y0 + i * cell), width=0.5, oc=parcels)
    outer = pymupdf.Rect(
        x0 - 10 * MM, y0 - 10 * MM, x0 + 3 * cell + 10 * MM, y0 + 3 * cell + 10 * MM
    )
    page.draw_rect(outer, color=(1, 0, 0), width=1.0, oc=boundary)
    page.draw_rect(
        pymupdf.Rect(x0, y0, x0 + cell, y0 + cell), color=None, fill=(1, 1, 0.6), oc=land_use
    )
    for gx in range(5):  # grid crosses 100 mm apart
        for gy in range(4):
            cx, cy = 100 * MM + gx * 100 * MM, 100 * MM + gy * 100 * MM
            page.draw_line((cx - 3 * MM, cy), (cx + 3 * MM, cy), width=0.2, oc=grid)
            page.draw_line((cx, cy - 3 * MM), (cx, cy + 3 * MM), width=0.2, oc=grid)
    n = 0
    for r in range(3):
        for c in range(3):
            n += 1
            point = (x0 + c * cell + 20 * MM, y0 + r * cell + 30 * MM)
            if labels_as_text:
                page.insert_text(point, f"UP {n}", fontsize=10, oc=labels)
    page.insert_text((650 * MM, 560 * MM), "PLAN PARCELACIJE", fontsize=14)
    page.insert_text((650 * MM, 575 * MM), "R 1:1000", fontsize=12)
    page.insert_text((650 * MM, 540 * MM), "LEGENDA", fontsize=12)
    return page


def _raster_page(doc):  # noqa: ANN001, ANN202
    page = doc.new_page(width=A1[0], height=A1[1])
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1200, 850), False)
    pix.set_rect(pix.irect, (235, 235, 235))
    page.insert_image(page.rect, pixmap=pix)
    return page


def _table_page(doc):  # noqa: ANN001, ANN202
    page = doc.new_page(width=297 * MM, height=210 * MM)
    for r in range(12):
        for c in range(6):
            rect = pymupdf.Rect(
                20 * MM + c * 40 * MM,
                20 * MM + r * 12 * MM,
                60 * MM + c * 40 * MM,
                32 * MM + r * 12 * MM,
            )
            page.draw_rect(rect, width=0.3)
            page.insert_text(
                (rect.x0 + 2, rect.y0 + 20), f"UP {r + 1}" if c == 0 else "1.2", fontsize=8
            )
    return page


def _viewport(units_per_pt: float, bbox: str) -> str:
    """An AutoCAD-style /VP entry: a viewport with a rectilinear /Measure."""
    measure = f"<</Type/Measure/Subtype/RL/X[<</C {units_per_pt}>>]>>"
    return f"<</Type/Viewport/Measure{measure}/BBox[{bbox}]>>"


def _evaluate(page, doc, shows=("urban_parcels", "land_use", "plan_boundary"), **kw):  # noqa: ANN001, ANN003, ANN202
    drawings = page.get_cdrawings()
    report = inspect_page(doc, page, CONFIG, drawings=drawings)
    spec = A.DocumentSpec(id="t", name="t", kind="plan", status="adopted", files=[])
    fspec = A.FileSpec(path="t.pdf", shows=list(shows))
    return report, A.evaluate_sheet(spec, fspec, page, report, drawings, PROFILE, **kw)


# --- text helpers ----------------------------------------------------------------------------


def test_glyph_id_shift_is_decoded():
    assert decode_glyph_ids("&RS" + chr(92) + "ULJKW") == "Copyright"
    assert decode_glyph_ids("85%$1,67,ÿ.(") == "URBANISTIČKE"
    assert decode_glyph_ids("SRYUåLQH") == "površine"


@pytest.mark.parametrize(
    ("word", "shifted"),
    [
        ("85%$1,67,ÿ.(", True),  # URBANISTIČKE
        ("SRYUåLQH", True),  # površine
        ("3295ä,1(", True),  # POVRŠINE
        ("ZONE", False),
        ("HOTEL", False),
        ("P+1", False),
        ("A99", False),
        ("1:1000", False),
        ("parking", False),
    ],
)
def test_shifted_word_detection(word: str, shifted: bool):
    assert looks_glyph_shifted(word) is shifted


def test_fold_and_layer_key():
    assert fold("POVRŠINA Đurđa") == "povrsina djurdja"
    assert layer_key("X-GEO_SV$0$PARCELE_GRANICE") == "PARCELE_GRANICE"
    assert layer_key("granica") == "granica"


def test_paper_names():
    assert paper_name(1189, 841) == "A0"
    assert paper_name(420, 297) == "A3"
    assert paper_name(1510, 1005) == "custom 1510x1005"


# --- evidence ------------------------------------------------------------------------------


def test_grid_labels_give_the_crs_and_the_scale():
    # labels every 100 m, placed 100 mm apart: 1:1000
    words = [
        (x, 10.0, x + 40, 20.0, f"{6603000 + i * 100}")
        for i, x in enumerate((100 * MM, 200 * MM, 300 * MM))
    ]
    words += [
        (5.0, y, 45.0, y + 10, f"{4700200 - i * 100}.00")
        for i, y in enumerate((100 * MM, 200 * MM, 300 * MM))
    ]
    grid = find_grid(words, CONFIG.crs_candidates)
    assert len(grid) == 1
    assert grid[0].epsg == 3908
    assert grid[0].interval_m == 100
    assert grid[0].fit["east_scale"] == pytest.approx(1000, rel=0.01)
    assert grid[0].fit["north_up"] is True


def test_viewport_measure_is_read():
    doc = pymupdf.open()
    page = doc.new_page(width=A1[0], height=A1[1])
    doc.xref_set_key(
        page.xref,
        "VP",
        f"[{_viewport(0.35278, '0 0 2384 1684')}{_viewport(0.27377, '20 20 1800 1600')}]",
    )
    vps = parse_viewports(doc, page)
    assert [v.paper_space for v in vps] == [True, False]
    assert vps[1].scale_denominator == pytest.approx(776, abs=1)


def test_grid_crosses_prove_the_stated_scale_over_a_wrong_viewport():
    doc = pymupdf.open()
    page = _plan_page(doc)
    doc.xref_set_key(page.xref, "VP", f"[{_viewport(0.27377, '20 20 2300 1600')}]")
    report, sheet = _evaluate(page, doc)
    assert report.stated_scales == [1000]
    g = sheet.georef
    assert g.scale == 1000
    assert g.grid_lattice["lattice_m"] == 100
    assert "viewport" in g.scale_warning
    assert g.method == "grid_lattice"


def test_lattice_and_round_steps():
    paths = []
    for gx in range(4):
        for gy in range(3):
            cx, cy = gx * 200 * MM, gy * 200 * MM
            paths.append({"rect": (cx - 4 * MM, cy - 0.1, cx + 4 * MM, cy + 0.1), "items": []})
            paths.append({"rect": (cx - 0.1, cy - 4 * MM, cx + 0.1, cy + 4 * MM), "items": []})
    lat = lattice_spacing(paths, 500)
    assert lat["crosses"] == 12
    assert lat["spacing_mm"] == pytest.approx(200, abs=0.2)
    assert lat["lattice_m"] == 100
    assert round_lattice(100, 776) is None
    assert round_lattice(100, 1000) == 100


# --- classification ----------------------------------------------------------------------------


def test_layered_vector_sheet_is_class_a():
    doc = pymupdf.open()
    page = _plan_page(doc)
    report, sheet = _evaluate(page, doc)
    assert report.role == "plan_sheet"
    assert report.title == "plan parcelacije"
    assert report.legend is True
    assert sheet.sheet_class == "A"
    parcels = next(r for r in sheet.layer_types if r.layer_type == "urban_parcels")
    assert parcels.status == "A"
    assert parcels.matched_by == "profile"
    assert parcels.labels["form"] == "text"
    assert parcels.expected == 9
    assert parcels.sample["faces"] == 10  # the 9 parcels and the band inside the plan boundary
    assert parcels.sample["median_face_m2"] == pytest.approx(3600, rel=0.05)
    land_use = next(r for r in sheet.layer_types if r.layer_type == "land_use")
    assert land_use.status == "A"


def test_flattened_vector_sheet_is_class_b():
    doc = pymupdf.open()
    page = _plan_page(doc, layered=False)
    report, sheet = _evaluate(page, doc)
    assert report.layered_path_share == 0
    assert sheet.sheet_class == "B"
    parcels = next(r for r in sheet.layer_types if r.layer_type == "urban_parcels")
    assert parcels.status == "B"
    assert parcels.steps == ["style_separation"]


def test_scanned_sheet_is_class_c():
    doc = pymupdf.open()
    page = _raster_page(doc)
    report, sheet = _evaluate(page, doc)
    assert report.image_cover_pct >= 99  # the scan keeps its aspect ratio
    assert report.image_dpi == pytest.approx(1200 / (841 / 25.4), rel=0.01)
    assert sheet.sheet_class == "C"
    assert {r.status for r in sheet.layer_types} == {"C"}


def test_table_page_is_not_a_plan_sheet():
    doc = pymupdf.open()
    page = _table_page(doc)
    report, sheet = _evaluate(page, doc)
    assert report.role == "table"
    assert sheet.sheet_class == "T"
    assert sheet.georef is None


def test_parcel_layer_holding_part_of_the_parcels_is_class_b():
    doc = pymupdf.open()
    page = _plan_page(doc, labels_as_text=False)
    _, sheet = _evaluate(page, doc, parcel_count=20)
    parcels = next(r for r in sheet.layer_types if r.layer_type == "urban_parcels")
    assert parcels.expected == 20
    assert parcels.expected_basis == "parcels in the parameter table"
    assert parcels.status == "B"
    assert "10 faces for 20 expected" in parcels.note


def test_dotted_boundaries_close():
    # a 100 x 60 mm rectangle drawn as 0.6 mm dots every 2 mm (a plotted dotted linetype)
    dots = []
    step = 2.0 * MM
    corners = [(0, 0), (100 * MM, 0), (100 * MM, 60 * MM), (0, 60 * MM), (0, 0)]
    for (ax, ay), (bx, by) in zip(corners, corners[1:], strict=False):
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        for i in range(round(length / step)):
            x = ax + (bx - ax) * i * step / length
            y = ay + (by - ay) * i * step / length
            r = 0.3 * MM
            dots.append(
                {
                    "rect": (x - r, y - r, x + r, y + r),
                    "fill": (1, 0, 0),
                    "items": [("re", (x - r, y - r, x + r, y + r))],
                }
            )
    assert sample_faces(dots, 1000).faces == 0  # the default tolerance leaves gaps
    assert sample_faces(dots, 1000, tolerance_mm=1.0).faces == 1


# --- decisions --------------------------------------------------------------------------------


def _doc_result(
    statuses: dict[str, str], steps: dict[str, list[str]] | None = None, method="grid_lattice"
):  # noqa: ANN001, ANN202
    spec = A.DocumentSpec(id="d", name="d", kind="plan", status="adopted", files=[])
    georef = A.Georef(
        stated_scale=1000, viewport_scale=None, scale=1000, scale_basis="stated", method=method
    )
    sheet = A.SheetResult(
        document_id="d",
        file="d.pdf",
        page=1,
        role="plan_sheet",
        title=None,
        paper="A0",
        rotation=0,
        raster=False,
        image_cover_pct=0,
        image_dpi=None,
        paths=1000,
        layers_used=20,
        layered_share=0.9,
        shifted_words=0,
        words=100,
        sheet_class="A",
        layer_types=[],
        georef=georef,
    )
    result = A.DocumentResult(spec=spec, sheets=[sheet], reports=[])
    result.type_status = statuses
    result.type_steps = steps or {t: ["polygonize"] for t in statuses}
    result.georef_method = method
    return result


def test_all_layers_automatic_is_go():
    r = _doc_result({t: "A" for t in A.TARGET_TYPES})
    A.decide(r)
    assert r.decision == "GO"
    assert r.native_files == "not needed"


def test_semi_automatic_parcels_go_with_conditions_and_native_files():
    statuses = {t: "A" for t in A.TARGET_TYPES} | {"urban_parcels": "B"}
    steps = {t: ["polygonize"] for t in A.TARGET_TYPES} | {
        "urban_parcels": ["polygonize", "ocr_labels", "merge_cadastral_faces"]
    }
    r = _doc_result(statuses, steps)
    A.decide(r)
    assert r.decision == "GO WITH CONDITIONS"
    assert r.native_files == "recommended"
    assert any("Urban parcels is semi-automatic" in c for c in r.conditions)


def test_scanned_or_unreferenced_documents_are_no_go():
    r = _doc_result({t: "A" for t in A.TARGET_TYPES} | {"land_use": "C"})
    A.decide(r)
    assert (r.decision, r.native_files) == ("NO-GO", "required")
    r = _doc_result({t: "A" for t in A.TARGET_TYPES}, method="manual")
    A.decide(r)
    assert r.decision == "NO-GO"


def test_missing_target_layer_is_a_condition():
    r = _doc_result({"urban_parcels": "A", "urban_blocks": "A", "traffic_network": "A"})
    A.decide(r)
    assert r.decision == "GO WITH CONDITIONS"
    assert r.conditions == ["Planned land use: not on the supplied sheets"]


def test_deferred_traffic_layer_neither_blocks_nor_counts():
    # traffic is an MVP layer: a semi-automatic or missing road network changes nothing
    r = _doc_result({"urban_parcels": "A", "urban_blocks": "A", "land_use": "A"})
    A.decide(r)
    assert r.decision == "GO"
    r = _doc_result({t: "A" for t in A.TARGET_TYPES} | {"traffic_network": "B"})
    A.decide(r)
    assert (r.decision, r.native_files) == ("GO", "not needed")


# --- profile and GIS files -------------------------------------------------------------------


def test_podgorica_gis_profile_patterns_compile():
    gis = load_gis_profile("podgorica")
    assert gis is not None
    # tooling configuration only: the profile the API serves does not carry it
    assert "gis" not in load_profile("podgorica").model_dump()
    for table in (gis.plan_layers, gis.base_map_layers, gis.sheet_titles):
        for patterns in table.values():
            for p in patterns:
                re.compile(p)
    assert [c.epsg for c in gis.crs_candidates] == [3908, 25834]
    assert re.search(gis.plan_layers["urban_parcels"][0], fold("UP_Urbanisticka parcela_SS"))


@pytest.fixture
def ogrinfo() -> str:
    exe = find_ogrinfo()
    if exe is None:
        pytest.skip("GDAL ogrinfo not available")
    return exe


def test_gis_file_layers_crs_and_counts(tmp_path: Path, ogrinfo: str):
    src = tmp_path / "plan.geojson"
    src.write_text(
        '{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::3908"}},'
        '"features":[{"type":"Feature","properties":{"Layer":"UP_GRANICE"},"geometry":{"type":"Polygon",'
        '"coordinates":[[[6603500,4700160],[6603520,4700160],[6603520,4700180],[6603500,4700160]]]}},'
        '{"type":"Feature","properties":{"Layer":"BLOKOVI"},"geometry":{"type":"LineString",'
        '"coordinates":[[6603490,4700150],[6603560,4700150]]}}]}',
        encoding="utf-8",
    )
    report = inspect_gis(src, ogrinfo)
    assert report.error is None
    assert [(lyr.features, lyr.epsg) for lyr in report.layers] == [(2, 3908)]
    ogr2ogr = str(Path(ogrinfo).with_name(Path(ogrinfo).name.replace("ogrinfo", "ogr2ogr")))
    if not Path(ogr2ogr).is_file() and not shutil.which("ogr2ogr"):
        return
    dxf = tmp_path / "plan.dxf"
    subprocess.run(
        [ogr2ogr, "-f", "DXF", str(dxf), str(src)],
        env=gdal_env(ogrinfo),
        check=True,
        capture_output=True,
    )
    cad = inspect_gis(dxf, ogrinfo)
    assert cad.driver == "DXF"
    assert cad.layers[0].cad_layers == {"UP_GRANICE": 1, "BLOKOVI": 1}


def test_unreadable_gis_file_reports_instead_of_failing(tmp_path: Path, ogrinfo: str):
    bad = tmp_path / "plan.dwg"
    bad.write_bytes(b"AC1032 not really a drawing")
    report = inspect_gis(bad, ogrinfo)
    assert report.layers == []
    assert "DXF export" in report.error


# --- end to end ------------------------------------------------------------------------------


def test_cli_writes_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.gis import assess

    doc = pymupdf.open()
    _plan_page(doc)
    doc.save(tmp_path / "plan.pdf")
    scan = pymupdf.open()
    _raster_page(scan)
    scan.save(tmp_path / "scan.pdf")
    (tmp_path / "catalog.toml").write_text(
        """
[[document]]
id = "vector-plan"
name = "DUP Vector"
kind = "plan"
status = "adopted"
  [[document.files]]
  path = "plan.pdf"
  shows = ["urban_parcels", "land_use", "plan_boundary"]

[[document]]
id = "scanned-plan"
name = "DUP Scanned"
kind = "plan"
status = "adopted"
  [[document.files]]
  path = "scan.pdf"
  shows = ["urban_parcels", "land_use"]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(assess, "load_gis_profile", lambda _id: PROFILE)
    out = tmp_path / "out"
    cache = tmp_path / "cache" / "assessment.pickle"
    catalog = str(tmp_path / "catalog.toml")
    assert assess.main([catalog, "--out", str(out), "--cache", str(cache)]) == 0
    for name in (
        "report.md",
        "documents.csv",
        "pages.csv",
        "layer_types.csv",
        "layers.csv",
        "georeferencing.csv",
        "assessment.json",
    ):
        assert (out / name).is_file(), name
    documents = (out / "documents.csv").read_text(encoding="utf-8-sig")
    assert "vector-plan" in documents and "scanned-plan" in documents
    assert "NO-GO" in documents  # the scan
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## 5. Georeferencing evidence" in report
    assert (out / "previews" / "plan-p1-urban_parcels.png").is_file()

    # the report rebuilds from the cache without reading the sheets
    (out / "report.md").unlink()
    (tmp_path / "plan.pdf").unlink()
    assert assess.main([catalog, "--out", str(out), "--cache", str(cache), "--report-only"]) == 0
    assert "## 5. Georeferencing evidence" in (out / "report.md").read_text(encoding="utf-8")


def test_layer_form_follows_drawn_length_not_path_count():
    from core.gis.inspect_pdf import LayerStats, layer_form

    # 72 long parcel boundaries and 1 600 vertex circles / numbers on the same CAD layer
    stats = LayerStats(name="R-PARCELE", paths=1692, dashes=1620, closed=72)
    stats.length_mm = {"dashes": 9000.0, "rings": 21000.0}
    assert layer_form(stats) == "rings"
    stats.length_mm = {"dashes": 21000.0, "rings": 9000.0}
    assert layer_form(stats) == "dashes"


def test_filled_pieces_close_boundaries_but_fill_areas():
    from core.gis.inspect_pdf import LayerStats

    pieces = [LayerStats(name="UP-BLOKOVI", paths=400, form="tessellation")]
    pieces[0].length_mm = {"tessellation": 5000.0}
    assert A.steps_for("urban_blocks", pieces) == ["close_dotted_lines"]
    assert A.steps_for("land_use", pieces) == ["union_fills"]


def _sheet(file: str, results: list, shows: list[str], sheet_set: str | None = None):  # noqa: ANN202
    georef = A.Georef(stated_scale=500, viewport_scale=500, scale=500, scale_basis="stated")
    georef.method = "grid_lattice"
    return A.SheetResult(
        document_id="d",
        file=file,
        page=1,
        role="plan_sheet",
        title=None,
        paper="A0",
        rotation=0,
        raster=False,
        image_cover_pct=0,
        image_dpi=None,
        paths=5000,
        layers_used=40,
        layered_share=0.95,
        shifted_words=0,
        words=100,
        sheet_class="A",
        layer_types=results,
        georef=georef,
        shows=shows,
        sheet_set=sheet_set or file,
    )


def test_checked_sample_outranks_unchecked_and_blocks_follow_parcels():
    def blocks(faces: int, expected: int | None) -> A.LayerTypeResult:
        r = A.LayerTypeResult(layer_type="urban_blocks", status="A" if expected is None else "B")
        r.layers, r.steps = ["UP-BLOKOVI"], ["close_dotted_lines"]
        r.sample, r.expected = {"faces": faces, "method": "holes"}, expected
        return r

    parcels = A.LayerTypeResult(layer_type="urban_parcels", status="A", layers=["UP"])
    parcels.steps = ["ribbon_holes"]
    spec = A.DocumentSpec(id="d", name="d", kind="plan", status="adopted", files=[])
    result = A.DocumentResult(
        spec=spec,
        sheets=[
            _sheet("10a.pdf", [blocks(1, None), parcels], ["urban_blocks", "urban_parcels"]),
            _sheet("09.pdf", [blocks(2, 6)], ["urban_blocks"]),
        ],
        reports=[],
    )
    A.aggregate(result)
    assert result.type_status["urban_blocks"] == "B"  # the checked sheet: 2 of 6 zones
    spec.blocks_from_parcels = "every parcel id starts with its zone letter"
    A.aggregate(result)
    assert result.type_status["urban_blocks"] == "A"
    assert result.type_steps["urban_blocks"] == ["dissolve_parcels"]


def test_boundary_layer_closing_nothing_is_not_automatic():
    r = A.LayerTypeResult(layer_type="city_boundary", status="A", layers=["Granica GUPa"])
    r.sample = {"faces": 0, "method": "holes", "skipped": None}
    assert A.effective_status(r) == "B"
    r.sample = {"faces": 0, "method": "holes", "skipped": "7924947 segments"}
    assert A.effective_status(r) == "A"  # not sampled: reported as unverified instead
