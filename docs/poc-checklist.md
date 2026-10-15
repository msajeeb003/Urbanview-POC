# POC checklist: the 42 funded rows

The rows and hours are `UrbanView_POC_Estimation_v2.xlsx` (220 h, 42 rows, the plan the client
accepted). First checked on 2026-10-02 / 03 at commit `0a53738` by an independent audit of the
code with every screen and job run (on the live site for the public map, on a local copy with the
sample data for the flows that write). Re-marked on 2026-10-13 against the live site at data
version 16 and the code on `main`.

**Status words.** *Done* = built and it ran. *Done, no data on the live site* = built and it ran
with the sample data; the live site has nothing to show for it until the data named in the note
is supplied. *Partly evidenced* = built, but the row asks for a measurement that is not complete.

## Frontend (public map), 55 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 1 | Layout, navigation, shared components | 5 | Done | Shell, layer cards, API client, anonymous session id. Styled by the wireframe's own stylesheet instead of Tailwind (product owner, 2026-09-24: the map reproduces the wireframe exactly) |
| 2 | Mapbox map integration (S1) | 6 | Done | City extent, zones on by default, selection highlight, only covered areas drawn; since 2026-10-13 the plans' own buildings inside a plan, a block panel on click, tablet widths |
| 3 | Layer engine and layer cards | 6 | Done | Seven cards. The cadastral card says "no data yet" on the live site |
| 4 | Find a location (S2) | 6 | Done, no data on the live site (parcel number) | Address autocomplete with house numbers (own Photon geocoder since 2026-10-13) and map click work live. Cadastral municipality + parcel number needs the cadastral base; planned parcels are found by number ("UP 40") |
| 5 | Zone panel (S3) | 4 | Done | Name, documents with status. The summary text is not written for any zone yet |
| 6 | Parcel panel Group 1 (S3) | 6 | Done | The 11 fields are served with source links; the panel shows the BRD's rows (land use, height, IZ, II, area, GFA, coverage area). Cadastral vs planned area needs the cadastral base to show a comparison |
| 7 | Parcel panel Group 2 and ranges (S3) | 6 | Done, test data only on the live site | Figures appear once market rates are entered for a zone: Novi Grad shows a set marked "TEST dummy values", Stara Varoš has none. Saleable area is one figure, not a range (it has no market input). Both intent buttons record their event |
| 8 | Editable assumptions and live recalculation | 4 | Done, test data only on the live site | The three sliders work on Novi Grad's test set and say why they are disabled on Stara Varoš |
| 9 | Source document viewer | 5 | Done | Cited page opens with the value's cell framed |
| 10 | Order form (S4) and confirmation (S5) | 7 | Done | Guest form, reference, bank-transfer instructions on screen and by e-mail. The client's real bank details are on the server since 2026-10-05, in two sets ("Paying from"); payment-received and refund e-mails since 2026-10-09 |

## Admin, 34 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 11 | Admin shell and role-gated auth | 6 | Done | E-mailed sign-in links (live since 2026-10-02); admin, reviewer and expert each see only their sections |
| 12 | Documents and files (A1) | 6 | Done | Register under a zone, several files per version, per-file job status, scanned pages flagged for redraw |
| 13 | Review queue (A2) | 10 | Done | Value beside its PDF page, approve / amend / reject, note, filters, publish blocked while anything is pending; a geometry review for staged shapes |
| 14 | Order and payment queue (A6) | 8 | Done | Payment received / not received / refunded (a refund above the amount received is refused), assign expert, report upload delivers and e-mails |
| 15 | Financial assumptions editor (A5) | 4 | Done | Dated versions per district, formula version shown |

