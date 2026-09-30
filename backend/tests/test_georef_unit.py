"""Georeferencing (``core.gis.georef``) on a synthetic sheet with a known ground truth.

The truth maps the document's local frame (1:1000, 1 pt = 0.3528 m) to ETRS89-like UTM 34N
coordinates (EPSG:32634, WGS 84 datum: no datum shift, so an analytic UTM inverse is an
independent check of GDAL's output): a rotation of 0.35° and a shift to Podgorica. Control points
read off the truth must give an RMSE of zero and the truth's parameters; a bad point is found and
the fit rejected; grid crosses drawn at the truth's 100 m nodes become exact control points from
one rough seed; the stored transform applied with GDAL puts every vertex where the UTM inverse
says, and re-applying it gives the same digest.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("shapely")

import numpy as np  # noqa: E402
import pymupdf  # noqa: E402
import yaml  # noqa: E402
from shapely.geometry import MultiPolygon, Polygon  # noqa: E402

from core.cadastre.ogr import find_ogr2ogr  # noqa: E402
from core.gis.extract.gpkg import read_gpkg, write_gpkg  # noqa: E402
from core.gis.extract.rules import DocumentRules, Selector  # noqa: E402
from core.gis.extract.sheet import load_sheet  # noqa: E402
from core.gis.georef import __main__ as cli  # noqa: E402
from core.gis.georef.apply import digest, georeference  # noqa: E402
from core.gis.georef.fit import (  # noqa: E402
    FitError,
    fit,
    read_transform,
    to_local,
    write_transform,
)
from core.gis.georef.grid import find_crosses, suggest_grid_points  # noqa: E402
from core.gis.georef.points import (  # noqa: E402
    ControlPoint,
    add_points,
    points_hash,
    read_points,
    set_enabled,
    write_points,
)
from tests import gis_synthetic as syn  # noqa: E402

needs_gdal = pytest.mark.skipif(find_ogr2ogr() is None, reason="ogr2ogr not available")

CRS = "EPSG:32634"
THETA = math.radians(0.35)
E0, N0 = 355_000.0, 4_700_000.0
SHEET_POINTS = [(60, 60), (540, 60), (540, 360), (60, 360), (300, 210), (200, 300)]


def truth(x: float, y: float) -> tuple[float, float]:
    """Local metres -> UTM 34N (the ground truth)."""
    c, s = math.cos(THETA), math.sin(THETA)
    return (E0 + c * x - s * y, N0 + s * x + c * y)


def truth_inverse(e: float, n: float) -> tuple[float, float]:
    c, s = math.cos(THETA), math.sin(THETA)
    de, dn = e - E0, n - N0
    return (c * de + s * dn, -s * de + c * dn)


def utm_inverse(e: float, n: float, zone: int = 34) -> tuple[float, float]:
    """UTM (WGS 84, north) -> lon / lat: Krüger's series, sub-millimetre inside a zone."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    m = f / (2 - f)
    big_a = a / (1 + m) * (1 + m**2 / 4 + m**4 / 64)
    beta = (m / 2 - 2 * m**2 / 3 + 37 * m**3 / 96, m**2 / 48 + m**3 / 15, 17 * m**3 / 480)
    delta = (2 * m - 2 * m**2 / 3 - 2 * m**3, 7 * m**2 / 3 - 8 * m**3 / 5, 56 * m**3 / 15)
    xi = n / (k0 * big_a)
    eta = (e - 500_000.0) / (k0 * big_a)
    xi_p = xi - sum(
        b * math.sin(2 * j * xi) * math.cosh(2 * j * eta) for j, b in enumerate(beta, 1)
    )
    eta_p = eta - sum(
        b * math.cos(2 * j * xi) * math.sinh(2 * j * eta) for j, b in enumerate(beta, 1)
    )
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    lat = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(delta, 1))
    lon = math.radians(zone * 6 - 183) + math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(lon), math.degrees(lat)


def rules(**georef: object) -> DocumentRules:
    data = yaml.safe_load(syn.RULES_YAML)
    data["georef"] = {"crs": CRS, **georef}
    return DocumentRules.model_validate(data)


