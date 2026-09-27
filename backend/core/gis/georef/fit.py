"""The transform from a document's local frame to the plan's projected CRS.

Points are taken from their sheet's PDF points to the document's local frame with the sheet's
proven scale and offset (``local = pt * scale * 0.0254 / 72 + offset_m``), so one transform serves
every sheet of a document; the per-sheet page -> CRS transforms are derived from it and stored
too. Least squares on centred coordinates (state-system coordinates are in the millions):

- ``helmert`` (4 parameters: scale, rotation, shift) for vector sheets plotted from CAD, where the
  drawing is an exact similarity of the ground;
- ``affine`` (6 parameters) for scanned or redrawn sheets (paper stretch, shear).

The result carries every point's residual, the RMSE overall and per sheet, the fitted scale (a
Helmert scale far from 1 means a wrong sheet scale) and is rejected above ``max_rmse_m`` or below
``min_points``. The stored JSON is what ``apply`` reads, so the same file gives the same output.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.gis.extract.rules import DocumentRules, SheetRule
from core.gis.georef.points import ControlPoint, points_hash
from core.gis.inspect_pdf import M_PER_PT

SCALE_WARNING = 0.01  # |helmert scale - 1| above this: the sheet's scale is probably wrong
# A point is an outlier when the fit of the other points misses it by more than 3 x their RMSE,
# the document's threshold and 10 cm (leave-one-out: with few points one bad point pulls the fit
# towards itself, so its own residual never stands out; checked with at least one spare point)
OUTLIER_FACTOR = 3.0
OUTLIER_FLOOR_M = 0.1


class FitError(ValueError):
    """Too few points, an unknown sheet, a degenerate configuration."""


def sheet_k(sheet: SheetRule) -> float:
    return M_PER_PT * sheet.scale


def to_local(sheet: SheetRule, x_pt: float, y_pt: float) -> tuple[float, float]:
    k = sheet_k(sheet)
    return (x_pt * k + sheet.offset_m[0], y_pt * k + sheet.offset_m[1])


@dataclass(frozen=True, slots=True)
class Transform:
    """E = a x + b y + c, N = d x + e y + f (x, y in the document's local frame, metres)."""

    method: str
    crs: str
    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    def apply(self, xy: np.ndarray) -> np.ndarray:
        x, y = xy[:, 0], xy[:, 1]
        return np.column_stack((self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f))

    def shapely_matrix(self) -> list[float]:
        """For ``shapely.affinity.affine_transform``: [a, b, d, e, xoff, yoff]."""
        return [self.a, self.b, self.d, self.e, self.c, self.f]

    def for_sheet(self, sheet: SheetRule) -> list[float]:
        """The same transform from the sheet's PDF points: [a, b, c, d, e, f]."""
        k = sheet_k(sheet)
        ox, oy = sheet.offset_m
        return [
            self.a * k,
            self.b * k,
            self.a * ox + self.b * oy + self.c,
            self.d * k,
            self.e * k,
            self.d * ox + self.e * oy + self.f,
        ]

    @property
    def scale(self) -> float:
        return math.sqrt(abs(self.a * self.e - self.b * self.d))

    @property
    def rotation_deg(self) -> float:
        return math.degrees(math.atan2(self.d, self.a))


@dataclass
class Residual:
    id: str
    sheet: str
    dx_m: float
    dy_m: float
    r_m: float
    outlier: bool = False


@dataclass
class FitResult:
    transform: Transform
    residuals: list[Residual]
    rmse_m: float
    max_residual_m: float
    points_used: int
    points_sha256: str
    sheets: list[dict[str, Any]]
    max_rmse_m: float
    min_points: int
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rmse_m <= self.max_rmse_m and self.points_used >= self.min_points

    def as_json(self, document: str) -> dict[str, Any]:
        t = self.transform
        return {
            "document": document,
            "crs": t.crs,
            "method": t.method,
            "params": {"a": t.a, "b": t.b, "c": t.c, "d": t.d, "e": t.e, "f": t.f},
            "scale": t.scale,
            "rotation_deg": t.rotation_deg,
            "rmse_m": self.rmse_m,
            "max_residual_m": self.max_residual_m,
            "max_rmse_m": self.max_rmse_m,
            "min_points": self.min_points,
            "points_used": self.points_used,
            "points_sha256": self.points_sha256,
            "sheets": self.sheets,
            "residuals": [
                {
                    "id": r.id,
                    "sheet": r.sheet,
                    "dx_m": round(r.dx_m, 4),
                    "dy_m": round(r.dy_m, 4),
                    "r_m": round(r.r_m, 4),
                    "outlier": r.outlier,
                }
                for r in self.residuals
            ],
            "warnings": self.warnings,
            "fitted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def _solve(local: np.ndarray, world: np.ndarray, method: str) -> tuple[float, ...]:
    lc = local.mean(axis=0)
    wc = world.mean(axis=0)
    lx, ly = (local - lc).T
    we, wn = (world - wc).T
    if method == "helmert":
        # E' = p x - q y, N' = q x + p y  (p = s cos t, q = s sin t)
        design = np.vstack((np.column_stack((lx, -ly)), np.column_stack((ly, lx))))
        target = np.concatenate((we, wn))
        (p, q), *_ = np.linalg.lstsq(design, target, rcond=None)
        a, b, d, e = p, -q, q, p
    else:
        design = np.column_stack((lx, ly))
        (a, b), *_ = np.linalg.lstsq(design, we, rcond=None)
        (d, e), *_ = np.linalg.lstsq(design, wn, rcond=None)
    c = wc[0] - (a * lc[0] + b * lc[1])
    f = wc[1] - (d * lc[0] + e * lc[1])
    return float(a), float(b), float(c), float(d), float(e), float(f)


def _leave_one_out(
    local: np.ndarray, world: np.ndarray, method: str, needed: int, max_rmse: float
) -> list[bool]:
    n = len(local)
    if n - 1 <= needed:  # no spare point: the others fit exactly, nothing to judge by
        return [False] * n
    flags = []
    for i in range(n):
        keep = np.arange(n) != i
        t = Transform(method, "", *_solve(local[keep], world[keep], method))
        rest = np.hypot(*(world[keep] - t.apply(local[keep])).T)
        rest_rmse = float(math.sqrt(float(np.mean(rest**2))))
        miss = float(np.hypot(*(world[i] - t.apply(local[i : i + 1])[0])))
        flags.append(miss > max(OUTLIER_FACTOR * rest_rmse, max_rmse, OUTLIER_FLOOR_M))
    return flags


def fit(
    points: list[ControlPoint],
    rules: DocumentRules,
    *,
    crs: str,
    method: str | None = None,
    max_rmse_m: float | None = None,
    min_points: int | None = None,
) -> FitResult:
    settings = rules.georef
    method = method or settings.method
    max_rmse = max_rmse_m if max_rmse_m is not None else settings.max_rmse_m
    minimum = min_points if min_points is not None else settings.min_points
    sheets = {s.id: s for s in rules.sheets}
    used = [p for p in points if p.enabled]
    unknown = sorted({p.sheet for p in used} - set(sheets))
    if unknown:
        raise FitError(f"control points on unknown sheets {unknown} (sheets: {sorted(sheets)})")
    needed = 2 if method == "helmert" else 3
    if len(used) < max(needed, 1):
        raise FitError(f"{len(used)} enabled control points: a {method} fit needs {needed}")
    local = np.array([to_local(sheets[p.sheet], p.x_pt, p.y_pt) for p in used], dtype=float)
    world = np.array([(p.easting, p.northing) for p in used], dtype=float)
    spread = np.ptp(local, axis=0)
    if float(np.hypot(*spread)) < 1.0:
        raise FitError("the control points all sit in one spot: spread them over the sheet")
    transform = Transform(method, crs, *_solve(local, world, method))
    delta = world - transform.apply(local)
    r = np.hypot(delta[:, 0], delta[:, 1])
    rmse = float(math.sqrt(float(np.mean(r**2))))
    outliers = _leave_one_out(local, world, method, needed, max_rmse)
    residuals = [
        Residual(p.id, p.sheet, float(dx), float(dy), float(rr), outlier=bad)
        for p, (dx, dy), rr, bad in zip(used, delta, r, outliers, strict=True)
    ]
    per_sheet = []
    for sheet in rules.sheets:
        rs = [x.r_m for x in residuals if x.sheet == sheet.id]
        per_sheet.append(
            {
                "sheet": sheet.id,
                "page": sheet.page,
                "points": len(rs),
                "rmse_m": round(math.sqrt(sum(v * v for v in rs) / len(rs)), 4) if rs else None,
                "page_to_crs": transform.for_sheet(sheet),
            }
        )
    warnings = []
    if len(used) < minimum:
        warnings.append(f"{len(used)} points: at least {minimum} are required (6+ recommended)")
    elif len(used) < 6:
        warnings.append(f"{len(used)} points: 6 or more are recommended")
    if method == "helmert" and abs(transform.scale - 1) > SCALE_WARNING:
        warnings.append(
            f"fitted scale {transform.scale:.4f}: the local frame is in ground metres, so the "
            "sheet's scale in the rules is probably wrong"
        )
    outliers = [x.id for x in residuals if x.outlier]
    if outliers:
        warnings.append(f"points with large residuals (check or disable them): {outliers}")
    if rmse > max_rmse:
        warnings.append(f"RMSE {rmse:.3f} m is above the document's threshold {max_rmse} m")
    return FitResult(
        transform=transform,
        residuals=residuals,
        rmse_m=round(rmse, 4),
        max_residual_m=round(float(r.max()), 4),
        points_used=len(used),
        points_sha256=points_hash(points),
        sheets=per_sheet,
        max_rmse_m=max_rmse,
        min_points=minimum,
        warnings=warnings,
    )


def write_transform(path: Path, result: FitResult, document: str) -> dict[str, Any]:
    data = result.as_json(document)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def read_transform(path: Path) -> tuple[Transform, dict[str, Any]]:
    if not path.is_file():
        raise FitError(f"no stored transform {path.name}: run `fit` first")
    data = json.loads(path.read_text(encoding="utf-8"))
    p = data["params"]
    return Transform(
        data["method"], data["crs"], p["a"], p["b"], p["c"], p["d"], p["e"], p["f"]
    ), data
