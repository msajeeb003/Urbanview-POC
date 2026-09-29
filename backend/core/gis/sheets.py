"""The week-1 assessment's sheet classes, as pure rules (no PDF library): the assessment
(``core.gis.assessment``) measures a sheet and classifies it with these, and the PDF
pre-processing (``core.extraction.preprocess``) applies the same raster rule to every page it
reads, so the admin console's "needs QGIS redraw" flag is the assessment's own decision.

- A: a vector sheet whose PDF layers (OCGs = the CAD layers) identify the plan's layers;
- B: vector, but flattened / unlayered (the layers hold too little of the drawing);
- C: a scanned raster: nothing to extract, the sheet is georeferenced and redrawn in QGIS
  (BRD §2.6, a probable extra cost);
- T: no geometry (tables, text).
"""

from __future__ import annotations

RASTER_IMAGE_COVER_PCT = 60.0  # the largest single image covers this share of the page, or more
RASTER_MAX_PATHS = 1000  # ... and fewer vector paths than this are drawn on it
NEEDS_REDRAW = "C"


def is_raster_sheet(image_cover_pct: float, paths: int) -> bool:
    """A scanned sheet: one image covers most of the page and hardly anything is drawn."""
    return image_cover_pct >= RASTER_IMAGE_COVER_PCT and paths < RASTER_MAX_PATHS


def sheet_class_of(raster: bool, plan_sheet: bool, layered_share: float, layers: int) -> str:
    """A vector with identifiable layers, B vector but flattened / unlayered, C scanned raster,
    T no geometry (tables, text). Whether a layer holds all of its features is the layer type's
    class, not the sheet's."""
    if raster:
        return "C"
    if not plan_sheet:
        return "T"
    return "B" if layered_share < 0.6 or layers < 3 else "A"