def control_points(positions=SHEET_POINTS, sheet: str = "a") -> list[ControlPoint]:
    points = []
    for i, (x, y) in enumerate(positions, start=1):
        e, n = truth(x * syn.K, y * syn.K)
        points.append(ControlPoint(f"cp{i}", sheet, float(x), float(y), e, n, "manual"))
    return points


# --- control points ---------------------------------------------------------------------------


def test_points_csv_round_trip_replace_disable_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "doc.points.csv"
    points = control_points()
    write_points(path, points)
    assert read_points(path) == points
    assert path.read_bytes().count(b"\r") == 0
    moved = ControlPoint("cp2", "a", 540.0, 60.0, 1.0, 2.0, "label", "retyped")
    replaced = add_points(points, [moved, ControlPoint("cp7", "a", 1.0, 1.0, 3.0, 4.0)])
    assert [p.id for p in replaced] == ["cp1", "cp2", "cp3", "cp4", "cp5", "cp6", "cp7"]
    assert replaced[1] == moved
    base = points_hash(points)
    off = set_enabled(points + [replace(control_points([(10, 10)])[0], id="x")], {"x"}, False)
    assert points_hash(off) == base  # a disabled point does not change the fingerprint
    with pytest.raises(ValueError, match="no control point"):
        set_enabled(points, {"nope"}, False)
    path.write_text("id,sheet,x_pt,y_pt,easting,northing,source\nq,a,1,2,3,4,guess\n")
    with pytest.raises(ValueError, match="source 'guess'"):
        read_points(path)
    path.write_text("id,sheet,x_pt,y_pt,easting,northing\nq,a,1,2,3,4\nq,a,1,2,3,4\n")
    with pytest.raises(ValueError, match="used twice"):
        read_points(path)


# --- the fit ----------------------------------------------------------------------------------


def test_helmert_fit_recovers_the_known_transform_with_zero_rmse() -> None:
    doc = rules()
    result = fit(control_points(), doc, crs=CRS)
    t = result.transform
    assert result.ok and result.points_used == 6
    assert result.rmse_m < 1e-6 and result.max_residual_m < 1e-6
    assert t.a == pytest.approx(math.cos(THETA), abs=1e-9)
    assert t.d == pytest.approx(math.sin(THETA), abs=1e-9)
    assert t.scale == pytest.approx(1.0, abs=1e-9)
    assert t.rotation_deg == pytest.approx(0.35, abs=1e-7)
    assert result.warnings == []
    assert not any(r.outlier for r in result.residuals)
    (sheet,) = result.sheets
    assert sheet["sheet"] == "a" and sheet["points"] == 6 and sheet["rmse_m"] < 1e-6
    # any position on the sheet lands on the truth, through the document frame and per sheet
    for x_pt, y_pt in ((123.4, 234.5), (10.0, 400.0), (599.0, 1.0)):
        expected = truth(x_pt * syn.K, y_pt * syn.K)
        local = np.array([to_local(doc.sheets[0], x_pt, y_pt)])
        assert t.apply(local)[0] == pytest.approx(expected, abs=1e-6)
        a, b, c, d, e, f = sheet["page_to_crs"]
        assert (a * x_pt + b * y_pt + c, d * x_pt + e * y_pt + f) == pytest.approx(
            expected, abs=1e-6
        )


def test_a_bad_point_is_found_and_the_fit_rejected() -> None:
    doc = rules(max_rmse_m=0.5)
    points = control_points()
    bad = ControlPoint("cp3", "a", 540.0, 360.0, points[2].easting + 3.0, points[2].northing)
    points[2] = bad
    result = fit(points, doc, crs=CRS)
    assert not result.ok and result.rmse_m > 0.5
    assert [r.id for r in result.residuals if r.outlier] == ["cp3"]
    assert any("cp3" in w for w in result.warnings)
    assert any("above the document's threshold" in w for w in result.warnings)
    fixed = fit(set_enabled(points, {"cp3"}, False), doc, crs=CRS)
    assert fixed.ok and fixed.rmse_m < 1e-6 and fixed.points_used == 5
    assert any("6 or more" in w for w in fixed.warnings)


