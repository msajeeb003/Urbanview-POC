"""Per-document extraction rules (YAML), one file per planning document.

Every plan is drawn differently, so which PDF layers, styles and labels make up the parcels,
blocks, land use and traffic network is configuration, not code: a rules file per document,
written with the style-cluster helper (``python -m core.gis.extract styles``) and the assessment
report, versioned next to the municipality profile (``municipalities/<id>/extraction/*.yaml``).
Manual fixes (an offset, an excluded legend, a label override) are recorded here too, never by
editing the extracted data.

Coordinates in rules: sheet regions in the sheet's PDF points (origin bottom-left, like the
review queue); tolerances in millimetres on paper (``*_mm``); areas and offsets in ground metres
(``*_m2``, ``offset_m``) once the sheet's proven scale applies.
"""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.gis.inspect_pdf import fold, layer_key

# target layers of the extraction, in build order (later layers may use earlier ones), and the
# UrbanView staging layer each one feeds (jobs.publish_layers.STAGED_LAYERS)
TARGET_LAYERS = (
    "plan_boundary",
    "urban_parcels",
    "urban_blocks",
    "planned_land_use",
    "planned_traffic",
)
STAGED_AS = {
    "plan_boundary": "document_coverage",
    "urban_parcels": "urban_parcels",
    "urban_blocks": "urban_blocks",
    "planned_land_use": "land_use",
    "planned_traffic": "traffic_network",
}
LINE_LAYERS = {"planned_traffic"}
RULES_SUFFIXES = (".yaml", ".yml")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Selector(_Model):
    """Which drawing paths (or text spans) a rule takes; every condition given must hold."""

    layer: str | None = None  # exact PDF layer (optional-content group) name
    layer_regex: str | None = None  # on the accent-folded name without the xref prefix
    stroke: str | None = None  # "#rrggbb"
    fill: str | None = None  # "#rrggbb"; "none" = stroke only
    width_min: float | None = None  # line width, points
    width_max: float | None = None
    dashed: bool | None = None
    filled: bool | None = None
    min_size_mm: float | None = None  # longest side of the path's box on paper
    max_size_mm: float | None = None

    @field_validator("stroke", "fill")
    @classmethod
    def _colour(cls, v: str | None) -> str | None:
        if v is None or v == "none":
            return v
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(f"colour {v!r}: use #rrggbb")
        return v.lower()

    @cached_property
    def _regex(self) -> re.Pattern[str] | None:
        return re.compile(self.layer_regex, re.IGNORECASE) if self.layer_regex else None

    def matches_layer(self, name: str) -> bool:
        if self.layer is not None and name != self.layer:
            return False
        rx = self._regex
        return not (rx is not None and not rx.search(fold(layer_key(name))))


class LabelRule(_Model):
    """Where the ids of a layer's features are written and how to read them."""

    source: Literal["text", "glyphs"] = "text"
    select: list[Selector]
    pattern: str  # regex; group 1 is the attribute value, the whole match the label text
    attribute: str  # urban_parcel_number | block_ref | land_use_code
    max_distance_mm: float = 4.0  # nearest feature when no polygon contains the label
    # glyphs: characters drawn as filled vector outlines, read by template matching
    glyph_height_mm: tuple[float, float] = (2.0, 8.0)
    glyph_chars: str = "0123456789UPABCDEFGHIKabcdefgh/.-"
    font: str = "helv"  # MuPDF built-in font name ("hebo" for bold lettering), or a .ttf path
    min_score: float = 0.6
    max_gap: float = 0.65  # glyphs further apart (in cap heights) start a new label

    @field_validator("pattern")
    @classmethod
    def _one_group(cls, v: str) -> str:
        if re.compile(v).groups < 1:
            raise ValueError("the label pattern needs one capture group (the attribute value)")
        return v


class Category(_Model):
    """A land-use (or road) category: the paths that draw it and the code it stands for."""

    select: list[Selector]
    code: str
    name: str | None = None


