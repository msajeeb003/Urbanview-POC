"""Visual checks of an extraction.

- ``render_sheet``: the sheet as a PNG with a world file (``.pgw``) in the document's local frame,
  so QGIS draws the GeoPackage layers exactly over the PDF (the acceptance overlay);
- ``render_preview``: the same page with the extracted features drawn on it (outlines, a light
  fill per feature, the feature key at its representative point), for a quick look without QGIS.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
from shapely.geometry.base import BaseGeometry

from core.gis.extract.extract import Feature
from core.gis.extract.geometry import line_parts, polygon_parts
from core.gis.extract.sheet import Sheet

STYLE = {  # layer -> (stroke rgb, width pt, fill?)
    "plan_boundary": ((0.0, 0.0, 0.0), 2.4, False),
    "urban_blocks": ((0.1, 0.25, 0.85), 1.6, False),
    "urban_parcels": ((0.85, 0.1, 0.1), 0.8, True),
    "planned_land_use": ((0.1, 0.55, 0.2), 0.5, True),
    "planned_traffic": ((0.95, 0.5, 0.0), 1.2, False),
}


def _page(pdf: bytes, sheet: Sheet) -> tuple[pymupdf.Document, pymupdf.Page]:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    page = doc[sheet.rule.page - 1]
    page.set_rotation(0)  # the local frame follows the unrotated page
    return doc, page


def _clip(
    sheet: Sheet, local_bounds: tuple[float, float, float, float] | None, margin_m: float
) -> pymupdf.Rect | None:
    if local_bounds is None:
        return None
    x0, y0, x1, y1 = local_bounds
    k = sheet.metres_per_pt
    ox, oy = sheet.rule.offset_m
    px0 = (x0 - margin_m - ox) / k
    px1 = (x1 + margin_m - ox) / k
    py0 = sheet.height_pt - (y1 + margin_m - oy) / k
    py1 = sheet.height_pt - (y0 - margin_m - oy) / k
    rect = pymupdf.Rect(px0, py0, px1, py1) & pymupdf.Rect(0, 0, sheet.width_pt, sheet.height_pt)
    return None if rect.is_empty else rect


def render_sheet(
    pdf: bytes,
    sheet: Sheet,
    out_png: Path,
    dpi: int = 100,
    local_bounds: tuple[float, float, float, float] | None = None,
    margin_m: float = 20.0,
) -> Path:
    """The page as a PNG plus a world file mapping pixels to the local frame."""
    doc, page = _page(pdf, sheet)
    clip = _clip(sheet, local_bounds, margin_m) or pymupdf.Rect(
        0, 0, sheet.width_pt, sheet.height_pt
    )
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    step_pt = clip.width / pix.width  # page points per pixel
    k = sheet.metres_per_pt
    ox, oy = sheet.rule.offset_m
    size = step_pt * k
    cx = (clip.x0 + step_pt / 2) * k + ox
    cy = (sheet.height_pt - (clip.y0 + step_pt / 2)) * k + oy
    world = "\n".join(f"{v:.6f}" for v in (size, 0.0, 0.0, -size, cx, cy)) + "\n"
    out_png.with_suffix(".pgw").write_text(world, encoding="ascii")
    doc.close()
    return out_png


def _colour(key: str) -> tuple[float, float, float]:
    h = hashlib.sha256(key.encode()).digest()
    return (0.35 + h[0] / 400, 0.35 + h[1] / 400, 0.35 + h[2] / 400)


def _draw(shape: pymupdf.Shape, sheet: Sheet, geom: BaseGeometry, layer: str, key: str) -> None:
    stroke, width, fill = STYLE[layer]
    local = sheet.to_page(geom)
    for poly in polygon_parts(local):
        for ring in (poly.exterior, *poly.interiors):
            shape.draw_polyline([pymupdf.Point(x, y) for x, y in ring.coords])
        shape.finish(
            color=stroke,
            width=width,
            fill=_colour(key) if fill else None,
            fill_opacity=0.35 if fill else 1,
            even_odd=True,
            closePath=True,
        )
    for ln in line_parts(local):
        shape.draw_polyline([pymupdf.Point(x, y) for x, y in ln.coords])
        shape.finish(color=stroke, width=width, closePath=False)


def render_preview(
    pdf: bytes,
    sheet: Sheet,
    features: list[Feature],
    out_png: Path,
    dpi: int = 72,
    local_bounds: tuple[float, float, float, float] | None = None,
    margin_m: float = 20.0,
) -> Path:
    doc, page = _page(pdf, sheet)
    # one shape and one commit: every commit rewrites the page's (large) content stream
    shape = page.new_shape()
    order = [
        "planned_land_use",
        "urban_parcels",
        "urban_blocks",
        "planned_traffic",
        "plan_boundary",
    ]
    for layer in order:
        for f in features:
            if f.layer == layer:
                _draw(shape, sheet, f.geom, layer, f.key)
    for f in features:
        if f.layer not in ("urban_parcels", "urban_blocks"):
            continue
        p = sheet.to_page(f.geom.representative_point())
        size = 7 if f.layer == "urban_parcels" else 14
        colour = (0.6, 0.0, 0.0) if f.layer == "urban_parcels" else (0.0, 0.1, 0.6)
        text = f.key + ("*" if f.qa_flags - {"sliver_removed", "invalid_fixed"} else "")
        shape.insert_text(
            pymupdf.Point(p.x - size, p.y + size / 3), text, fontsize=size, color=colour
        )
    shape.commit()
    clip = _clip(sheet, local_bounds, margin_m) or pymupdf.Rect(
        0, 0, sheet.width_pt, sheet.height_pt
    )
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    doc.close()
    return out_png