def test_fit_refuses_unknown_sheets_and_too_few_points() -> None:
    doc = rules()
    with pytest.raises(FitError, match="unknown sheets"):
        fit(control_points(sheet="zz"), doc, crs=CRS)
    with pytest.raises(FitError, match="needs 2"):
        fit(control_points([(60, 60)]), doc, crs=CRS)
    with pytest.raises(FitError, match="one spot"):
        fit(control_points([(60, 60), (60, 60.001)]), doc, crs=CRS)
    few = fit(control_points(SHEET_POINTS[:3]), doc, crs=CRS)
    assert not few.ok and few.rmse_m < 1e-6  # exact, but under min_points (4)
    assert any("at least 4" in w for w in few.warnings)


def test_affine_fits_a_stretched_sheet_and_helmert_warns_on_a_wrong_scale() -> None:
    doc = rules(method="affine")

    def stretched(x: float, y: float) -> tuple[float, float]:
        e, n = truth(x * 1.002, y * 0.998 + x * 0.001)  # paper stretch and a little shear
        return e, n

    points = [
        ControlPoint(f"s{i}", "a", float(x), float(y), *stretched(x * syn.K, y * syn.K))
        for i, (x, y) in enumerate(SHEET_POINTS, start=1)
    ]
    affine = fit(points, doc, crs=CRS)
    assert affine.ok and affine.rmse_m < 1e-6 and affine.transform.method == "affine"
    helmert = fit(points, doc, crs=CRS, method="helmert")
    assert helmert.rmse_m > 0.05
    # a sheet whose scale in the rules is wrong (1:776 read off the viewport, truly 1:1000)
    wrong = [
        ControlPoint(
            p.id,
            p.sheet,
            p.x_pt,
            p.y_pt,
            *truth(p.x_pt * syn.K * 1000 / 776, p.y_pt * syn.K * 1000 / 776),
        )
        for p in control_points()
    ]
    scaled = fit(wrong, rules(), crs=CRS)
    assert scaled.rmse_m < 1e-6
    assert any("sheet's scale" in w for w in scaled.warnings)


# --- grid crosses -----------------------------------------------------------------------------

GRID_W, GRID_H = 1600.0, 1200.0


def turned(theta_deg: float):
    """A truth like ``truth`` / ``truth_inverse`` with another rotation: 90.35° draws the plan with
    its north pointing right on the page, 180.35° down, -89.65° left."""
    c, s = math.cos(math.radians(theta_deg)), math.sin(math.radians(theta_deg))

    def world(x: float, y: float) -> tuple[float, float]:
        return (E0 + c * x - s * y, N0 + s * x + c * y)

    def local(e: float, n: float) -> tuple[float, float]:
        de, dn = e - E0, n - N0
        return (c * de + s * dn, -s * de + c * dn)

    return world, local


def grid_sheet(
    world=truth, local=truth_inverse
) -> tuple[bytes, list[tuple[float, float, float, float]]]:
    """A sheet with the state grid's crosses (layer MREZA, 3 mm arms) at the truth's 100 m nodes,
    and a few other small paths on another layer. Returns the PDF and every cross as (x_pt, y_pt
    in the sheet frame, easting, northing)."""
    doc = pymupdf.open()
    page = doc.new_page(width=GRID_W, height=GRID_H)
    grid = doc.add_ocg("MREZA", on=True)
    other = doc.add_ocg("PARCELE", on=True)
    arm = 3 / 25.4 * 72 / 2  # half of a 3 mm arm, in points
    corners = [world(x * syn.K, y * syn.K) for x in (0, GRID_W) for y in (0, GRID_H)]
    es = [e for e, _ in corners]
    ns = [n for _, n in corners]
    crosses = []
    for e in np.arange(math.ceil(min(es) / 100) * 100, max(es), 100.0):
        for n in np.arange(math.ceil(min(ns) / 100) * 100, max(ns), 100.0):
            lx, ly = local(float(e), float(n))
            x, y = lx / syn.K, ly / syn.K
            if not (20 < x < GRID_W - 20 and 20 < y < GRID_H - 20):
                continue
            crosses.append((x, y, float(e), float(n)))
            py = GRID_H - y  # pymupdf page coordinates: origin top-left
            shape = page.new_shape()
            shape.draw_line(pymupdf.Point(x - arm, py), pymupdf.Point(x + arm, py))
            shape.draw_line(pymupdf.Point(x, py - arm), pymupdf.Point(x, py + arm))
            shape.finish(color=(0, 0, 0), width=0.3, oc=grid)
            shape.commit()
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(400, 400, 404, 404))
    shape.finish(color=(0, 0, 0), width=0.3, oc=other)
    shape.commit()
    return doc.tobytes(), crosses


