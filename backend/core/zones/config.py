"""The zone tooling's configuration: one ``data/zones/<municipality>/zones.toml`` per municipality.

Everything place-specific (paths, the reference listing and its zone ids, the registry's URLs and
type names, the editing CRS, the tolerances, the base map) comes from that file; a new
municipality is a new folder and file, never code. ``ZONES_DATA_DIR`` points elsewhere than the
repository's ``data/zones``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "zones"


def data_dir() -> Path:
    return Path(os.environ.get("ZONES_DATA_DIR") or DEFAULT_DATA_DIR)


@dataclass(frozen=True, slots=True)
class ReferenceConfig:
    name: str
    url: str
    capture: Path
    zone_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EregistriConfig:
    registry_page: str
    list_url: str
    details_url: str
    snapshot: Path
    match_threshold: float
    invalid_markers: tuple[str, ...]
    columns: dict[str, int]
    types: dict[str, str]

    def details(self, document_id: str | int) -> str:
        return self.details_url.format(id=document_id)


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    overlap_tolerance_m2: float = 1.0
    gap_tolerance_m2: float = 1.0
    extent: Path | None = None
    require_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class TemplateConfig:
    basemap_name: str = "OpenStreetMap"
    basemap_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    basemap_zmax: int = 19
    reference_layers: tuple[str, ...] = (
        "cadastral_municipalities",
        "cadastral_parcels",
        "document_coverage",
        "current_zones",
    )
    initial_zones: Path | None = None
    layout_paper: str = "A3"


@dataclass(frozen=True, slots=True)
class ZoneSetConfig:
    municipality_id: str
    root: Path  # data/zones/<municipality>
    title: str
    dataset_prefix: str
    editing_crs: int
    geopackage: Path
    project: Path
    zones_seed: Path
    documents: Path
    zones_export: Path
    reports: Path
    reference: ReferenceConfig
    eregistri: EregistriConfig
    validation: ValidationConfig
    template: TemplateConfig
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _path(root: Path, value: str | None) -> Path | None:
    return (root / value) if value else None


def load_zone_config(municipality_id: str, base: Path | None = None) -> ZoneSetConfig:
    root = (base or data_dir()) / municipality_id
    path = root / "zones.toml"
    if not path.is_file():
        raise FileNotFoundError(f"no zone configuration at {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if raw.get("municipality_id") != municipality_id:
        raise ValueError(f"{path}: municipality_id must be {municipality_id!r}")
    files = raw.get("files", {})
    ref = raw.get("reference", {})
    reg = raw.get("eregistri", {})
    val = raw.get("validation", {})
    tpl = raw.get("template", {})
    return ZoneSetConfig(
        municipality_id=municipality_id,
        root=root,
        title=raw.get("title", f"{municipality_id} zones"),
        dataset_prefix=raw.get("dataset_prefix", f"{municipality_id}-zones"),
        editing_crs=int(raw.get("editing_crs", 4326)),
        geopackage=root / files.get("geopackage", f"{municipality_id}_zones.gpkg"),
        project=root / files.get("project", f"{municipality_id}_zones.qgz"),
        zones_seed=root / files.get("zones_seed", "zones.csv"),
        documents=root / files.get("documents", "zone_documents.csv"),
        zones_export=root / files.get("zones_export", "zones.geojson"),
        reports=root / files.get("reports", "reports"),
        reference=ReferenceConfig(
            name=ref.get("name", ""),
            url=ref.get("url", ""),
            capture=root / ref.get("capture", "source/reference.txt"),
            zone_ids=dict(ref.get("zone_ids", {})),
        ),
        eregistri=EregistriConfig(
            registry_page=reg.get("registry_page", ""),
            list_url=reg.get("list_url", ""),
            details_url=reg.get("details_url", "{id}"),
            snapshot=root / reg.get("snapshot", "source/eregistri.json"),
            match_threshold=float(reg.get("match_threshold", 0.72)),
            invalid_markers=tuple(reg.get("invalid_markers", ())),
            columns={k: int(v) for k, v in reg.get("columns", {}).items()},
            types=dict(reg.get("types", {})),
        ),
        validation=ValidationConfig(
            overlap_tolerance_m2=float(val.get("overlap_tolerance_m2", 1.0)),
            gap_tolerance_m2=float(val.get("gap_tolerance_m2", 1.0)),
            extent=_path(root, val.get("extent")),
            require_confirmed=bool(val.get("require_confirmed", False)),
        ),
        template=TemplateConfig(
            basemap_name=tpl.get("basemap_name", "OpenStreetMap"),
            basemap_url=tpl.get("basemap_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
            basemap_zmax=int(tpl.get("basemap_zmax", 19)),
            reference_layers=tuple(tpl.get("reference_layers", TemplateConfig.reference_layers)),
            initial_zones=_path(root, tpl.get("initial_zones")),
            layout_paper=tpl.get("layout_paper", "A3"),
        ),
        raw=raw,
    )


def available_municipalities(base: Path | None = None) -> list[str]:
    root = base or data_dir()
    return sorted(p.parent.name for p in root.glob("*/zones.toml"))
