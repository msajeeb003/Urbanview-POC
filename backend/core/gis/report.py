"""Write the geometry assessment: CSV tables, a JSON dump and a markdown report.

Everything here is derived from ``core.gis.assessment.Assessment``; nothing is decided here.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

from core.gis.assessment import (
    DEFERRED_TYPES,
    LAYER_TYPES,
    STAGED_AS,
    STEP_TEXT,
    TARGET_TYPES,
    Assessment,
    DocumentResult,
    SheetResult,
)
from core.gis.inspect_gis import GisReport
from core.gis.inspect_pdf import PdfReport


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


GIS_FIELDS = [
    "file",
    "driver",
    "layer",
    "geometry",
    "features",
    "epsg",
    "crs",
    "cad_layers",
    "error",
]


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    """A CSV with the union of the rows' keys; an empty table still gets its header."""
    fields = list(fields or [])
    for row in rows:
        for k in row:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _pdf_report(doc: DocumentResult, file: str) -> PdfReport | None:
    for r in doc.reports:
        if isinstance(r, PdfReport) and r.path.replace("\\", "/").endswith(file.replace("\\", "/")):
            return r
    return None


def _page_report(doc: DocumentResult, sheet: SheetResult):  # noqa: ANN202
    report = _pdf_report(doc, sheet.file)
    return None if report is None else report.page_reports[sheet.page - 1]


# --- CSV ---------------------------------------------------------------------------------------


def document_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for d in a.documents:
        split = {t: d.type_status.get(t, "") for t in d.type_status}
        rows.append(
            {
                "document_id": d.spec.id,
                "document": d.spec.name,
                "kind": d.spec.kind,
                "plan_type": d.spec.plan_type,
                "status": d.spec.status,
                "registry": d.spec.registry,
                "sheets": len([s for s in d.sheets if s.georef]),
                "sheet_classes": " ".join(sorted(s.sheet_class for s in d.sheets if s.georef)),
                **{f"class_{t}": split.get(t, "") for t in LAYER_TYPES},
                "parameter_table_parcels": len(d.parameter_ids) or "",
                "extraction_h": d.extraction_hours,
                "georeferencing_h": d.georef_hours,
                "georeferencing_method": d.georef_method,
                "decision": d.decision,
                "native_gis_cad_files": d.native_files,
                "conditions": " | ".join(d.conditions),
                "counted_in_totals": d.counted,
            }
        )
    return rows


def page_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for d in a.documents:
        for s in d.sheets:
            p = _page_report(d, s)
            g = s.georef
            rows.append(
                {
                    "document_id": d.spec.id,
                    "file": s.file,
                    "page": s.page,
                    "role": s.role,
                    "class": s.sheet_class,
                    "title": s.title,
                    "paper": s.paper,
                    "width_mm": p.width_mm if p else "",
                    "height_mm": p.height_mm if p else "",
                    "rotation": s.rotation,
                    "has_text_layer": bool(p and p.text_words >= 20),
                    "text_words": s.words,
                    "glyph_shifted_words": s.shifted_words,
                    "image_only": s.raster,
                    "images": p.images if p else "",
                    "largest_image_cover_pct": s.image_cover_pct,
                    "largest_image_dpi": s.image_dpi or "",
                    "vector_paths": s.paths,
                    "segments": p.segments if p else "",
                    "lines": (p.segments_by_kind.get("l", 0) if p else ""),
                    "curves": (p.segments_by_kind.get("c", 0) if p else ""),
                    "rects": (p.segments_by_kind.get("re", 0) if p else ""),
                    "filled_paths": p.filled_paths if p else "",
                    "closed_paths": p.closed_paths if p else "",
                    "pdf_layers_used": s.layers_used,
                    "paths_on_layers_pct": round(s.layered_share * 100, 1),
                    "style_groups": p.style_groups if p else "",
                    "legend": p.legend if p else "",
                    "stated_scale": (g.stated_scale if g else "") or "",
                    "viewport_scale": (g.viewport_scale if g else "") or "",
                    "scale_used": (g.scale if g else "") or "",
                    "scale_basis": g.scale_basis if g else "",
                    "scale_warning": (g.scale_warning if g else "") or "",
                    "grid_labels": _grid_text(g.grid_labels) if g else "",
                    "grid_crosses": _lattice_text(g.grid_lattice) if g else "",
                    "coordinate_table": (g.coordinate_table if g else "") or "",
                    "crs_mentions": ", ".join(g.crs_mentions) if g else "",
                    "georeferencing_method": g.method if g else "",
                }
            )
    return rows


