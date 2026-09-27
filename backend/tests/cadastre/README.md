# Cadastral sample extracts (tests)

Small hand-made exports in the shape the loader expects from UZN (ETRS89 / UTM 34N, EPSG:25834,
field names of the `[cadastre.sources.uzn_geoportal.fields]` placeholders), a few metres from
Njegoševa in Podgorica. Synthetic: the KOs are "Test KO Alpha" / "Test KO Beta", not real ones.

- `parcels_v1.geojson`: six parcels in two KOs; the same number 1234/5 in both KOs, a
  self-intersecting parcel (repaired into two triangles, 300 m²), a number written as "1236/1".
- `parcels_v2.geojson`: the next export: one parcel removed, one added, one geometry and one
  address changed, three unchanged.
- `parcels_v3.geojson`: only one Alpha parcel (a large change; Beta out of scope).
- `parcels_duplicate.geojson`: the same (KO, number, sub-number) twice.
- `ko_boundaries.geojson`: delivered KO boundaries.
