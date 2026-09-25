"""Vector PDF geometry extraction (build plan ticket 07; assessment class A / B sheets).

A per-document YAML rule file (``rules``) says which PDF layers, styles and labels make up each
target layer; the extraction reads the sheets with pymupdf (``sheet``), builds and cleans the
geometry with shapely (``geometry``), reads parcel numbers from text or from vector glyph
outlines (``glyphs``), assigns attributes and writes a GeoPackage (``gpkg``) plus a QA summary
(``extract``). Output coordinates are the document's local frame: ground metres, x east, y
north, origin arbitrary (not georeferenced: ticket 08 fits the transform to the state system).
Every feature also carries its sheet, page, raw path ids and a source bbox in the sheet's own
PDF points (origin bottom-left, like the review queue), so a reviewer can trace it on the page.

Cadastral parcels never come out of here: they are imported from UZN into their own table. The
planned (urban) parcels, blocks, land use and traffic network are separate layers, never merged.
"""