def layer_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for d in a.documents:
        for s in d.sheets:
            p = _page_report(d, s)
            if p is None:
                continue
            owner = {name: r.layer_type for r in s.layer_types for name in r.layers}
            for layer in p.layers:
                rows.append(
                    {
                        "document_id": d.spec.id,
                        "file": s.file,
                        "page": s.page,
                        "layer_type": owner.get(layer.name, ""),
                        **layer.as_row(),
                    }
                )
    return rows


def layer_type_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for d in a.documents:
        for s in d.sheets:
            for r in s.layer_types:
                rows.append(
                    {
                        "document_id": d.spec.id,
                        "file": s.file,
                        "page": s.page,
                        "layer_type": r.layer_type,
                        "staged_as": STAGED_AS.get(r.layer_type, ""),
                        "class": r.status,
                        "pdf_layers": " | ".join(r.layers),
                        "matched_by": r.matched_by,
                        "forms": ", ".join(f"{k} {v}" for k, v in r.forms.items()),
                        "paths": r.paths,
                        "steps": " | ".join(r.steps),
                        "sample_faces": (r.sample or {}).get("faces", ""),
                        "sample_area_m2": (r.sample or {}).get("area_m2", "") or "",
                        "sample_method": (r.sample or {}).get("method", ""),
                        "expected": r.expected or "",
                        "expected_basis": r.expected_basis or "",
                        "faces_with_cadastral_base": (r.sample_with_cadastre or {}).get(
                            "faces", ""
                        ),
                        "id_labels": (r.labels or {}).get("form", ""),
                        "id_labels_distinct_text": (r.labels or {}).get("text_distinct", ""),
                        "note": r.note,
                    }
                )
    return rows


def georef_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for d in a.documents:
        for s in d.sheets:
            g = s.georef
            if g is None:
                continue
            rows.append(
                {
                    "document_id": d.spec.id,
                    "file": s.file,
                    "crs_epsg": g.crs_epsg or "",
                    "crs_basis": g.crs_basis or "",
                    "stated_scale": g.stated_scale or "",
                    "viewport_scale": g.viewport_scale or "",
                    "scale_used": g.scale or "",
                    "scale_basis": g.scale_basis,
                    "scale_warning": g.scale_warning or "",
                    "grid_labels": _grid_text(g.grid_labels),
                    "grid_label_fit": json.dumps(g.grid_labels.get("fit")) if g.grid_labels else "",
                    "grid_crosses": _lattice_text(g.grid_lattice),
                    "coordinate_table": g.coordinate_table or "",
                    "cadastral_base": _cadastral_text(g.cadastral_base),
                    "geodetic_control_paths": g.geodetic_points,
                    "crs_mentions": ", ".join(g.crs_mentions),
                    "geospatial_pdf": g.geospatial_pdf,
                    "method": g.method,
                    "control_points": g.control_points,
                }
            )
    return rows


def gis_rows(a: Assessment) -> list[dict[str, Any]]:
    rows = []
    for r in a.gis_files:
        if not r.layers:
            rows.append({"file": r.path, "driver": r.driver or "", "error": r.error or ""})
        for layer in r.layers:
            rows.append(
                {
                    "file": r.path,
                    "driver": r.driver,
                    "layer": layer.name,
                    "geometry": layer.geometry_type,
                    "features": layer.features,
                    "epsg": layer.epsg or "",
                    "crs": layer.crs_name or "",
                    "cad_layers": json.dumps(layer.cad_layers, ensure_ascii=False)
                    if layer.cad_layers
                    else "",
                    "error": r.error or "",
                }
            )
    return rows


def _grid_text(grid: dict[str, Any] | None) -> str:
    if not grid:
        return ""
    e, n = grid["eastings"], grid["northings"]
    return (
        f"EPSG:{grid['epsg']} E {e[0]:.0f}–{e[-1]:.0f}, N {n[0]:.0f}–{n[-1]:.0f}"
        f" ({len(e) + len(n)} labels, every {grid['interval_m']:.0f} m)"
    )


def _lattice_text(lat: dict[str, Any] | None) -> str:
    if not lat:
        return ""
    step = f", {lat['lattice_m']} m lattice" if lat.get("lattice_m") else ""
    return f"{lat['crosses']} crosses {lat['spacing_mm']} mm apart{step}"