class LayerRule(_Model):
    """How one target layer is built.

    methods: ``polygonize`` (node the linework, faces; dots and dashes no longer than
    ``dot_max_mm`` stand for their centre / axis, gaps up to ``gap_mm`` are bridged and dangling
    ends are extended up to ``extend_mm`` to meet other linework),
    ``holes`` (wide polylines plotted as filled ribbons: buffer by ``gap_mm``, union, the holes
    grown back are the faces), ``fills`` (union of the filled pieces of each category),
    ``classify`` (the polygons of ``classify_from``, each coded by the category covering most of
    it, hatch lines counted ``gap_mm`` wide, or by a code label inside it), ``derive`` (union of
    ``derive_from`` features grouped by ``derive_by``), ``lines`` (linework merged into lines,
    dash gaps up to ``gap_mm`` bridged).
    """

    method: Literal["polygonize", "holes", "fills", "classify", "derive", "lines"]
    select: list[Selector] = Field(default_factory=list)
    closing: list[str] = Field(default_factory=list)  # target layers closing the faces
    fallback: list[Selector] = Field(default_factory=list)  # extra linework for unmatched labels
    gap_mm: float | None = None
    dot_max_mm: float = 3.0  # polygonize: closed pieces up to this size are dots / dashes
    extend_mm: float = 0.0  # polygonize: dangling ends reach other linework within this
    snap_mm: float = 0.05
    min_area_m2: float = 2.0
    max_area_m2: float | None = None
    min_length_m: float = 1.0  # lines shorter than this are annotation
    labels: LabelRule | None = None
    keep: Literal["all", "labelled"] = "all"
    categories: list[Category] = Field(default_factory=list)
    classify_from: str | None = None
    min_share: float = 0.3  # classify: the winning category must cover this share of the polygon
    derive_from: str | None = None
    derive_by: str | None = None
    close_m: float = 0.0  # derive: morphological closing that joins parts across narrow gaps
    clip: bool = True  # clip to the plan boundary (drops legends, title blocks, neighbours)
    road_class: str | None = None  # lines: constant attribute

    @model_validator(mode="after")
    def _method_needs(self) -> LayerRule:
        m = self.method
        if m in ("polygonize", "holes", "lines") and not self.select:
            raise ValueError(f"method {m} needs `select`")
        if m == "fills" and not self.categories and not self.select:
            raise ValueError("method fills needs `categories` or `select`")
        if m == "classify" and not (self.classify_from and self.categories):
            raise ValueError("method classify needs `classify_from` and `categories`")
        if m == "derive" and not (self.derive_from and self.derive_by):
            raise ValueError("method derive needs `derive_from` and `derive_by`")
        return self


class SheetRule(_Model):
    id: str
    file: str  # relative to the source folder (the CLI); the job finds sheets by sha256
    sha256: str | None = None
    page: int = 1
    scale: float  # the drawing's scale denominator, proven by the assessment (1:1000 -> 1000)
    offset_m: tuple[float, float] = (0.0, 0.0)  # sheet frame -> document frame, ground metres
    layers: list[str]  # the target layers this sheet supplies
    exclude: list[tuple[float, float, float, float]] = Field(
        default_factory=list
    )  # regions to ignore, sheet PDF points (x0, y0, x1, y1), origin bottom-left

    @field_validator("layers")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [x for x in v if x not in TARGET_LAYERS]
        if unknown:
            raise ValueError(f"unknown target layers {unknown}; use {list(TARGET_LAYERS)}")
        return v


class TableSource(_Model):
    """Pairs read from a parameter table's text layer (the table is a text PDF)."""

    pdf: str
    pattern: str  # one group: the id; two groups: (key, value)
    sha256: str | None = None


class ParcelBlocks(_Model):
    """How each parcel's block is known: from its number, or from the parameter table."""

    from_number: str | None = None  # regex on urban_parcel_number, group 1 = block ref
    table: TableSource | None = None  # groups (block, parcel number)

    @model_validator(mode="after")
    def _one(self) -> ParcelBlocks:
        if bool(self.from_number) == bool(self.table):
            raise ValueError("parcel_blocks: give exactly one of from_number / table")
        return self


class DocumentInfo(_Model):
    id: str  # the assessment catalog id
    name: str
    registry: str | None = None


class DocumentRules(_Model):
    document: DocumentInfo
    sheets: list[SheetRule]
    layers: dict[str, LayerRule]
    expected_parcels: TableSource | None = None  # the parcel ids the plan must yield (QA)
    parcel_blocks: ParcelBlocks | None = None
    label_overrides: dict[str, str] = Field(default_factory=dict)  # label text read -> corrected

    @field_validator("layers")
    @classmethod
    def _targets(cls, v: dict[str, LayerRule]) -> dict[str, LayerRule]:
        unknown = [x for x in v if x not in TARGET_LAYERS]
        if unknown:
            raise ValueError(f"unknown target layers {unknown}; use {list(TARGET_LAYERS)}")
        return v

    @model_validator(mode="after")
    def _references(self) -> DocumentRules:
        for name, rule in self.layers.items():
            for ref in [*rule.closing, rule.classify_from, rule.derive_from]:
                if ref and ref not in self.layers:
                    raise ValueError(f"layer {name} refers to {ref!r}, which has no rule")
        supplied = {layer for s in self.sheets for layer in s.layers}
        for name, rule in self.layers.items():
            if rule.method not in ("derive", "classify") and name not in supplied:
                raise ValueError(f"layer {name}: no sheet supplies it (sheets[].layers)")
        return self

    def sheet_for_sha256(self, sha256: str) -> SheetRule | None:
        return next((s for s in self.sheets if s.sha256 and s.sha256 == sha256), None)


def load_rules(path: Path) -> DocumentRules:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return DocumentRules.model_validate(data)


def rules_dir(municipality_id: str) -> Path:
    from core.municipality import PROFILES_DIR  # noqa: PLC0415

    return PROFILES_DIR / municipality_id / "extraction"


def find_rules(
    municipality_id: str, sha256: str, directory: Path | None = None
) -> tuple[Path, DocumentRules] | None:
    """The rules file listing a sheet with this SHA-256 (how the job finds a stored PDF's rules)."""
    folder = directory or rules_dir(municipality_id)
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in RULES_SUFFIXES:
            rules = load_rules(path)
            if rules.sheet_for_sha256(sha256):
                return path, rules
    return None
