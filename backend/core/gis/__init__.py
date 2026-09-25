"""GIS track tooling (build plan: GIS track; BRD "Geometry extraction").

P0 gate 1 lives here today: ``core.gis.assess`` inspects the client's planning PDFs and any
GIS / CAD files, classifies every plan sheet (A vector with identifiable layers, B vector but
flattened, C scanned raster), records the georeferencing evidence and writes the assessment
report that sets the extraction-vs-redraw split and the coverage plan. The vector extraction and
georeferencing items (P1) build on the same inspection code.

Heavy dependencies (pymupdf, shapely, GDAL's ``ogrinfo``) are optional extras (``pip install -e
".[gis]"``) imported only by the submodules that need them; the API never imports this package.
Place-specific knowledge (CAD layer naming, sheet titles, coordinate systems) comes from the
municipality profile's ``[gis]`` table, never from code.
"""