def _cadastral_text(cad: dict[str, Any] | None) -> str:
    if not cad:
        return ""
    return f"{', '.join(cad['layers'])} ({cad['paths']} paths, numbers as {cad['labels']})"


# --- markdown ----------------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def redraw_hours(a: Assessment) -> float:
    """Hours of class C work (manual redraw) among the counted documents."""
    return sum(
        d.type_hours.get(t, 0.0)
        for d in a.documents
        if d.counted
        for t, s in d.type_status.items()
        if s == "C"
    )


def _units(a: Assessment) -> dict[str, int]:
    """Document x layer-type units by class, for the layers the POC builds."""
    units: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    for d in a.documents:
        if not d.counted:
            continue
        for t, s in d.type_status.items():
            if s in units and t not in DEFERRED_TYPES:
                units[s] += 1
    return units


def markdown(a: Assessment, inputs: list[dict[str, Any]], versions: dict[str, str]) -> str:
    lines: list[str] = []
    add = lines.append
    units = _units(a)
    total_units = sum(units.values()) or 1
    sheets = [s for d in a.documents for s in d.sheets if s.georef]
    sheet_classes = {c: sum(1 for s in sheets if s.sheet_class == c) for c in ("A", "B", "C")}
    add("# Geometry assessment — generated report")
    add("")
    add(
        f"Generated {date.today().isoformat()} by `python -m core.gis.assess` "
        f"({', '.join(f'{k} {v}' for k, v in versions.items())}). "
        "The summary for the gate is `docs/gis/geometry-assessment.md`; this file is the evidence."
    )
    add("")
    add("## 1. Split")
    add("")
    classes = ", ".join(f"{c} {n}" for c, n in sheet_classes.items())
    shares = ", ".join(f"{c} {n} ({n * 100 // total_units} %)" for c, n in units.items())
    add(
        f"Plan sheets: {len(sheets)} — {classes}. Document × layer-type units the POC builds "
        f"(deferred layers excluded): {shares}."
    )
    add("")
    extraction, georef = a.extraction_hours, a.georef_hours
    plan_07, plan_08 = a.plan_hours["vector_extraction"], a.plan_hours["georeferencing"]
    add(
        _md_table(
            [
                "Item",
                "Plan (estimate v2)",
                "Build (once)",
                "Apply (documents)",
                "Assessed",
                "Diff.",
            ],
            [
                [
                    "07 Vector PDF geometry extraction",
                    f"{plan_07:g} h",
                    f"{a.extraction_build_hours:g} h ({', '.join(a.techniques) or 'pipeline'})",
                    f"{a.extraction_apply_hours:g} h",
                    f"{extraction:g} h",
                    f"{extraction - plan_07:+g} h",
                ],
                [
                    "08 Georeferencing and reprojection",
                    f"{plan_08:g} h",
                    f"{a.georef_build_hours:g} h ({', '.join(a.georef_methods)})"
                    f" + datum {a.datum_hours:g} h",
                    f"{a.georef_apply_hours:g} h",
                    f"{georef:g} h",
                    f"{georef - plan_08:+g} h",
                ],
                [
                    "Zone definition: zones, KO and plan boundaries from the PUP maps",
                    f"{a.plan_hours['zone_definition']:g} h",
                    "(shares the 07 build)",
                    f"{a.base_map_hours:g} h",
                    f"{a.base_map_hours:g} h",
                    f"{a.base_map_hours - a.plan_hours['zone_definition']:+g} h",
                ],
                ["Manual redraw (class C)", "not planned", "", f"{redraw_hours(a):g} h", "", ""],
                [
                    "Deferred, not built in the POC: traffic network (MVP layer), GUP and "
                    "state / municipal borders (context)",
                    "not planned",
                    "",
                    f"{a.deferred_hours:g} h",
                    "",
                    "",
                ],
            ],
        )
    )
    add("")
    add(
        f"Coordinate system: {a.crs_note}. Datum transformation to ETRS89 / WGS 84: "
        f"{a.datum_hours:g} h once (see §5). Alternatives not counted: "
        + (", ".join(d.spec.name for d in a.documents if not d.counted) or "none")
        + "."
    )
    add("")
    add("## 2. Documents")
    add("")
    rows = []
    for d in a.documents:
        rows.append(
            [
                d.spec.name,
                d.spec.status,
                " ".join(sorted(s.sheet_class for s in d.sheets if s.georef)) or "—",
                ", ".join(f"{LAYER_TYPES.get(t, t)} {s}" for t, s in d.type_status.items()),
                f"{d.extraction_hours:g}",
                f"{d.georef_hours:g} ({d.georef_method})",
                d.decision + ("" if d.counted else " (alternative not counted)"),
                d.native_files,
            ]
        )
    add(
        _md_table(
            [
                "Document",
                "Status",
                "Sheet classes",
                "Layer types",
                "Extraction h",
                "Georef h",
                "Decision",
                "Native GIS/CAD",
            ],
            rows,
        )
    )
    add("")
    add("## 3. Layer types")
    add("")
    rows = []
    for d in a.documents:
        if d.spec.kind != "plan":
            continue
        for t in TARGET_TYPES + ("plan_boundary",):
            if t in d.type_status:
                steps = [STEP_TEXT[x] for x in d.type_steps.get(t, []) if x != "polygonize"] or [
                    "polygonize"
                ]
                deferred = " (deferred: MVP layer)" if t in DEFERRED_TYPES else ""
                rows.append(
                    [
                        d.spec.name,
                        LAYER_TYPES[t] + deferred,
                        STAGED_AS.get(t, ""),
                        d.type_status[t],
                        "; ".join(steps),
                        f"{d.type_hours[t]:g}",
                    ]
                )
            else:
                rows.append(
                    [d.spec.name, LAYER_TYPES[t], STAGED_AS.get(t, ""), "not supplied", "", ""]
                )
    add(
        _md_table(
            ["Document", "Layer type", "Staged as", "Class", "Extraction approach", "Hours"], rows
        )
    )
    add("")
    add("## 4. Sheets")
    for d in a.documents:
        for s in d.sheets:
            add("")
            add(f"### {s.file} — class {s.sheet_class}")
            add("")
            g = s.georef
            facts = [
                f"role {s.role}",
                f"paper {s.paper}" + (f", rotated {s.rotation}°" if s.rotation else ""),
                f"{s.paths:,} vector paths, {s.layers_used} PDF layers in use "
                f"({s.layered_share * 100:.0f} % of paths on a layer)",
                f"text layer {s.words} words "
                f"({s.shifted_words} only readable after the glyph-id shift)",
                f"largest image {s.image_cover_pct} % of the page"
                + (f" at {s.image_dpi} dpi" if s.image_dpi else ""),
            ]
            if g:
                facts.append(
                    f"scale {g.scale_basis}"
                    + (f" — **{g.scale_warning}**" if g.scale_warning else "")
                )
            add("; ".join(facts) + ".")
            add("")
            rows = []
            for r in s.layer_types:
                sample = r.sample or {}
                faces = ""
                if sample:
                    faces = str(sample.get("faces"))
                    if r.expected:
                        faces += f" / {r.expected} expected"
                    if r.sample_with_cadastre:
                        faces += f"; {r.sample_with_cadastre['faces']} with the cadastral base"
                    if sample.get("skipped"):
                        faces = f"not sampled ({sample['skipped']})"
                labels = ""
                if r.labels:
                    labels = (
                        f"{r.labels['form']} ({r.labels['text_distinct']} distinct)"
                        if r.labels["form"] == "text"
                        else r.labels["form"]
                    )
                rows.append(
                    [
                        r.label,
                        r.status,
                        ", ".join(r.layers[:5])
                        + (f" … +{len(r.layers) - 5}" if len(r.layers) > 5 else ""),
                        ", ".join(f"{k} {v:,}" for k, v in r.forms.items()),
                        faces,
                        labels,
                        "; ".join(STEP_TEXT[x] for x in r.steps),
                    ]
                )
            add(
                _md_table(
                    [
                        "Layer type",
                        "Class",
                        "PDF layers",
                        "Forms (paths)",
                        "Sample faces",
                        "Ids",
                        "Steps",
                    ],
                    rows,
                )
            )
            if s.previews:
                add("")
                add(
                    " ".join(
                        f"![{LAYER_TYPES.get(t, t)}](previews/{name})"
                        for t, name in s.previews.items()
                    )
                )
    add("")
    add("## 5. Georeferencing evidence")
    add("")
    rows = []
    for d in a.documents:
        for s in d.sheets:
            g = s.georef
            if not g:
                continue
            rows.append(
                [
                    s.file,
                    f"EPSG:{g.crs_epsg} — {g.crs_basis}" if g.crs_epsg else "",
                    _grid_text(g.grid_labels) or "—",
                    _lattice_text(g.grid_lattice) or "—",
                    g.coordinate_table or "—",
                    _cadastral_text(g.cadastral_base) or "—",
                    g.control_points,
                ]
            )
    add(
        _md_table(
            [
                "Sheet",
                "CRS",
                "Grid labels",
                "Grid crosses",
                "Coordinate table",
                "Cadastral base",
                "Method",
            ],
            rows,
        )
    )
    add("")
    add("## 6. Effort model")
    add("")
    e = a.effort

    def listed(hours: dict[str, float]) -> str:
        return ", ".join(f"{k.replace('_', ' ')} {v:g} h" for k, v in hours.items() if v)

    add(
        "Stated assumptions (`core.gis.assessment.EFFORT`). **Apply**, per document × layer type: "
        + ", ".join(f"class {c} {h:g} h" for c, h in e["class_h"].items())
        + f" (C = manual redraw of a 30–50 ha plan), plus per step: {listed(e['step_h'])}; "
        f"each further part of a split sheet set {e['split_part_h']:g} h. Georeferencing per "
        f"sheet: {listed(e['georef_h'])}; each further sheet of the same plan is registered to "
        f"the first ({e['georef_next_sheet_h']:g} h). **Build**, once: extraction pipeline "
        f"{e['build_h']['pipeline']:g} h plus the techniques the documents need "
        f"({listed({k: v for k, v in e['build_h'].items() if k != 'pipeline'})}); "
        f"georeferencing {e['georef_build_h']['base']:g} h plus the methods used "
        f"({listed({k: v for k, v in e['georef_build_h'].items() if k != 'base'})}); "
        f"datum {e['datum_h']:g} h."
    )
    add("")
    add("## 7. Inputs")
    add("")
    add(
        _md_table(
            ["File", "Size", "SHA-256"],
            [[i["file"], f"{i['size'] / 1e6:.1f} MB", f"`{i['sha256'][:16]}…`"] for i in inputs],
        )
    )
    add("")
    gis = [r for r in a.gis_files if isinstance(r, GisReport)]
    add("## 8. GIS / CAD files")
    add("")
    if not gis:
        add(
            "None supplied. When the client provides DWG / DXF / SHP / GeoPackage files, drop them "
            "next to the catalog and rerun: every layer is listed with its CRS and feature count "
            f"(GDAL: {a.gdal or 'not found'})."
        )
    else:
        add(
            _md_table(
                ["File", "Driver", "Layer", "Geometry", "Features", "EPSG", "Note"],
                [
                    [r.path, r.driver, lyr.name, lyr.geometry_type, lyr.features, lyr.epsg, r.error]
                    for r in gis
                    for lyr in r.layers
                ]
                or [[r.path, r.driver, "", "", "", "", r.error] for r in gis],
            )
        )
    add("")
    return "\n".join(lines)


