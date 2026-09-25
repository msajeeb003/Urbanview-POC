"""Vector tiles: one PMTiles archive per publish version, one source layer per map layer.

``TippecanoeTileBuilder`` runs tippecanoe once per layer (each with its own zoom range) and
merges the results with ``tile-join`` into a single archive, so every layer keeps its own name
and visibility on the map. Tests inject a :class:`TileBuilder` fake; the worker image builds
tippecanoe from source (``backend/Dockerfile.worker``, 2.17+ for PMTiles output).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("urbanview.jobs.tiles")


class TileBuildError(RuntimeError):
    """tippecanoe / tile-join failed or is not installed (a hard failure: never retried)."""


@dataclass(frozen=True, slots=True)
class LayerFile:
    """One exported layer: newline-delimited GeoJSON features on disk."""

    layer_id: str
    path: Path
    feature_count: int
    geometry_type: str  # polygon | line | point
    min_zoom: int
    max_zoom: int


@dataclass(frozen=True, slots=True)
class TileBuildReport:
    archive: Path
    size_bytes: int
    sha256: str
    layers: tuple[LayerFile, ...]
    tool: str


class TileBuilder(Protocol):
    def build(self, layers: Sequence[LayerFile], archive: Path) -> TileBuildReport: ...


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tippecanoe_command(binary: str, layer: LayerFile, output: Path) -> list[str]:
    """The per-layer tippecanoe invocation (a pure function, unit-tested)."""
    command = [
        binary,
        "--output",
        str(output),
        "--force",
        "--quiet",
        "--layer",
        layer.layer_id,
        f"--minimum-zoom={layer.min_zoom}",
        f"--maximum-zoom={layer.max_zoom}",
        # every feature survives at every zoom of the layer's range: parcels must never vanish
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--coalesce-densest-as-needed",
        "--extend-zooms-if-still-dropping",
    ]
    if layer.geometry_type == "polygon":
        command.append("--detect-shared-borders")
    if layer.geometry_type == "point":
        # keep every point at every zoom (tippecanoe thins points below the maximum zoom)
        command.append("--drop-rate=1")
    command.append(str(layer.path))
    return command


def tile_join_command(binary: str, parts: Sequence[Path], archive: Path) -> list[str]:
    return [
        binary,
        "--output",
        str(archive),
        "--force",
        "--no-tile-size-limit",
        *[str(part) for part in parts],
    ]


class TippecanoeTileBuilder:
    def __init__(self, tippecanoe: str = "tippecanoe", tile_join: str = "tile-join") -> None:
        self.tippecanoe = tippecanoe
        self.tile_join = tile_join

    def _run(self, command: list[str]) -> None:
        if shutil.which(command[0]) is None:
            raise TileBuildError(
                f"{command[0]!r} is not installed on this worker (tippecanoe 2.17+ is required)"
            )
        log.info("running %s", " ".join(command[:3]))
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            raise TileBuildError(f"{command[0]} exited with {completed.returncode}: {tail}")

    def build(self, layers: Sequence[LayerFile], archive: Path) -> TileBuildReport:
        if not layers:
            raise TileBuildError("no layers to build")
        work_dir = archive.parent / "parts"
        work_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        for layer in layers:
            part = work_dir / f"{layer.layer_id}.pmtiles"
            self._run(tippecanoe_command(self.tippecanoe, layer, part))
            parts.append(part)
        if len(parts) == 1:
            shutil.copyfile(parts[0], archive)
        else:
            self._run(tile_join_command(self.tile_join, parts, archive))
        return TileBuildReport(
            archive=archive,
            size_bytes=archive.stat().st_size,
            sha256=sha256_of(archive),
            layers=tuple(layers),
            tool="tippecanoe",
        )
