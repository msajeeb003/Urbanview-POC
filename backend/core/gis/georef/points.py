"""Control points: one CSV per document, next to its extraction rules.

Columns: ``id`` (unique in the file), ``sheet`` (a sheet id of the rules), ``x_pt`` / ``y_pt``
(position on that sheet in PDF points, origin bottom-left: the frame of every ``source_bbox``),
``easting`` / ``northing`` (metres in the plan's projected CRS), ``source`` (grid | label | table |
cadastre | corner | manual), ``note``, ``enabled`` (1 / 0: a point is disabled, never deleted, so
the record of what was tried stays). The file is the input; the fitted transform is stored next to
it and on the dataset version.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

COLUMNS = ("id", "sheet", "x_pt", "y_pt", "easting", "northing", "source", "note", "enabled")
SOURCES = ("grid", "label", "table", "cadastre", "corner", "manual")


@dataclass(frozen=True, slots=True)
class ControlPoint:
    id: str
    sheet: str
    x_pt: float
    y_pt: float
    easting: float
    northing: float
    source: str = "manual"
    note: str = ""
    enabled: bool = True

    def row(self) -> list[str]:
        return [
            self.id,
            self.sheet,
            repr(float(self.x_pt)),
            repr(float(self.y_pt)),
            repr(float(self.easting)),
            repr(float(self.northing)),
            self.source,
            self.note,
            "1" if self.enabled else "0",
        ]


def points_path(rules_path: Path, configured: str | None) -> Path:
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else rules_path.parent / path
    return rules_path.with_name(f"{rules_path.stem}.points.csv")


def transform_path(rules_path: Path) -> Path:
    return rules_path.with_name(f"{rules_path.stem}.transform.json")


def read_points(path: Path) -> list[ControlPoint]:
    if not path.is_file():
        return []
    points: list[ControlPoint] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            pid = (row.get("id") or "").strip()
            if not pid:
                raise ValueError(f"{path.name} line {n}: a point needs an id")
            if pid in seen:
                raise ValueError(f"{path.name} line {n}: id {pid!r} is used twice")
            seen.add(pid)
            source = (row.get("source") or "manual").strip() or "manual"
            if source not in SOURCES:
                raise ValueError(f"{path.name} line {n}: source {source!r}; use one of {SOURCES}")
            try:
                point = ControlPoint(
                    id=pid,
                    sheet=(row.get("sheet") or "").strip(),
                    x_pt=float(row["x_pt"]),
                    y_pt=float(row["y_pt"]),
                    easting=float(row["easting"]),
                    northing=float(row["northing"]),
                    source=source,
                    note=(row.get("note") or "").strip(),
                    enabled=(row.get("enabled") or "1").strip() not in ("0", "false", "no"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name} line {n}: {exc}") from exc
            points.append(point)
    return points


def write_points(path: Path, points: list[ControlPoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(COLUMNS)
        for p in points:
            writer.writerow(p.row())


def points_hash(points: list[ControlPoint]) -> str:
    """Fingerprint of the enabled points: a stored transform is only applied while it matches."""
    digest = hashlib.sha256()
    for p in sorted((p for p in points if p.enabled), key=lambda p: p.id):
        digest.update(("\t".join(p.row()) + "\n").encode("utf-8"))
    return digest.hexdigest()


def add_points(existing: list[ControlPoint], new: list[ControlPoint]) -> list[ControlPoint]:
    """Append points; an id already in the file is replaced (the grid suggester re-run)."""
    by_id = {p.id: p for p in existing}
    for p in new:
        by_id[p.id] = p
    order = [p.id for p in existing] + [p.id for p in new if p.id not in {q.id for q in existing}]
    return [by_id[i] for i in order]


def set_enabled(points: list[ControlPoint], ids: set[str], enabled: bool) -> list[ControlPoint]:
    missing = ids - {p.id for p in points}
    if missing:
        raise ValueError(f"no control point {sorted(missing)}")
    return [replace(p, enabled=enabled) if p.id in ids else p for p in points]
