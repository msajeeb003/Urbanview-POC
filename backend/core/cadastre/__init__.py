"""The cadastral base loader: existing cadastral parcels and their cadastral municipalities (KO).

A source adapter (``adapters``: the UZN geoportal or eMapa for parcels, eKatastar for the ownership
and legal-burden flags) supplies an export, but only where bulk access is recorded as confirmed in
``[cadastre.sources.<id>]`` of the municipality profile (``config``): otherwise it stops with a
clear message; there is no scraping fallback. ogr2ogr reads the export in whatever format it comes
(``ogr``: Shapefile, GeoPackage, GML, DXF, a zip, a WFS layer) and reprojects it to EPSG:4326;
the records are mapped to KO, number, sub-number and address (``normalise``), loaded into PostGIS,
validated and staged as a versioned dataset with a diff against the previous version (``dataset``,
``report``). The publish job applies a staged dataset (``dataset.apply_cadastral_datasets``).
CLI: ``python -m core.cadastre``.
"""
