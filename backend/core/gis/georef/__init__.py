"""Georeferencing of extracted plan geometry (build plan ticket 08).

A plan's geometry comes out of the extraction (``core.gis.extract``) in the document's local frame
(ground metres, arbitrary origin) or, for a sheet redrawn by hand in QGIS, in the sheet's PDF
points. Control points tie positions on a sheet (PDF points, origin bottom-left, like every
``source_bbox``) to coordinates in the plan's projected CRS (``points``: one CSV per document,
built from the grid crosses with one seed coordinate, from coordinate labels or vertex tables, or
from cadastral corners). A Helmert or affine transform is fitted from the document's local frame to
the projected CRS, with residuals and RMSE, and rejected above the document's threshold (``fit``);
the stored transform is applied to every layer and GDAL reprojects the result to EPSG:4326
(``apply``). In PostGIS the planned parcels and blocks are snapped to the cadastral base within a
tolerance, validated (extent, overlap with the cadastre, overlapping parcels) and staged as a
versioned dataset for the review / publish flow (``stage``). CLI: ``python -m core.gis.georef``.
"""
