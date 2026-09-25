"""Classify plan sheets, weigh the georeferencing evidence and decide go / no-go (P0 gate 1).

Classes, per plan sheet and per layer type:

A  vector with identifiable layers: the features sit on CAD layers the profile (or the catalog)
   names and the sample extraction closes them; extraction is automatic, with the form-specific
   step listed (close dotted lines, ribbon holes, union fills, hatch extents).
B  vector but flattened / unlayered, or a layer that holds only part of the features (the rest
   drawn on the cadastral base): semi-automatic, separation by style and manual cleanup.
C  scanned raster: manual redraw by a surveying / GIS engineer (BRD §2.6, a probable extra cost).

Effort is estimated per document and layer type (the best sheet set that shows it), plus the
georeferencing of every sheet and the one-off datum transformation. The effort model is a set of
stated assumptions (``EFFORT``), printed in the report. Coverage counts adopted documents only.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from core.gis.inspect_gis import GIS_SUFFIXES, GisReport, find_ogrinfo, gdal_version, inspect_gis
from core.gis.inspect_pdf import (
    InspectConfig,
    LayerStats,
    PageReport,
    PdfReport,
    fold,
    inspect_pdf,
    layer_key,
    redraw_layers,
)
from core.gis.sample import (
    BOUNDARY_TOLERANCE_MM,
    fragment_spacing_mm,
    lattice_spacing,
    round_lattice,
    sample_faces,
)
from core.municipality import GisProfile

# --- vocabulary ----------------------------------------------------------------------------------

LAYER_TYPES: dict[str, str] = {
    "urban_parcels": "Urban parcels",
    "urban_blocks": "Urban blocks",
    "land_use": "Planned land use",
    "traffic_network": "Planned traffic network",
    "plan_boundary": "Plan boundary",
    "zones": "Zones",
    "plan_boundaries": "Plan boundaries (document coverage)",
    "city_boundary": "City (GUP) boundary",
    "admin_boundaries": "Administrative boundaries",
}
TARGET_TYPES = ("urban_parcels", "urban_blocks", "land_use", "traffic_network")
# Assessed like the others but not built in the POC: the POC map's layers are zones, cadastral
# parcels, planned parcels, land use, blocks and the two choropleths (estimate v2); traffic is
# an MVP layer (pilot technical scope §1.4) and the GUP / state / municipal borders are map
# context. Their hours are reported apart, never in ticket 07, and they never decide go / no-go.
DEFERRED_TYPES = ("traffic_network", "city_boundary", "admin_boundaries")
# UrbanView staging layer each type feeds (jobs.publish_layers.STAGED_LAYERS)
STAGED_AS = {
    "urban_parcels": "urban_parcels",
    "urban_blocks": "urban_blocks",
    "land_use": "land_use",
    "traffic_network": "traffic_network",
    "plan_boundary": "document_coverage",
    "plan_boundaries": "document_coverage",
    "zones": "zones",
}
POLYGON_TYPES = {
    "urban_parcels",
    "urban_blocks",
    "land_use",
    "plan_boundary",
    "zones",
    "plan_boundaries",
    "city_boundary",
}
LABEL_TYPE = {
    "urban_parcels": "urban_parcel_labels",
    "urban_blocks": "block_labels",
    "plan_boundaries": "plan_labels",
}
# polygon types read as areas (fills, hatch extents); the others are read from their boundaries
AREA_TYPES = {"land_use", "zones"}
# boundary layers that close the faces of another type (a block ends at the plan boundary)
CLOSING_TYPES = {"urban_parcels": ("plan_boundary",), "urban_blocks": ("plan_boundary",)}

STEP_TEXT = {
    "polygonize": "polygonize the layer's linework",
    "close_dotted_lines": "close the dotted / dashed linework (buffer-union) before polygonizing",
    "ribbon_holes": "parcels are the holes between wide-polyline outlines",
    "union_fills": "union the tessellated fills of each category layer",
    "hatch_extents": "areas from hatch-line extents, or urban parcels + the land-use attribute",
    "ocr_labels": "ids are drawn as vector glyphs: OCR the isolated label layer, or native file",
    "merge_cadastral_faces": (
        "boundaries partly on the cadastral base: polygonize with it and merge faces by parcel id"
    ),
    "style_separation": "no layer holds it: separate by colour / line style, clean up by hand",
    "dissolve_parcels": (
        "union of the urban parcels by block (the block comes from the parcel ids or the table)"
    ),
    "redraw": "scanned raster: georeference and redraw in QGIS",
}

# Effort model (hours): the assessment's stated assumptions, tuned with the team, never per sheet.
# "Build" is one-off development (tickets 07 / 08) of the techniques the documents need;
# "apply" is the per-document work of running it and checking the result.
EFFORT: dict[str, Any] = {
    # apply, per document x layer type: A run + topology QA; B semi-automatic separation and
    # cleanup; C manual redraw over the georeferenced scan (a 30-50 ha plan)
    "class_h": {"A": 0.5, "B": 3.0, "C": 8.0},
    "step_h": {  # apply, on top of the class, per document x layer type
        "polygonize": 0.0,
        "close_dotted_lines": 0.25,
        "ribbon_holes": 0.25,
        "union_fills": 0.25,
        "hatch_extents": 0.5,
        "ocr_labels": 0.5,  # check the OCR'd ids against the parameter table
        "merge_cadastral_faces": 1.5,
        "style_separation": 0.0,
        "dissolve_parcels": 0.25,
        "redraw": 0.0,
    },
    "split_part_h": 0.25,  # every further part of a split sheet set (10a / 10b)
    # ticket 07, once, for the techniques the counted documents need: what remains after this
    # assessment's code (layer matching from the profile / catalog, buffer-union polygonization
    # of lines, dots, dashes and ribbons, fill unions, layer re-drawing are in core.gis already)
    "build_h": {
        # features out of the sampled faces: id-label join, land-use codes, clip to the plan
        # boundary, staging GeoJSON (topology QA is excluded from the POC: "pipeline hardening")
        "pipeline": 4.0,
        "close_dotted_lines": 0.25,
        "ribbon_holes": 0.25,
        "union_fills": 0.25,
        "hatch_extents": 1.0,
        "ocr_labels": 2.5,  # OCR of the isolated label layer (tesseract), check against the table
        "merge_cadastral_faces": 1.5,
        "style_separation": 1.0,
        "dissolve_parcels": 0.5,
        "redraw": 0.0,
    },
    "georef_h": {  # apply, per sheet (the first sheet of a document; further sheets x factor)
        "geospatial_pdf": 0.25,
        "grid_labels": 0.5,
        "coordinate_table": 1.0,
        "grid_lattice": 0.5,
        "cadastral_match": 1.5,
        "manual": 4.0,
    },
    "georef_build_h": {  # ticket 08, once (grid-label fits and cross lattices exist already)
        "base": 2.0,  # affine fit, residual report, transform of staged geometry
        "geospatial_pdf": 0.5,
        "grid_labels": 0.25,
        "coordinate_table": 0.5,
        "grid_lattice": 1.0,  # seed position, snap every cross to its round coordinates
        "cadastral_match": 3.0,
        "manual": 0.0,
    },
    # every further sheet of the same plan is registered to its first georeferenced sheet on
    # common linework (plan boundary, cadastral corners): same drawing, another viewport
    "georef_next_sheet_h": 0.25,
    "datum_h": 2.0,  # once: MGI 1901 -> ETRS89 parameters for Montenegro (UZN or a local fit)
}
# estimate v2 (UrbanView_POC_Estimation_v2.xlsx), GIS track
PLAN_H = {"vector_extraction": 11.0, "georeferencing": 6.0, "zone_definition": 4.0}
COMPLETE_RATIO = 0.8  # sampled faces / expected features for a layer to count as complete


# --- catalog ----------------------------------------------------------------------------------


@dataclass
class FileSpec:
    path: str
    shows: list[str]
    layers: dict[str, list[str]] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)
    sheet_set: str | None = None
    coordinate_table: str | None = None  # reviewer's note: a vertex table the text layer lacks

    @property
    def set_key(self) -> str:
        return self.sheet_set or self.path


@dataclass
class DocumentSpec:
    id: str
    name: str
    kind: str  # plan | base_map
    status: str
    files: list[FileSpec]
    plan_type: str | None = None
    registry: str | None = None
    registry_url: str | None = None
    planner: str | None = None
    area_ha: float | None = None
    zone_candidate: str | None = None
    parameters: str | None = None
    parcel_id_pattern: str | None = None
    crs_epsg: int | None = None
    crs_basis: str | None = None
    alternative_group: str | None = None  # documents that supply the same layers (pick one)
    blocks_from_parcels: str | None = None  # how each parcel's block is known, when it is
    observations: list[str] = field(default_factory=list)


def load_catalog(path: Path) -> list[DocumentSpec]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    docs = []
    file_keys = set(FileSpec.__dataclass_fields__)
    doc_keys = set(DocumentSpec.__dataclass_fields__) - {"files"}
    for raw in data.get("document", []):
        files = [
            FileSpec(**{k: v for k, v in f.items() if k in file_keys}) for f in raw.get("files", [])
        ]
        docs.append(DocumentSpec(files=files, **{k: v for k, v in raw.items() if k in doc_keys}))
    return docs


# --- results -----------------------------------------------------------------------------------


@dataclass
class LayerTypeResult:
    layer_type: str
    status: str  # A | B | C | absent
    layers: list[str] = field(default_factory=list)
    matched_by: str = "none"  # catalog | profile | none
    forms: dict[str, int] = field(default_factory=dict)  # form -> paths
    paths: int = 0
    segments: int = 0
    steps: list[str] = field(default_factory=list)
    sample: dict[str, Any] | None = None
    sample_with_cadastre: dict[str, Any] | None = None
    expected: int | None = None
    expected_basis: str | None = None
    labels: dict[str, Any] | None = None  # the id labels of this type on the sheet
    note: str = ""

    @property
    def label(self) -> str:
        return LAYER_TYPES.get(self.layer_type, self.layer_type)


@dataclass
class Georef:
    stated_scale: int | None
    viewport_scale: float | None
    scale: float | None  # the scale the drawing has on the page
    scale_basis: str
    scale_warning: str | None = None
    crs_epsg: int | None = None
    crs_basis: str | None = None
    grid_labels: dict[str, Any] | None = None
    grid_lattice: dict[str, Any] | None = None
    coordinate_table: str | None = None
    cadastral_base: dict[str, Any] | None = None
    geodetic_points: int = 0
    crs_mentions: list[str] = field(default_factory=list)
    geospatial_pdf: bool = False
    method: str = "manual"
    control_points: str = ""


@dataclass
class SheetResult:
    document_id: str
    file: str
    page: int
    role: str
    title: str | None
    paper: str
    rotation: int
    raster: bool
    image_cover_pct: float
    image_dpi: float | None
    paths: int
    layers_used: int
    layered_share: float
    shifted_words: int
    words: int
    sheet_class: str  # A | B | C | T (table / text: no geometry)
    layer_types: list[LayerTypeResult]
    georef: Georef | None
    shows: list[str] = field(default_factory=list)
    previews: dict[str, str] = field(default_factory=dict)  # layer type -> png file name
    sheet_set: str = ""


@dataclass
class DocumentResult:
    spec: DocumentSpec
    sheets: list[SheetResult]
    reports: list[PdfReport | GisReport]
    parameter_ids: list[str] = field(default_factory=list)
    type_status: dict[str, str] = field(default_factory=dict)  # best status per layer type
    type_hours: dict[str, float] = field(default_factory=dict)
    type_steps: dict[str, list[str]] = field(default_factory=dict)
    extraction_hours: float = 0.0  # apply: running the pipeline on this document
    deferred_hours: float = 0.0  # apply, for the layer types the POC does not build
    georef_hours: float = 0.0  # apply: georeferencing its sheets
    georef_method: str = ""
    georef_methods: list[str] = field(default_factory=list)
    decision: str = ""  # GO | GO WITH CONDITIONS | NO-GO
    conditions: list[str] = field(default_factory=list)
    native_files: str = "not needed"  # required | recommended | not needed
    native_reason: str = ""
    counted: bool = True  # False for the unchosen alternative of an alternative group


@dataclass
class Assessment:
    documents: list[DocumentResult]
    gis_files: list[GisReport]
    gdal: str | None
    effort: dict[str, Any]
    plan_hours: dict[str, float]
    datum_hours: float
    crs_note: str
    inputs: list[dict[str, Any]] = field(default_factory=list)  # file, size, sha256

    @property
    def counted(self) -> list[DocumentResult]:
        return [d for d in self.documents if d.counted]

    @property
    def techniques(self) -> list[str]:
        """Extraction steps the counted documents need, each built once (ticket 07)."""
        steps = {
            step
            for d in self.counted
            for t, steps in d.type_steps.items()
            if t not in DEFERRED_TYPES
            for step in steps
        }
        return sorted(s for s in steps if self.effort["build_h"].get(s))

    @property
    def extraction_build_hours(self) -> float:
        build = self.effort["build_h"]
        return round(build["pipeline"] + sum(build[s] for s in self.techniques), 1)

    @property
    def extraction_apply_hours(self) -> float:
        """Plan documents (ticket 07); the PUP base maps belong to the zone-definition item."""
        return round(sum(d.extraction_hours for d in self.counted if d.spec.kind == "plan"), 1)

    @property
    def deferred_hours(self) -> float:
        return round(sum(d.deferred_hours for d in self.counted), 1)

    @property
    def base_map_hours(self) -> float:
        return round(sum(d.extraction_hours for d in self.counted if d.spec.kind != "plan"), 1)

    @property
    def extraction_hours(self) -> float:
        return round(self.extraction_build_hours + self.extraction_apply_hours, 1)

    @property
    def georef_methods(self) -> list[str]:
        return sorted({m for d in self.counted for m in d.georef_methods})

    @property
    def georef_build_hours(self) -> float:
        build = self.effort["georef_build_h"]
        return round(build["base"] + sum(build[m] for m in self.georef_methods), 1)

    @property
    def georef_apply_hours(self) -> float:
        return round(sum(d.georef_hours for d in self.counted), 1)

    @property
    def georef_hours(self) -> float:
        return round(self.georef_build_hours + self.georef_apply_hours + self.datum_hours, 1)


# --- layer matching --------------------------------------------------------------------------


def match_layers(
    stats: list[LayerStats], patterns: dict[str, list[str]], overrides: dict[str, list[str]]
) -> dict[str, tuple[list[LayerStats], str]]:
    """layer type -> (layers holding it, how they were matched)."""
    by_name = {s.name: s for s in stats}
    out: dict[str, tuple[list[LayerStats], str]] = {}
    for layer_type, names in overrides.items():
        out[layer_type] = ([by_name[n] for n in names if n in by_name], "catalog")
    for layer_type, regexes in patterns.items():
        if layer_type in out:
            continue
        compiled = [re.compile(r, re.IGNORECASE) for r in regexes]
        found = [
            s for s in stats if s.name and any(c.search(fold(layer_key(s.name))) for c in compiled)
        ]
        out[layer_type] = (found, "profile")
    return out


# --- per sheet -------------------------------------------------------------------------------


def is_raster(page: PageReport) -> bool:
    return page.image_cover_pct >= 60 and page.paths < 1000


def forms_of(stats: list[LayerStats], by_length: bool = False) -> dict[str, float]:
    """Form -> paths (or drawn length in mm) over the layers holding a type."""
    forms: Counter[str] = Counter()
    for s in stats:
        forms[s.form] += sum(s.length_mm.values()) if by_length else s.paths
    return dict(forms.most_common())


def steps_for(layer_type: str, stats: list[LayerStats]) -> list[str]:
    """Extraction steps from the forms that make up at least a quarter of the drawn length.

    Area types (land use, zones) are read from their fills or hatch extents; boundary types
    (parcels, blocks, plan boundaries) from their linework, in which filled pieces are the dashes
    of wide dashed lines or the outlines of wide polylines, closed by the buffer-union.
    """
    forms = forms_of(stats, by_length=True)
    total = sum(forms.values()) or 1
    dominant = {f for f, n in forms.items() if n >= 0.25 * total}
    steps: list[str] = []
    if layer_type in AREA_TYPES:
        if "tessellation" in dominant:
            steps.append("union_fills")
        if "hatch" in dominant:
            steps.append("hatch_extents")
        if "dashes" in dominant and not steps:
            steps.append("close_dotted_lines")
    elif layer_type in POLYGON_TYPES:
        if {"dashes", "tessellation"} & dominant:
            steps.append("close_dotted_lines")
        if "ribbons" in dominant:
            steps.append("ribbon_holes")
        if "hatch" in dominant:
            steps.append("hatch_extents")
    elif "dashes" in dominant:
        steps.append("close_dotted_lines")
    if not steps and layer_type in POLYGON_TYPES:
        steps.append("polygonize")
    return steps


def label_evidence(stats: list[LayerStats]) -> dict[str, Any] | None:
    if not stats:
        return None
    texts = sum(s.text_spans for s in stats)
    distinct = sum(s.text_distinct for s in stats)
    glyphs = sum(s.glyphs for s in stats)
    form = "text" if texts and texts >= glyphs / 10 else ("glyphs" if glyphs else "none")
    return {
        "layers": [s.name for s in stats],
        "text_spans": texts,
        "text_distinct": distinct,
        "glyph_paths": glyphs,
        "form": form,
        "sample": [t for s in stats for t in s.text_sample][:6],
    }


def resolve_scale(
    page: PageReport, lattices: list[dict[str, Any]]
) -> tuple[float | None, str, str | None, dict[str, Any] | None]:
    """The scale the drawing has on the page, and the grid-cross lattice that proves it.

    The stated scale, the viewport measure and the grid crosses (whose spacing must come out as a
    round number of metres) are checked against each other; a viewport measure that disagrees
    with a proven scale is reported, because measuring with it would scale every area wrongly.
    """
    viewport = page.main_scale
    stated = page.stated_scales[0] if page.stated_scales else None
    candidates = [s for s in (stated, viewport) if s]
    for lattice in sorted(lattices, key=lambda x: -x["crosses"]):
        for s in candidates:
            step = round_lattice(lattice["spacing_mm"], s)
            if not step:
                continue
            source = "stated" if s == stated else "viewport"
            basis = (
                f"{source} 1:{s:g}, confirmed by {lattice['crosses']} grid crosses {step} m apart"
            )
            warning = None
            if viewport and stated and abs(viewport - stated) / stated > 0.02:
                warning = (
                    f"the PDF viewport measure says 1:{viewport:g}, the grid proves 1:{s:g}: "
                    "never measure with the viewport factor of this file"
                )
            return float(s), basis, warning, {**lattice, "lattice_m": step, "spacing_m": step}
    if viewport and stated and abs(viewport - stated) / stated > 0.02:
        warning = f"viewport measure 1:{viewport:g} disagrees with the stated 1:{stated}"
        return float(stated), f"stated 1:{stated}", warning, None
    if viewport:
        agree = " (= stated)" if stated else ""
        return float(viewport), f"viewport 1:{viewport:g}{agree}", None, None
    if stated:
        return float(stated), f"stated 1:{stated}", None, None
    return None, "unknown", None, None


def choose_method(g: Georef) -> tuple[str, str]:
    if g.geospatial_pdf:
        return "geospatial_pdf", "embedded GeoPDF coordinates"
    if g.grid_labels:
        n = len(g.grid_labels["eastings"]) + len(g.grid_labels["northings"])
        return "grid_labels", f"affine fit on the labelled coordinate grid ({n} round labels)"
    if g.grid_lattice and g.grid_lattice.get("lattice_m"):
        step = g.grid_lattice["lattice_m"]
        seed = (
            "one vertex coordinate read from the table"
            if g.coordinate_table
            else f"one approximate position (within ±{step // 2} m, e.g. the georeferenced PUP map)"
        )
        crosses = g.grid_lattice["crosses"]
        return (
            "grid_lattice",
            f"{crosses} grid crosses on a {step} m lattice: {seed} snaps every cross to its "
            "round coordinates; affine fit on all crosses, checked on cadastral corners",
        )
    if g.coordinate_table:
        return "coordinate_table", "affine fit on numbered vertices from the coordinate table"
    if g.cadastral_base:
        return (
            "cadastral_match",
            "cadastral parcel corners matched to the UZN cadastre (KO + parcel number)",
        )
    return "manual", "manual control points on an orthophoto: no coordinate evidence on the sheet"


def _names_in(drawings: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    return [p for p in drawings if (p.get("layer") or "") in names]


def evaluate_sheet(
    doc: DocumentSpec,
    spec: FileSpec,
    page: pymupdf.Page,
    page_report: PageReport,
    drawings: list[dict[str, Any]],
    profile: GisProfile,
    parcel_count: int | None = None,
    preview_dir: Path | None = None,
    preview_prefix: str = "",
) -> SheetResult:
    """Classify one page. ``parcel_count`` (the parameter table's parcels) is the expected count
    for the urban parcel layer when the ids on the sheet are not readable as text."""
    raster = is_raster(page_report)
    patterns = profile.base_map_layers if doc.kind == "base_map" else profile.plan_layers
    matched = match_layers(page_report.layers, patterns, spec.layers)
    names_of = {t: [s.name for s in found] for t, (found, _) in matched.items()}
    plan_sheet = page_report.role == "plan_sheet"

    # georeferencing evidence first: the scale it proves feeds the sample areas
    control = matched.get("geodetic_control", ([], ""))[0]
    lattices = []
    for layer in control:
        lat = lattice_spacing(_names_in(drawings, {layer.name}), None)
        if lat:
            lattices.append({**lat, "layers": [layer.name]})
    scale, basis, warning, lattice = resolve_scale(page_report, lattices)
    grid = page_report.grid[0] if page_report.grid else None
    cadastral = [
        s for s in matched.get("cadastral_parcels", ([], ""))[0] if s.paths or s.text_spans
    ]
    georef = Georef(
        stated_scale=page_report.stated_scales[0] if page_report.stated_scales else None,
        viewport_scale=page_report.main_scale,
        scale=scale,
        scale_basis=basis,
        scale_warning=warning,
        grid_labels=None
        if grid is None
        else {
            "epsg": grid.epsg,
            "crs": grid.crs_name,
            "eastings": grid.eastings,
            "northings": grid.northings,
            "interval_m": grid.interval_m,
            "fit": grid.fit,
        },
        grid_lattice=lattice,
        coordinate_table=(
            f"{page_report.coordinate_pairs} coordinate pairs readable as text"
            if page_report.coordinate_pairs >= 4
            else spec.coordinate_table
        ),
        cadastral_base=None
        if not cadastral
        else {
            "layers": [s.name for s in cadastral],
            "paths": sum(s.paths for s in cadastral),
            "labels": "text" if sum(s.text_spans for s in cadastral) else "vector strokes / glyphs",
        },
        geodetic_points=sum(s.paths for s in control),
        crs_mentions=page_report.crs_mentions,
        geospatial_pdf=any(v.geospatial for v in page_report.viewports),
    )
    if grid is not None:
        georef.crs_epsg, georef.crs_basis = grid.epsg, "coordinate grid labels on the sheet"
    elif doc.crs_epsg:
        georef.crs_epsg, georef.crs_basis = doc.crs_epsg, doc.crs_basis or "catalog"
    elif profile.crs_candidates:
        georef.crs_epsg = profile.crs_candidates[0].epsg
        georef.crs_basis = "profile default (the state system); confirm on the first control point"
    georef.method, georef.control_points = choose_method(georef)

    # layer types
    results: list[LayerTypeResult] = []
    extra_targets = [t for t in TARGET_TYPES if doc.kind == "plan" and matched.get(t, ([], ""))[0]]
    for layer_type in dict.fromkeys(spec.shows + extra_targets):
        found = [s for s in matched.get(layer_type, ([], ""))[0] if s.paths or s.text_spans]
        how = matched.get(layer_type, ([], "none"))[1] if found else "none"
        r = LayerTypeResult(
            layer_type=layer_type, status="absent", layers=[s.name for s in found], matched_by=how
        )
        r.paths = sum(s.paths for s in found)
        r.segments = sum(s.segments for s in found)
        r.forms = forms_of(found)
        r.labels = label_evidence(matched.get(LABEL_TYPE.get(layer_type, ""), ([], ""))[0])
        if raster:
            r.status, r.steps = "C", ["redraw"]
        elif not found:
            if layer_type in spec.shows and page_report.paths >= 50:
                r.status, r.steps = "B", ["style_separation"]
                r.note = "no layer matches this type on the sheet"
        else:
            r.status = "A"
            r.steps = steps_for(layer_type, found)
            if r.labels and r.labels["form"] == "glyphs":
                r.steps.append("ocr_labels")
            r.expected, r.expected_basis = _expected(spec, layer_type, r.labels, parcel_count)
            if layer_type in POLYGON_TYPES and plan_sheet:
                _sample(r, drawings, names_of, scale)
        results.append(r)

    previews: dict[str, str] = {}
    if preview_dir is not None and plan_sheet:
        previews = _previews(page, drawings, results, names_of, preview_dir, preview_prefix)

    layered = [s for s in page_report.layers if s.name and s.paths]
    sheet_class = sheet_class_of(raster, plan_sheet, page_report.layered_path_share, len(layered))
    return SheetResult(
        document_id=doc.id,
        file=spec.path,
        page=page_report.page,
        role=page_report.role,
        title=page_report.title,
        paper=page_report.paper,
        rotation=page_report.rotation,
        raster=raster,
        image_cover_pct=page_report.image_cover_pct,
        image_dpi=page_report.image_dpi,
        paths=page_report.paths,
        layers_used=len(layered),
        layered_share=page_report.layered_path_share,
        shifted_words=page_report.shifted_words,
        words=page_report.text_words,
        sheet_class=sheet_class,
        layer_types=results,
        georef=georef if plan_sheet else None,
        shows=spec.shows,
        previews=previews,
        sheet_set=spec.set_key,
    )


def sheet_class_of(raster: bool, plan_sheet: bool, layered_share: float, layers: int) -> str:
    """A vector with identifiable layers, B vector but flattened / unlayered, C scanned raster,
    T no geometry (tables, text). Whether a layer holds all of its features is the layer type's
    class, not the sheet's."""
    if raster:
        return "C"
    if not plan_sheet:
        return "T"
    return "B" if layered_share < 0.6 or layers < 3 else "A"


def _expected(
    spec: FileSpec, layer_type: str, labels: dict[str, Any] | None, parcel_count: int | None
) -> tuple[int | None, str | None]:
    if layer_type in spec.expected:
        return spec.expected[layer_type], "catalog"
    if labels and labels["form"] == "text" and labels["text_distinct"]:
        return labels["text_distinct"], "distinct id labels on the sheet"
    if layer_type == "urban_parcels" and parcel_count:
        return parcel_count, "parcels in the parameter table"
    return None, None


def _sample(
    r: LayerTypeResult,
    drawings: list[dict[str, Any]],
    names_of: dict[str, list[str]],
    scale: float | None,
) -> None:
    names = set(r.layers)
    for closing in CLOSING_TYPES.get(r.layer_type, ()):
        names |= set(names_of.get(closing, []))
    paths = _names_in(drawings, names)
    fills = "union_fills" in r.steps
    hatch = "hatch_extents" in r.steps and not fills
    # dotted / dashed linework (the type's own, or the boundary that closes it) sets the tolerance
    dotted = (
        paths if "close_dotted_lines" in r.steps else _names_in(drawings, names - set(r.layers))
    )
    spacing = fragment_spacing_mm(dotted) if dotted else None
    tolerance = max(BOUNDARY_TOLERANCE_MM, round(0.6 * spacing, 2)) if spacing else None
    r.sample = sample_faces(
        paths, scale, hatch=hatch, fills_as_areas=fills, tolerance_mm=tolerance
    ).as_dict()
    r.sample["tolerance_mm"] = tolerance or None
    faces = r.sample["faces"]
    if r.sample.get("skipped"):
        r.note = f"not sampled ({r.sample['skipped']}): the class is not verified"
        return
    if not r.expected or faces >= COMPLETE_RATIO * r.expected:
        return
    r.status = "B"
    r.note = f"the layer closes {faces} faces for {r.expected} expected ({r.expected_basis})"
    extra = set(names_of.get("cadastral_parcels", []))
    if r.layer_type == "urban_parcels" and extra:
        with_cadastre = sample_faces(
            _names_in(drawings, names | extra), scale, tolerance_mm=tolerance
        )
        r.sample_with_cadastre = with_cadastre.as_dict()
        if with_cadastre.faces >= COMPLETE_RATIO * r.expected:
            r.steps.append("merge_cadastral_faces")
            r.note += f"; with the cadastral base {with_cadastre.faces} faces"


PREVIEW_MAX_SEGMENTS = 1_500_000  # larger layer sets (hatched map layers) are not re-drawn
PREVIEW_COLOURS = {
    "urban_parcels": (0.0, 0.2, 0.85),
    "urban_blocks": (0.75, 0.0, 0.6),
    "land_use": (0.1, 0.55, 0.15),
    "traffic_network": (0.3, 0.3, 0.3),
    "plan_boundary": (0.85, 0.0, 0.0),
    "zones": (0.1, 0.45, 0.2),
    "plan_boundaries": (0.0, 0.2, 0.85),
    "city_boundary": (0.85, 0.0, 0.0),
    "admin_boundaries": (0.3, 0.3, 0.3),
    "cadastral_parcels": (0.72, 0.72, 0.72),
}


def _previews(
    page: pymupdf.Page,
    drawings: list[dict[str, Any]],
    results: list[LayerTypeResult],
    names_of: dict[str, list[str]],
    out_dir: Path,
    prefix: str,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for r in results:
        if not r.layers or r.status == "C" or r.segments > PREVIEW_MAX_SEGMENTS:
            continue
        groups = [(r.layers, PREVIEW_COLOURS.get(r.layer_type, (0.0, 0.0, 0.0)))]
        if r.layer_type == "urban_parcels" and r.sample_with_cadastre:
            groups.insert(
                0, (names_of.get("cadastral_parcels", []), PREVIEW_COLOURS["cadastral_parcels"])
            )
        path = out_dir / f"{prefix}-{r.layer_type}.png"
        path.write_bytes(redraw_layers(page, groups, 1100, drawings=drawings))
        written[r.layer_type] = path.name
    return written


# --- per document -------------------------------------------------------------------------------


def parameter_ids(path: Path, pattern: str) -> list[str]:
    """Urban parcel ids a parameter table lists (its text layer as extracted, no decoding)."""
    doc = pymupdf.open(path)
    text = "\n".join(p.get_text("text") for p in doc)
    return sorted(set(re.findall(pattern, text)))


def _rank(status: str) -> int:
    return {"A": 0, "B": 1, "C": 2, "absent": 3}.get(status, 4)


def _verified(r: LayerTypeResult) -> bool:
    return bool(r.sample and r.expected and not r.sample.get("skipped"))


def effective_status(r: LayerTypeResult) -> str:
    """The status aggregation uses: a boundary layer whose linework closes no face at all is
    not automatic, whatever the sheet looked like."""
    if (
        r.status == "A"
        and r.layer_type in POLYGON_TYPES
        and r.layer_type not in AREA_TYPES
        and r.sample
        and not r.sample.get("skipped")
        and r.sample.get("faces") == 0
    ):
        return "B"
    return r.status


def _derive_blocks(result: DocumentResult) -> None:
    """Blocks as the union of their parcels when the parcel ids or the parameter table name
    each parcel's block (catalog ``blocks_from_parcels``) and the block layer does not close."""
    source = result.spec.blocks_from_parcels
    parcels = result.type_status.get("urban_parcels")
    if not source or parcels not in ("A", "B") or result.type_status.get("urban_blocks") == "A":
        return
    result.type_status["urban_blocks"] = parcels
    result.type_steps["urban_blocks"] = ["dissolve_parcels"]
    result.type_hours["urban_blocks"] = (
        EFFORT["class_h"]["A"] + EFFORT["step_h"]["dissolve_parcels"]
    )


def aggregate(result: DocumentResult) -> None:
    """Best status and effort per layer type, georeferencing hours, then the decision."""
    class_h, step_h = EFFORT["class_h"], EFFORT["step_h"]
    by_type: dict[str, dict[str, list[LayerTypeResult]]] = {}
    for sheet in result.sheets:
        for r in sheet.layer_types:
            if r.layer_type in sheet.shows:
                by_type.setdefault(r.layer_type, {}).setdefault(sheet.sheet_set, []).append(r)
    for layer_type, sets in by_type.items():
        # a set whose sample was checked against an expected count outranks unchecked sets
        checked = {k: v for k, v in sets.items() if any(_verified(r) for r in v)}
        best: tuple[float, str, list[str]] | None = None
        for results in (checked or sets).values():
            statuses = {effective_status(r) for r in results}
            status = next((s for s in ("C", "B", "absent") if s in statuses), "A")
            steps = list(dict.fromkeys(s for r in results for s in r.steps))
            hours = 0.0
            if status != "absent":
                hours = class_h[status] + sum(step_h.get(s, 0.0) for s in steps)
                hours += EFFORT["split_part_h"] * (len(results) - 1)
            better = best is None or (_rank(status), hours) < (_rank(best[1]), best[0])
            if better:
                best = (hours, status, steps)
        assert best is not None
        result.type_hours[layer_type] = round(best[0], 1)
        result.type_status[layer_type] = best[1]
        result.type_steps[layer_type] = best[2]
    _derive_blocks(result)
    result.extraction_hours = round(
        sum(h for t, h in result.type_hours.items() if t not in DEFERRED_TYPES), 1
    )
    result.deferred_hours = round(
        sum(h for t, h in result.type_hours.items() if t in DEFERRED_TYPES), 1
    )

    georef_h = EFFORT["georef_h"]
    plan_sheets = sorted(
        (s for s in result.sheets if s.georef is not None),
        key=lambda s: georef_h[s.georef.method],  # type: ignore[union-attr]
    )
    if result.spec.kind == "base_map":  # separate maps: each is georeferenced on its own
        hours = sum(georef_h[sh.georef.method] for sh in plan_sheets)  # type: ignore[union-attr]
        methods = {sh.georef.method for sh in plan_sheets}  # type: ignore[union-attr]
    elif plan_sheets:
        first = plan_sheets[0].georef.method  # type: ignore[union-attr]
        hours = georef_h[first] + EFFORT["georef_next_sheet_h"] * (len(plan_sheets) - 1)
        methods = {first}
    else:
        hours, methods = 0.0, set()
    result.georef_hours = round(hours, 2)
    result.georef_methods = sorted(methods)
    result.georef_method = plan_sheets[0].georef.method if plan_sheets else "none"  # type: ignore[union-attr]
    decide(result)


def decide(result: DocumentResult) -> None:
    """Go / no-go for the vector extraction item (ticket 07), and whether native files help."""
    spec = result.spec
    statuses = {
        t: s
        for t, s in result.type_status.items()
        if (t in TARGET_TYPES or spec.kind == "base_map") and t not in DEFERRED_TYPES
    }
    if not any(s.georef for s in result.sheets):
        result.decision, result.native_files = "NO-GO", "required"
        result.native_reason = "no plan sheet among the supplied files"
        return
    if "C" in statuses.values() or result.georef_method == "manual":
        result.decision, result.native_files = "NO-GO", "required"
        result.native_reason = (
            "scanned sheets or no coordinate evidence: vector extraction cannot run"
        )
        return
    conditions: list[str] = []
    for t, s in statuses.items():
        steps = result.type_steps.get(t, [])
        if s == "B":
            listed = "; ".join(STEP_TEXT[x] for x in steps if x != "polygonize")
            conditions.append(f"{LAYER_TYPES.get(t, t)} is semi-automatic: {listed}")
        elif "ocr_labels" in steps:
            conditions.append(f"{LAYER_TYPES.get(t, t)}: {STEP_TEXT['ocr_labels']}")
    if spec.kind == "plan":
        for t in TARGET_TYPES:
            if t not in statuses and t not in DEFERRED_TYPES:
                conditions.append(f"{LAYER_TYPES[t]}: not on the supplied sheets")
    result.conditions = conditions
    result.decision = "GO WITH CONDITIONS" if conditions else "GO"
    heavy = any(
        s == "B" or {"ocr_labels", "merge_cadastral_faces"} & set(result.type_steps.get(t, []))
        for t, s in statuses.items()
    )
    if heavy:
        result.native_files = "recommended"
        result.native_reason = (
            "the planner's DWG / DXF carries the parcel polygons, their ids as text and the vertex "
            "coordinates as data, which removes the semi-automatic steps"
        )


def refresh(assessment: Assessment, catalog: Path | None = None) -> None:
    """Recompute effort, decisions and alternatives from cached inspection results (after the
    effort model, the rules or the catalog's document facts changed; the sheets are not read
    again). With ``catalog``, each document's facts are re-read from it."""
    if catalog is not None:
        specs = {spec.id: spec for spec in load_catalog(catalog)}
        for d in assessment.documents:
            d.spec = specs.get(d.spec.id, d.spec)
            for sheet in d.sheets:  # a CRS the reviewer confirmed replaces the profile default
                g = sheet.georef
                if g and d.spec.crs_epsg and (g.crs_basis or "").startswith("profile default"):
                    g.crs_epsg, g.crs_basis = d.spec.crs_epsg, d.spec.crs_basis or "catalog"
    for d in assessment.documents:
        for sheet in d.sheets:
            sheet.sheet_class = sheet_class_of(
                sheet.raster, sheet.role == "plan_sheet", sheet.layered_share, sheet.layers_used
            )
        d.type_status, d.type_hours, d.type_steps = {}, {}, {}
        d.conditions, d.native_files, d.native_reason, d.counted = [], "not needed", "", True
        aggregate(d)
    pick_alternatives(assessment.documents)
    assessment.effort, assessment.plan_hours = EFFORT, PLAN_H
    assessment.datum_hours = EFFORT["datum_h"]


def pick_alternatives(documents: list[DocumentResult]) -> None:
    """Within an alternative group only the best document counts toward effort and coverage."""
    groups: dict[str, list[DocumentResult]] = {}
    for d in documents:
        if d.spec.alternative_group:
            groups.setdefault(d.spec.alternative_group, []).append(d)
    for members in groups.values():

        def key(d: DocumentResult) -> tuple[int, int, int, float]:
            # only the layers the POC builds decide between alternatives
            kept = [t for t in d.type_status if t not in DEFERRED_TYPES]
            worst = max((_rank(d.type_status[t]) for t in kept), default=4)
            unverified = sum(
                1
                for sh in d.sheets
                for r in sh.layer_types
                if r.layer_type in kept and (r.sample or {}).get("skipped")
            )
            steps = sum(len(d.type_steps.get(t, [])) for t in kept)
            return worst, unverified, steps, d.extraction_hours + d.georef_hours

        best = min(members, key=key)
        for d in members:
            d.counted = d is best


# --- orchestration ------------------------------------------------------------------------------


def run(
    catalog: Path,
    profile: GisProfile,
    preview_dir: Path | None = None,
    ogrinfo: str | None = None,
    progress: Callable[[str], None] = print,
) -> Assessment:
    base = catalog.parent
    config = InspectConfig.from_profile(profile)
    ogr = find_ogrinfo(ogrinfo)
    documents: list[DocumentResult] = []
    catalogued: set[Path] = set()
    for spec in load_catalog(catalog):
        result = DocumentResult(spec=spec, sheets=[], reports=[])
        if spec.parameters and spec.parcel_id_pattern:
            result.parameter_ids = parameter_ids(base / spec.parameters, spec.parcel_id_pattern)
            catalogued.add((base / spec.parameters).resolve())
        parcel_sets = Counter(f.set_key for f in spec.files if "urban_parcels" in f.shows)
        for fspec in spec.files:
            path = (base / fspec.path).resolve()
            catalogued.add(path)
            if path.suffix.lower() == ".pdf":
                progress(f"inspecting {fspec.path}")
                count = len(result.parameter_ids) if parcel_sets.get(fspec.set_key) == 1 else None
                result.reports.append(
                    inspect_pdf(
                        path,
                        config,
                        on_page=_hook(spec, fspec, profile, count, preview_dir, result),
                    )
                )
            elif path.suffix.lower() in GIS_SUFFIXES or path.is_dir():
                progress(f"inspecting {fspec.path} (GDAL)")
                result.reports.append(inspect_gis(path, ogr))
        aggregate(result)
        documents.append(result)
    pick_alternatives(documents)

    # GIS / CAD files dropped next to the catalog without an entry are inspected too
    loose = [
        p
        for p in sorted(base.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in GIS_SUFFIXES
        and p.resolve() not in catalogued
        and p.name != catalog.name
    ]
    gis_reports = []
    for p in loose:
        progress(f"inspecting {p.relative_to(base)} (GDAL)")
        gis_reports.append(inspect_gis(p, ogr))
    catalogued_gis = [r for d in documents for r in d.reports if isinstance(r, GisReport)]
    return Assessment(
        documents=documents,
        gis_files=catalogued_gis + gis_reports,
        gdal=gdal_version(ogr) if ogr else None,
        effort=EFFORT,
        plan_hours=PLAN_H,
        datum_hours=EFFORT["datum_h"],
        crs_note=crs_note(profile, documents),
    )


def _hook(
    spec: DocumentSpec,
    fspec: FileSpec,
    profile: GisProfile,
    parcel_count: int | None,
    preview_dir: Path | None,
    result: DocumentResult,
) -> Callable[[pymupdf.Page, PageReport, list[dict[str, Any]]], None]:
    def on_page(page: pymupdf.Page, report: PageReport, drawings: list[dict[str, Any]]) -> None:
        # preview names: the file's stem as a slug ("novi-grad-1-i-2-land-use-p1")
        prefix = re.sub(r"[^a-z0-9]+", "-", fold(f"{Path(fspec.path).stem} p{report.page}"))
        result.sheets.append(
            evaluate_sheet(
                spec,
                fspec,
                page,
                report,
                drawings,
                profile,
                parcel_count,
                preview_dir,
                prefix.strip("-"),
            )
        )

    return on_page


def crs_note(profile: GisProfile, documents: list[DocumentResult]) -> str:
    found = Counter(
        s.georef.crs_epsg
        for d in documents
        for s in d.sheets
        if s.georef and s.georef.crs_basis == "coordinate grid labels on the sheet"
    )
    if not found:
        return "No coordinate labels on the sheets; the profile's first candidate is assumed."
    epsg, n = found.most_common(1)[0]
    name = next((c.name for c in profile.crs_candidates if c.epsg == epsg), str(epsg))
    return f"EPSG:{epsg} ({name}), read from the coordinate grid labels of {n} sheet(s)"
