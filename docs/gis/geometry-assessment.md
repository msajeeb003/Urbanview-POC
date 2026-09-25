# Geometry assessment — week-1 gate (P0 gate 1)

25 September 2026 · Artech Digital for Monmaks d.o.o. · build plan P0 gate 1, BRD "Geometry
extraction: to be determined by sample assessment"

**Inputs:** the client's GIS folder ("UrbanView – Initial GIS data preparation"): two plans the
client picked for the prototype, *DUP Novi grad 1 i 2 – izmjene i dopune* and *UP Stara Varoš –
izmjene i dopune* (5 plan sheets and 2 parameter tables), and 4 maps of the PUP atlas for the
city-wide boundaries. No DWG / DXF / SHP / GeoPackage files were supplied. Files and checksums:
`docs/gis/source/`, `docs/gis/assessment/report.md` §7.

**Method:** `python -m core.gis.assess` (backend `core/gis`, `make gis-assess`) opens every
page with pymupdf, measures the vector drawing per PDF layer, samples the geometry (can each
layer's linework be closed into the parcels, blocks and areas the plan shows?), checks the stated
scale against the drawing, reads the coordinate evidence and classifies every sheet and layer.
GDAL `ogrinfo` handles GIS / CAD files. The evidence (tables, CSVs, layer-isolation images) is
`docs/gis/assessment/`. A new document is a catalog entry and a rerun, so every plan that enters
coverage gets the same check.

## Result: go for vector extraction

**All 9 graphical sheets are vector with identifiable layers (class A).** They are AutoCAD 2012 /
Map 3D 2013 plots that kept their CAD layers as PDF layers: 74–122 layers in use per plan sheet,
8–14 per PUP map, 86–100 % of the drawing on a named layer (the unlayered 14 % on Novi Grad's
sheet 10 is its vertex coordinate table). None is a scanned raster; the PUP maps carry a raster
topographic base (143–200 dpi, 25–90 % of the sheet) under vector boundaries.

**Recommended split for the POC documents: extraction 100 %, manual redraw 0 %.** The surveying /
GIS-engineer redraw of BRD §2.6 is not needed for these documents, and the manual QGIS redraw
workflow that the POC exclusions parked until this assessment can stay out.

| Document (eRegistri) | Sheets | Urban parcels | Urban blocks | Land use | Plan boundary | Georeferencing | Ticket 07 | Native files |
|---|---|---|---|---|---|---|---|---|
| UP Stara Varoš – izmjene i dopune (#4207, adopted 2012, 24.3 ha) | 10a + 10b 1:500, 09 1:1000 — A A A | **A** 370 / 387 and 350 / 367 parcels close; ids as text | **A** union of parcels by zone letter | **A** 13 category layers, solid fills | **A** | 100 m grid crosses | **GO** | not needed |
| DUP Novi grad 1 i 2 – izmjene i dopune (#4182, adopted 2012, 46.8 ha) | 10, 09 at 1:1000 — A A | **B** the parcel layer closes 35 of 91; the rest follow the cadastral base; numbers drawn as glyphs | **B** union of parcels by the table's Blok column | **A** 7 hatch layers, codes as text | **A** dotted, closes | 100 m grid crosses + 330-vertex table | **GO with conditions** | recommended |
| PUP: administrative division (KO), planning division (zones), 1:50 000 (#3163, 2014) | A, A | — | — | — | zones **A** | labelled grid, 1:50 000 exact | **GO** | not needed |
| PUP: DUP / UP boundaries, option B, 1:10 000 (option A as a check) | A | — | — | — | document coverage **A**: 136 faces for 113 DUP numbers (text) | labelled grid, 1:10 000 exact | **GO** | not needed |

Per layer type, over the layers the POC builds: 9 of 11 document × layer units are automatic
(A), 2 semi-automatic (B: Novi Grad's parcels and blocks), none needs a redraw (C). The planned
traffic network is vector and layered in both plans but is an MVP layer (pilot technical scope
§1.4, not among the POC's 7 map layers), so it is assessed and deferred.

**Conditions on Novi Grad.** (1) Parcels: polygonize the parcel layer together with the
cadastral base and merge the faces per parcel number; the numbers are vector glyphs, so OCR the
isolated `!BROJEVI UP` layer and check the result against the 91 parcels of the parameter table.
The planner's DWG removes both steps. (2) Blocks: the union of the parcels by the table's Blok
column (A–F); the plan's dotted block dividers stop short of the plan boundary.

**Re-estimate of tickets 07 and 08** (effort model and per-class rates:
`docs/gis/assessment/report.md` §6; "build" is what remains after the code this assessment
already wrote: layer matching, closing dotted lines, ribbons and fills, grid detection):

| Item | Plan (estimate v2) | Assessed | Change |
|---|---|---|---|
| 07 Vector PDF geometry extraction | 11 h | 21.3 h = build 10.2 h + apply 11.1 h (Stara Varoš 3.3 h, Novi Grad 7.8 h) | +10.3 h |
| 07 if the Novi Grad DWG arrives | 11 h | ≈ 12.5 h (no OCR, no cadastral merge, parcels class A) | ≈ +1.5 h |
| 08 Georeferencing and reprojection | 6 h | 8.4 h = build 3.2 h + datum shift 2 h + apply 3.2 h | +2.4 h |
| Zone definition (QGIS with the client) | 4 h | 1.8 h of it goes to extracting zones, KO and plan boundaries from the PUP maps | within |
| Manual redraw (class C) | — | 0 h | — |
| Deferred: traffic network, GUP and state / municipal borders | — | 5.5 h, outside the POC | — |

Estimated effort per class, per document × layer type (apply): **A 0.5 h, B 3 h, C 8 h** (C = a
30–50 ha plan redrawn over its georeferenced scan), plus 0.25–1.5 h per extra step.

## Georeferencing evidence (ticket 08)

- **Coordinate system: MGI 1901 / Balkans zone 6 (EPSG:3908), the state Gauss-Krüger system,
  not ETRS89 / UTM 34N (EPSG:25834) as the profile assumed.** The PUP maps carry labelled grids
  (6 580 000 – 6 635 000 E, 4 675 000 – 4 730 000 N; 48–64 labels per map fit the page at exactly
  1:50 000 or 1:10 000), and the Novi Grad sheet lists its parcel vertices as
  X = 6 603 501.68, Y = 4 700 163.54 (the easting written as X). The profile now says
  `source_crs_epsg = 3908`.
- **Control points are on every plan sheet.** Each carries a lattice of grid crosses 100 m
  apart (Novi Grad 68 crosses, Stara Varoš 28 + 19). One approximate position snaps every cross
  to its round coordinates, so the affine fit needs no hand-picked points. Novi Grad adds a table
  of about 330 numbered parcel vertices with coordinates; the cadastral base (UZN parcels and
  buildings) under every plan serves as the independent check.
- **Scale is proved, never read from the PDF alone.** The Novi Grad PDF's own viewport measure
  says 1:776; its grid crosses are 100 mm apart and its plan boundary encloses 47.0 ha against
  46.8 ha in the registry, so the sheet is at its stated 1:1000. Measuring with the embedded
  factor would shrink every area by 40 %.
- **Datum shift is the real work in ticket 08.** EPSG's only MGI 1901 → WGS 84 transformation for
  Montenegro (EPSG:3965) is good to about 10 m, too coarse for parcels 10–30 m wide. The shift
  has to be fitted on common points (UZN cadastral data in both systems) or taken from UZN's
  official parameters.

## Coverage plan

Coverage is defined by data: a place is covered when an adopted document with published values
governs it. The POC ingests a subset of plans to prove the pipeline (POC exclusions, "Full
Podgorica ingestion"), in this order:

1. **UP Stara Varoš – izmjene i dopune** (PUP planning unit 3 Stara Varoš–Zabjelo): every layer
   automatic, about 3.5 h extraction and 1 h georeferencing. Goes live once the zone B question
   (risk 2) is answered.
2. **DUP Novi grad 1 i 2 – izmjene i dopune** (west of the Morača, unit to confirm at gate 2):
   semi-automatic parcels, about 8 h, or less with the DWG.
3. **City-wide layers from the PUP maps:** planning-division zones (23 planning units in 5
   planning areas) as the starting point for gate 2 (zones are drawn in QGIS with the client), KO
   polygons from the administrative division, and the boundary of every DUP / UP from option B.
   Those boundaries load as documents with coverage switched off (`coverage_live = false`) until
   their values are extracted, so the map shows where plans exist without treating them as
   covered.

**Out of POC coverage:** every other Podgorica plan (not supplied); *UP Stara Varoš – blok 7*
(#9699, adopted 2020, not supplied); *UP Stara Varoš – dio zone A* (in preparation); *UP Stara
Varoš – dio zone B* (#4210) until the client confirms its status; *DUP Stambena zajednica VI and
VII – Stara Varoš* (#4535, #4547; other areas, not supplied).

## Native GIS / CAD files to request

| From | What | Why | Status |
|---|---|---|---|
| The planner of DUP Novi grad 1 i 2 (Arhient d.o.o.) via the client or Agencija za izgradnju i razvoj Podgorice | DWG / DXF of sheet 10 *Plan parcelacije*, or at least the vertex coordinate table as a spreadsheet | Parcel polygons, parcel numbers as text and the vertex coordinates as data: removes the semi-automatic parcel step and the OCR | recommended, not blocking |
| Uprava za nekretnine (UZN) | MGI 1901 ↔ MONREF97 transformation parameters, or the cadastral parcels of the covered KOs in both systems | Sub-metre datum shift (EPSG:3965 is 10 m) | needed for ticket 08 |
| Nobody else | The Stara Varoš sheets and the PUP maps extract as they are | — | not needed |

## Risks and questions for the client

1. **Two plans are a sample.** Both came from AutoCAD with their layers intact; other planners'
   or older PDFs may be flattened or scanned. Every document entering coverage gets its own run
   of the assessment (minutes) and its own go / no-go before it is planned.
2. **Stara Varoš has neighbours with their own plans.** *UP Stara Varoš – dio zone B*
   (eRegistri #4210, 2011, 0.29 ha) predates this 2012 amendment and is still listed; location
   resolution lets the smallest adopted document win, so the client should confirm whether it
   still governs its 0.29 ha. *Stara Varoš – blok 7* (#9699, 2020) borders it to the east
   (adopted, not supplied) and *dio zone A* (decision on drafting 96/23) to the south is in
   preparation, so outside coverage.
3. **Which PUP planning unit Novi Grad sits in.** Its coordinates fall west of the Morača
   between units *1 Nova Varoš* and *3 Stara Varoš–Zabjelo*; the unit's label is not printed on
   the planning-division sheet. To settle at gate 2 (zone definition).
4. **Base maps date from the 2014 PUP.** The 2025 amendment (#11738, 150.8 ha) may move
   boundaries inside its area; check before zones are published.
5. **Text on the plan sheets is partly in AutoCAD's glyph-id encoding** (letters shifted by 29,
   Č / Ć / Š / Ž / Đ remapped). The tool decodes it; the AI extraction must do the same on sheet
   text. The parameter tables are clean Word text.
