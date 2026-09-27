# Georeferencing plan geometry

The extraction (`python -m core.gis.extract run`, build plan ticket 07) gives each planning
document's layers (plan boundary, planned urban parcels, blocks, land use, traffic network) in the
document's local frame: ground metres with no known origin. Georeferencing (`backend/core/gis/georef/`,
migration 0025, ticket 08) brings them onto the map:

1. **Control points** tie positions on a sheet to coordinates in the plan's projected CRS.
2. **Fit**: a Helmert or affine transform with every residual and the RMSE per sheet, rejected
   above the document's threshold.
3. **Apply**: the stored transform on every layer, then GDAL (ogr2ogr) to EPSG:4326 into a GeoPackage.
4. **Stage** (PostGIS): snap planned parcels and blocks to the cadastral base, validate, and stage
   one versioned dataset for the publish job. The residual report shows on the document in the
   admin console.

## Frames and coordinate systems

| Frame | Units | Where |
|---|---|---|
| sheet | PDF points, origin bottom-left (like every `source_bbox`) | control points, redrawn sheets (`--frame sheet:<id>`) |
| document local | ground metres: `pt × scale × 0.0254 / 72 + offset_m` per sheet (the rules file) | the extraction's GeoPackage |
| plan CRS | metres, `georef.crs` in the rules, else the profile's `source_crs_epsg` | control-point coordinates, the fitted transform |
| storage and tiles | EPSG:4326 | the GeoPackage, the staged dataset, the publish job |

The ticket expected the plans in EPSG:25834. The geometry assessment found otherwise: the
Podgorica plans are drawn in **EPSG:3908** (MGI 1901 / Balkans zone 6). The vertex tables write the
easting as X, with the zone prefix: X = 6 603 501.68. `source_crs_epsg = 3908` in the profile says
so. **Datum:** the only MGI 1901 → WGS 84 operation PROJ has for Montenegro (EPSG:3965) is good to
about 10 m. Two ways to do better:

- **Control points from the cadastre.** Cadastral corners from the loaded base (EPSG:25834,
  ETRS89 / MONREF97): set `georef.crs: EPSG:25834`. The fit then maps straight into the cadastre's
  CRS and no datum shift is involved.
- **A better operation for GDAL.** Put it in `georef.transform` (an ogr2ogr `-ct` string, e.g. a
  Helmert fitted on common points), or pass it with `apply --ct`.

The staging measures what is left: the mean vector from the plan's vertices to the cadastral
vertices they follow (see *Snapping*).

## Per document: the procedure

Everything runs from `backend/`. RULES is the document's rules file
(`municipalities/podgorica/extraction/<doc>.yaml`). The control points sit next to it in
`<doc>.points.csv` and the fitted transform in `<doc>.transform.json`. Both are versioned with the
rules.

```bash
# 1. control points: grid crosses from one seed coordinate (within ±50 m), and / or single points
python -m core.gis.georef grid RULES --source ../docs/gis/source --sheet parcels \
    --seed X_PT Y_PT EASTING NORTHING --write
python -m core.gis.georef add RULES --sheet parcels --x-pt 812.4 --y-pt 377.9 \
    --easting 6603501.68 --northing 4700163.54 --source table --note "vertex 214"
python -m core.gis.georef points RULES              # list, with residuals once fitted
python -m core.gis.georef disable RULES parcels-g017 # leave a point out (never deleted)

# 2. fit: residual per point, RMSE per sheet; stored only under the threshold (exit 1 above)
python -m core.gis.georef fit RULES [--method affine]

# 3 + 4. apply the stored transform, GDAL to EPSG:4326, snap, validate, stage
python -m core.gis.extract run RULES --source ../docs/gis/source --out ../data/gis/<doc>
python -m core.gis.georef apply RULES --gpkg ../data/gis/<doc>/<doc>.gpkg \
    --out ../data/gis/<doc> --document-id N --stage [--store]

# then the publish job (admin console, "Publish") serves it
python -m core.gis.georef datasets --document-id N
python -m core.gis.georef show geo-N-20260927-1 [--transform-out FILE]
```

**Control-point sources** (`source` column): `grid` (state-grid crosses), `label` (a coordinate
printed on the sheet), `table` (a vertex-table point whose number is found on the sheet), `cadastre`
(a cadastral corner the plan keeps), `corner` (a sheet corner of known coordinates), `manual`.
At least 4 enabled points (`georef.min_points`); 6 or more are recommended. Points are spread over
the sheet (the fit refuses a cluster). Points on several sheets of one document are fitted
together. Each sheet's offset in the rules puts it in the one local frame, so one transform serves
every sheet. The per-sheet page → CRS parameters are derived and stored too.

**Grid crosses.** The DUP / UP sheets carry the state grid as small crosses at round 100 m
coordinates: Novi Grad has 68 on layer `MREZA` at 1:1000, Stara Varoš 28 + 19 at 1:500. The
crosses carry no labels, so one seed is enough. The seed is a position on the sheet with a
coordinate known to within 50 m (a vertex-table point, a street corner read from the geoportal).
Every cross's coordinate is then the seed plus its measured distance, rounded to the grid. A seed
more than 50 m off moves *every* cross by 100 m and the fit cannot tell, so the seed's source goes
in the point notes. The staging's cadastral checks catch a gross shift (below). The displayed page
(its /Rotate) is taken as north-up.

## The fit