def test_grid_crosses_become_exact_control_points_from_one_rough_seed() -> None:
    pdf, crosses = grid_sheet()
    doc = rules()
    rule = doc.sheets[0].model_copy(update={"file": "grid.pdf"})
    sheet = load_sheet(pdf, rule, keep=[Selector(layer_regex="^mreza$")])
    found = find_crosses(sheet, "^mreza$")
    assert len(found) == len(crosses) >= 12
    for c in found:
        assert min(math.dist((c.x_pt, c.y_pt), (x, y)) for x, y, _, _ in crosses) < 0.01
    # the seed: a spot on the sheet whose coordinate is known only to within 25 m
    sx, sy = 500.0, 420.0
    e, n = truth(sx * syn.K, sy * syn.K)
    suggestion = suggest_grid_points(sheet, rule, seed=(sx, sy, e + 23.0, n - 17.0))
    assert suggestion.warnings == []
    assert suggestion.angle_deg == pytest.approx(-0.35, abs=0.02)
    assert len(suggestion.points) == len(crosses)
    for p in suggestion.points:
        x, y, ce, cn = min(crosses, key=lambda c: math.dist((p.x_pt, p.y_pt), c[:2]))
        assert (p.easting, p.northing) == (ce, cn)  # exact grid values
        assert p.source == "grid" and p.sheet == "a"
    result = fit(suggestion.points, doc, crs=CRS)
    assert result.ok and result.rmse_m < 0.005  # cross centres read to a few mm
    assert result.transform.rotation_deg == pytest.approx(0.35, abs=0.001)
    # a seed more than half an interval off puts every cross on the next node, and the fit
    # cannot tell: the cadastral overlap check of the staging has to
    wrong = suggest_grid_points(sheet, rule, seed=(sx, sy, e + 60.0, n))
    for p in wrong.points:
        x, y, ce, cn = min(crosses, key=lambda c: math.dist((p.x_pt, p.y_pt), c[:2]))
        assert (p.easting, p.northing) == (ce + 100.0, cn)
    assert fit(wrong.points, doc, crs=CRS).rmse_m < 0.005


@pytest.mark.parametrize(("north", "quarter"), [("right", 90.0), ("down", 180.0), ("left", -90.0)])
def test_a_sheet_drawn_north_right_down_or_left_needs_its_rule(north, quarter) -> None:
    world, local = turned(quarter + 0.35)
    pdf, crosses = grid_sheet(world, local)
    doc = rules()
    rule = doc.sheets[0].model_copy(update={"file": "grid.pdf", "north": north})
    sheet = load_sheet(pdf, rule, keep=[Selector(layer_regex="^mreza$")])
    sx, sy = 500.0, 420.0
    e, n = world(sx * syn.K, sy * syn.K)
    seed = (sx, sy, e + 23.0, n - 17.0)
    suggestion = suggest_grid_points(sheet, rule, seed=seed)
    assert suggestion.warnings == []
    assert len(suggestion.points) == len(crosses) >= 12
    for p in suggestion.points:
        _, _, ce, cn = min(crosses, key=lambda c: math.dist((p.x_pt, p.y_pt), c[:2]))
        assert (p.easting, p.northing) == (ce, cn)
    result = fit(suggestion.points, doc, crs=CRS)
    assert result.ok and result.rmse_m < 0.005
    turn = (result.transform.rotation_deg - (quarter + 0.35) + 180) % 360 - 180
    assert turn == pytest.approx(0, abs=0.001)
    # read as if north were up, the same crosses get a turned or mirrored lattice that still fits
    # perfectly: nothing but the rule can tell
    as_up = suggest_grid_points(sheet, rule.model_copy(update={"north": "up"}), seed=seed)
    nearest = [min(crosses, key=lambda c: math.dist((p.x_pt, p.y_pt), c[:2])) for p in as_up.points]
    misplaced = [
        p for p, c in zip(as_up.points, nearest, strict=True) if (p.easting, p.northing) != c[2:]
    ]
    assert len(as_up.points) == len(crosses) and len(misplaced) >= len(crosses) - 1
    assert fit(as_up.points, doc, crs=CRS).rmse_m < 0.005