## Backend, 62 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 16 | Project setup, config, rate limiting | 4 | Done | Per-IP limit for public traffic; the site's own calls are not counted |
| 17 | Point-in-polygon and parcel-number lookup | 6 | Done | One query per lookup |
| 18 | Panel payload assembly | 6 | Done | |
| 19 | Formula engine package | 6 | Done, awaiting validation | Fixtures written first; the client has not validated them (P0 gate 3); every panel says so |
| 20 | Feasibility endpoint | 2 | Done | The map itself recalculates in the browser with the same engine |
| 21 | Geocoder proxy | 2 | Done | Own Photon service on the server since 2026-10-13 (50–200 ms); the public instance had timed out |
| 22 | Source page endpoint | 1 | Done | |
| 23 | Events endpoint and dashboard aggregates | 4 | Done | 13 events, funnel, districts, both interest buttons |
| 24 | Documents, files and job triggers | 6 | Done | |
| 25 | Review API and audit log | 4 | Done | Audit log cannot be edited or deleted |
| 26 | Assumptions and users | 3 | Done | |
| 27 | Order creation, payment status and emails | 7 | Done | Four order e-mails in the order's language |
| 28 | Queue infrastructure | 4 | Done | A job interrupted by a worker restart stays "running" until cleared by hand |
| 29 | Publish approved records | 5 | Done | One button; 16 versions published so far. Earlier versions are kept; switching back is a manual step |
| 30 | Transactional email setup | 2 | Done | Mail account set on the server (2026-10-02); the support address `support@urbanview.io` was set on 2026-10-15 |

## AI extraction, 29 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 31 | Extraction schema and prompts | 6 | Done | |
| 32 | PDF text and table extraction | 6 | Done | Scanned pages are flagged, never read |
| 33 | Listings and Monstat normalisation | 5 | Done, no data on the live site | Import and its review run from the command line and the API; there is no screen for them. Rates can be typed on the Financial assumptions screen |
| 34 | Extraction worker | 6 | Done | |
| 35 | Prompt tuning against sample plans | 6 | Partly evidenced | Two plans measured: Novi Grad 100 % of its cells, Stara Varoš 100 % on pages 1–23 of 57; the rest was not read and no baseline is recorded. The two plans' live values were read from their tables without the model (product owner, 2026-10-01) |

## GIS, 36 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 36 | Sample assessment: vector vs scanned PDFs | 6 | Done | |
| 37 | Vector PDF geometry extraction | 11 | Done | Run from the command line; parcels, blocks, land use and, since 2026-10-13, existing buildings |
| 38 | Georeferencing and reprojection | 6 | Done | Run from the command line. The plans are drawn in EPSG:3908, not 25834; snapping to the cadastral base has nothing to snap to yet, so both plans were aligned to OpenStreetMap's buildings (2026-10-13) |
| 39 | Cadastral import | 6 | Done, no data on the live site | The loader refuses to run until a source's bulk access is confirmed; none is. The UZN service behind the geoportal answers 401 without credentials |
| 40 | Parcel link computation | 3 | Done, no data on the live site | Needs the cadastral base |
| 41 | Choropleth cells | 4 | Done | Coverage, FAR, floors and GFA have data live; sale price has Novi Grad's test values only |

## Data preparation, 4 h

| # | Row | h | Status | Note |
|---|---|---|---|---|
| 42 | Podgorica zone definition | 4 | Done | 11 zones live. Zone types are not set and the document lists are not confirmed by the client |

## Totals

42 rows, 220 h. Done: 42 rows have code behind them and ran. Fully evidenced on the live site
with real data: 32. Built but waiting for data: 7 (rows 4, 7, 8, 33, 39, 40 and the sale-price
part of 41). Awaiting the client's sign-off: 1 (row 19). Partly evidenced: 1 (row 35). Pending
inputs: 1 (row 42, zone types and summaries).

## What the live site still needs (data, not code)

- Market rates per district (Novi Grad's test set replaced, Stara Varoš's entered): until then
  Group 2, the sliders and the price heatmap show test values or nothing.
- A cadastral base from a confirmed source: until then no cadastral parcels, no parcel-number
  search, no cadastral vs planned comparison.
- Zone types and zone summaries.
- The client's validation of the formula fixtures, and approval of the disclaimer and the
  Montenegrin wording.