- `helmert` (4 parameters: scale, rotation, shift) for vector sheets plotted from CAD. `affine`
  (6 parameters) for scanned or redrawn sheets (paper stretch, shear).
- Least squares on centred coordinates. The report gives every residual (dx, dy, r), the RMSE
  overall and per sheet, the maximum residual, the fitted scale and rotation.
- **Rejected** above `georef.max_rmse_m` (default 0.5 m) or below `min_points`: nothing is stored.
- **Outliers**: a point the other points' fit misses by more than 3 × their RMSE, the threshold
  and 10 cm (leave-one-out). With few points, a bad point pulls the fit towards itself, so its own
  residual never stands out.
- A Helmert scale more than 1 % from 1 means the sheet's scale in the rules is wrong (the local
  frame is already in ground metres).
- The stored `<doc>.transform.json` holds the parameters, the per-sheet `page_to_crs`, the
  residuals and the SHA-256 of the enabled points. `apply` refuses a transform whose points
  changed since the fit. The same inputs and the same stored transform give the same features:
  the dataset's `output_sha256`, reproduced by a re-run. `show --transform-out` writes a dataset's
  transform back to a file for `apply --transform`.

## Snapping to the cadastral base

Planned parcels and blocks only, in the metric CRS (EPSG:25834, the cadastre profile's
`area_crs_epsg`).

- Each vertex within `georef.snap_tolerance_m` (0.5 m) of a served cadastral vertex moves onto it
  (`ST_Snap`). This removes the hairline gaps and overlaps where the plan follows existing
  boundaries.
- Vertices farther away stay: an intentional re-parcelling is the plan's content.
- Every feature carries `vertices`, `snapped_vertices`, `snapped_ratio` (QA attributes on the
  staged feature).
- `<doc>.snap-log.csv` lists every moved vertex and every **near miss** (a cadastral vertex between
  1 and 3 tolerances away, not moved) for review.
- **The overlay check.** The mean vector from the vertices near the cadastre (within 3 tolerances)
  to their cadastral vertex is the plan's systematic offset (`systematic_offset_m`, shown on the
  document). It is near 0 when the plan sits on the cadastre. It is warned as `systematic_offset`
  above half the tolerance, and as `no_common_vertices` when a sizeable plan has no vertex near the
  cadastre at all (offset beyond the snapping range).

## Validation

| Check | Result |
|---|---|
| every feature inside the municipality's extent (profile `bounds`) | error `outside_extent` |
| the planned parcels overlap the cadastral parcels under them (cadastral parcels present, zero overlap) | error `no_cadastral_overlap` |
| no cadastral parcels under the document (base not loaded there) | warning `no_cadastral_base` |
| planned parcels of the document overlapping each other by more than 1 m² | warning `parcel_overlaps` (pairs listed) |
| parcels without a number, numbers drawn twice | warnings `unnumbered_parcels`, `repeated_parcel_numbers` |
| systematic offset, no vertex near the cadastre | warnings `systematic_offset`, `no_common_vertices` |

An error records the dataset `invalid`, stages nothing, and the CLI exits 1.

## Staging and publishing

One `georef_datasets` row per run: document, `dataset_version` (`geo-<doc>-<yyyymmdd>-<n>`), source
(`extraction` | `manual_redraw`), CRS, method, the transform with every residual, `rmse_m`,
`max_residual_m`, `points_used`, the per-sheet RMSE, snapping, validation, the batches, the output
digest and the GeoPackage's bucket key (`--store`).

The batches follow the staged-geometry contract:

- `document_coverage`: the plan boundary.
- `urban_parcels`: key `<document id>|UP <n>`. The number takes the profile's term, like the seeds
  and the panel.
- `urban_blocks`: the plan's block label. A staged block updates the block of that label it
  overlaps, else it is new.
- `land_use` and `traffic_network`: generic layers. The newest batch replaces the layer at publish,
  so a document's batch carries the other documents' features forward.

Every feature carries `document_id` and `dataset_version`. A newer run of the document supersedes
its staged one. The publish job applies the batches and marks the dataset `published` (the
document's previous one `superseded`).

The admin console's document page shows the latest run: status, RMSE against the limit, the
maximum residual, a per-sheet table, snapping and the offset, the cadastral overlap share, and the
warnings.

## Scanned / redrawn sheets

A scanned sheet is redrawn in QGIS with the extraction's layers and columns (the GeoPackage the
extraction writes is the template). Draw it either over the rendered sheet (`extract run
--overlay`: PNG + world file in the local frame, apply with `--frame local`) or in the sheet's PDF
points (`--frame sheet:<id>`). Use `method: affine` for the paper's distortion, and `--redrawn` so
the dataset records `manual_redraw`. Everything else is the same.

## Status of the POC documents (2026-09-27)

The tooling, tests and admin display are done. The POC sheets are not georeferenced yet:

- Each sheet needs **one seed** within ±50 m for its grid crosses: Novi Grad parcel sheet, Stara
  Varoš parts 1 and 2. For Novi Grad, the vertex table on sheet 10 gives X = 6 603 501.68,
  Y = 4 700 163.54 for one vertex, but that vertex's position on the parcel sheet still has to be
  found.
- The Stara Varoš land-use sheet has no grid. It needs 4+ points from cadastral corners, or from
  features it shares with the parts 1 and 2 sheets.
- The cadastral base is not loaded yet (access not confirmed). Until it is, snapping and the
  overlap checks have nothing to compare with (warning `no_cadastral_base`), and the offset check
  cannot run.