def write_all(
    a: Assessment, out_dir: Path, inputs: list[dict[str, Any]], versions: dict[str, str]
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "documents.csv": document_rows(a),
        "pages.csv": page_rows(a),
        "layer_types.csv": layer_type_rows(a),
        "layers.csv": layer_rows(a),
        "georeferencing.csv": georef_rows(a),
        "gis_files.csv": gis_rows(a),
    }
    written = []
    for name, rows in files.items():
        _write_csv(out_dir / name, rows, GIS_FIELDS if name == "gis_files.csv" else None)
        written.append(out_dir / name)
    dump = {
        "versions": versions,
        "inputs": inputs,
        "effort_model": a.effort,
        "plan_hours": a.plan_hours,
        "extraction_hours": a.extraction_hours,
        "georeferencing_hours": a.georef_hours,
        "crs": a.crs_note,
        "documents": [
            {k: _jsonable(v) for k, v in dataclasses.asdict(d).items() if k != "reports"}
            | {"reports": [_jsonable(r) for r in d.reports]}
            for d in a.documents
        ],
        "gis_files": [_jsonable(r) for r in a.gis_files],
    }
    (out_dir / "assessment.json").write_text(
        json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(markdown(a, inputs, versions), encoding="utf-8")
    written += [out_dir / "assessment.json", out_dir / "report.md"]
    return written