# --- apply with GDAL --------------------------------------------------------------------------


def world_features() -> dict[str, list[tuple[object, dict]]]:
    """The plan in UTM 34N: boundary, one parcel, its block and land use."""
    e, n = E0 + 120.0, N0 + 80.0

    def poly(*pts: tuple[float, float]) -> MultiPolygon:
        return MultiPolygon([Polygon([(e + x, n + y) for x, y in pts])])

    parcel = poly((0, 0), (40, 0), (40, 30), (0, 30))
    base = {"document_id": None, "document_ref": "synthetic", "sheet": "a", "page": 1}
    return {
        "plan_boundary": [
            (poly((-20, -20), (80, -20), (80, 60), (-20, 60)), {**base, "feature_key": "coverage"})
        ],
        "urban_parcels": [
            (
                parcel,
                {
                    **base,
                    "feature_key": "1",
                    "urban_parcel_number": "1",
                    "block_ref": "A",
                    "source_bbox": [1.0, 2.0, 3.0, 4.0],
                    "qa_flags": ["multi_label"],
                },
            )
        ],
        "urban_blocks": [
            (
                poly((-5, -5), (45, -5), (45, 35), (-5, 35)),
                {**base, "feature_key": "A", "block_ref": "A"},
            )
        ],
        "planned_land_use": [
            (parcel, {**base, "feature_key": "UP 1", "code": "SS", "name": "Stanovanje"})
        ],
    }


def local_gpkg(path: Path) -> None:
    from shapely import transform as shapely_transform

    def inverse(coords: np.ndarray) -> np.ndarray:
        return np.array([truth_inverse(x, y) for x, y in coords])

    local = {
        layer: [(shapely_transform(g, inverse), props) for g, props in rows]
        for layer, rows in world_features().items()
    }
    write_gpkg(path, local, description="synthetic, local frame")


@needs_gdal
def test_the_stored_transform_puts_every_vertex_where_utm_says(tmp_path: Path) -> None:
    doc = rules()
    source = tmp_path / "synthetic.gpkg"
    local_gpkg(source)
    result = fit(control_points(), doc, crs=CRS)
    stored = tmp_path / "synthetic.transform.json"
    data = write_transform(stored, result, "synthetic")
    assert data["points_sha256"] == points_hash(control_points())
    transform, _ = read_transform(stored)
    out = georeference(
        source,
        doc,
        transform,
        tmp_path / "out.gpkg",
        tmp_path / "work",
        attributes={"document_id": 7},
    )
    world = world_features()
    assert {k: len(v) for k, v in out.items()} == {k: len(v) for k, v in world.items()}
    for layer, rows in out.items():
        (geom, props), (expected_geom, expected_props) = rows[0], world[layer][0]
        assert props["document_id"] == 7 and props["feature_key"] == expected_props["feature_key"]
        got = np.array(_coords(geom))
        want = np.array([utm_inverse(x, y) for x, y in _coords(expected_geom)])
        assert got.shape == want.shape
        assert np.abs(got - want).max() < 1e-8  # ~1 mm
    parcel = out["urban_parcels"][0][1]
    assert parcel["qa_flags"] == ["multi_label"] and parcel["source_bbox"] == [1.0, 2.0, 3.0, 4.0]
    assert parcel["urban_parcel_number"] == "1" and parcel["block_ref"] == "A"
    # re-applying the stored transform gives the same features; the label is not in the digest
    again = georeference(
        source,
        doc,
        read_transform(stored)[0],
        tmp_path / "again.gpkg",
        tmp_path / "work2",
        attributes={"document_id": 7, "dataset_version": "geo-7-x"},
    )
    assert digest(again) == digest(out)


@needs_gdal
def test_a_sheet_redrawn_in_pdf_points_takes_the_same_path(tmp_path: Path) -> None:
    doc = rules(method="affine")
    from shapely import transform as shapely_transform

    source = tmp_path / "local.gpkg"
    local_gpkg(source)
    redrawn = {
        layer: [(shapely_transform(g, lambda c: c / syn.K), props) for g, props in rows]
        for layer, rows in read_gpkg(source).items()
    }
    write_gpkg(tmp_path / "redrawn.gpkg", redrawn)
    transform = fit(control_points(), doc, crs=CRS).transform
    a = georeference(source, doc, transform, tmp_path / "a.gpkg", tmp_path / "wa")
    b = georeference(
        tmp_path / "redrawn.gpkg",
        doc,
        transform,
        tmp_path / "b.gpkg",
        tmp_path / "wb",
        frame="sheet:a",
    )
    for layer in a:
        ga, gb = np.array(_coords(a[layer][0][0])), np.array(_coords(b[layer][0][0]))
        assert np.abs(ga - gb).max() < 1e-9
    with pytest.raises(ValueError, match="no sheet"):
        georeference(source, doc, transform, tmp_path / "c.gpkg", tmp_path / "wc", frame="sheet:z")


def _coords(geom) -> list[tuple[float, float]]:
    import shapely

    return [tuple(c) for c in shapely.get_coordinates(geom)]


# --- the CLI ----------------------------------------------------------------------------------


def test_cli_adds_points_fits_and_refuses_a_stale_transform(tmp_path: Path, capsys) -> None:
    data = yaml.safe_load(syn.RULES_YAML)
    data["georef"] = {"crs": CRS, "max_rmse_m": 0.5}
    rules_file = tmp_path / "synthetic.yaml"
    rules_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    for p in control_points():
        code = cli.main(
            [
                "add",
                str(rules_file),
                "--sheet",
                "a",
                "--x-pt",
                str(p.x_pt),
                "--y-pt",
                str(p.y_pt),
                "--easting",
                repr(p.easting),
                "--northing",
                repr(p.northing),
                "--id",
                p.id,
                "--source",
                "label",
            ]
        )
        assert code == 0
    assert (
        cli.main(
            [
                "add",
                str(rules_file),
                "--sheet",
                "zz",
                "--x-pt",
                "1",
                "--y-pt",
                "1",
                "--easting",
                "1",
                "--northing",
                "1",
            ]
        )
        == 2
    )
    assert cli.main(["fit", str(rules_file)]) == 0
    stored = tmp_path / "synthetic.transform.json"
    first = json.loads(stored.read_text(encoding="utf-8"))
    assert first["rmse_m"] < 1e-6 and first["points_used"] == 6 and first["crs"] == CRS
    assert cli.main(["points", str(rules_file)]) == 0
    assert "current" in capsys.readouterr().out
    # a mistyped point: the fit is rejected and the stored transform is left alone
    assert (
        cli.main(
            [
                "add",
                str(rules_file),
                "--sheet",
                "a",
                "--x-pt",
                "300",
                "--y-pt",
                "100",
                "--easting",
                "355200",
                "--northing",
                "4700100",
                "--id",
                "typo",
            ]
        )
        == 0
    )
    assert cli.main(["fit", str(rules_file)]) == 1
    assert "typo" in capsys.readouterr().out
    assert json.loads(stored.read_text(encoding="utf-8")) == first
    source = tmp_path / "local.gpkg"
    local_gpkg(source)
    out = tmp_path / "out"
    apply = ["apply", str(rules_file), "--gpkg", str(source), "--out", str(out)]
    assert cli.main(apply) == 2  # the points changed since the stored fit
    assert "changed since the stored fit" in capsys.readouterr().err
    assert cli.main(["disable", str(rules_file), "typo"]) == 0
    if find_ogr2ogr() is not None:
        assert cli.main(apply) == 0
        assert (out / "synthetic.georef.gpkg").is_file()
