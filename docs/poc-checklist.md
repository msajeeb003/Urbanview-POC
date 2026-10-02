# POC checklist: the 42 funded rows

Checked on 2026-10-02 / 03 against `UrbanView_POC_Estimation_v2.xlsx` (220 h, 42 rows), at
commit `0a53738`. Each row was read in the code by an independent audit and, where a screen or a
job exists, run: on the live site (public map), or on a local copy with the sample data (the
flows that write: orders, review, publish, assumptions, users).

**Status words.** *Done* = built and it ran. *Done, no data on the live site* = built and it ran
with the sample data; the live site has nothing to show for it until the data named in the note
is supplied. *Partly evidenced* = built, but the row asks for a measurement that is not complete.

## Frontend (public map)

| # | Row | Status | Note |
|---|---|---|---|
| 1 | Layout, navigation, shared components | Done | Shell, layer cards, API client, anonymous session id |
| 2 | Mapbox map integration (S1) | Done | City extent, zones on by default, selection highlight, only covered areas drawn |
| 3 | Layer engine and layer cards | Done | Seven cards. The cadastral card says "no data yet" on the live site |
| 4 | Find a location (S2) | Done, no data on the live site (parcel number) | Address autocomplete and map click work live. Cadastral municipality + parcel number needs the cadastral base; planned parcels are found by number ("UP 40") |
| 5 | Zone panel (S3) | Done | Name, documents with status. The summary text is not written for any zone yet |
| 6 | Parcel panel Group 1 (S3) | Done | The 11 fields with source links. Cadastral vs planned area needs the cadastral base to show a comparison |
| 7 | Parcel panel Group 2 and ranges (S3) | Done, no data on the live site | Figures appear once market rates are entered for a zone. Saleable area is one figure, not a range (it has no market input) |
| 8 | Editable assumptions and live recalculation | Done, no data on the live site | The three sliders are disabled until the zone has market rates |
| 9 | Source document viewer | Done | Cited page opens with the value's cell framed |
| 10 | Order form (S4) and confirmation (S5) | Done | Guest form, reference, bank-transfer instructions on screen and by e-mail. The bank details on the server are placeholders |

## Admin

| # | Row | Status | Note |
|---|---|---|---|
| 11 | Admin shell and role-gated auth | Done | E-mailed sign-in links; admin, reviewer and expert each see only their sections |
| 12 | Documents and files (A1) | Done | Register under a zone, several files, per-file job status |
| 13 | Review queue (A2) | Done | Value beside its PDF page, approve / amend / reject, note, filters, publish blocked while anything is pending |
| 14 | Order and payment queue (A6) | Done | Payment received / not received / refunded, assign expert, report upload delivers and e-mails |
| 15 | Financial assumptions editor (A5) | Done | Dated versions, formula version shown |

## Backend

| # | Row | Status | Note |
|---|---|---|---|
| 16 | Project setup, config, rate limiting | Done | Per-IP limit for public traffic |
| 17 | Point-in-polygon and parcel-number lookup | Done | One query per lookup |
| 18 | Panel payload assembly | Done | |
| 19 | Formula engine package | Done | The fixtures are not yet validated by the client |
| 20 | Feasibility endpoint | Done | The map itself recalculates in the browser with the same engine |
| 21 | Geocoder proxy | Done | Public Photon service; answers in 1–3 s |
| 22 | Source page endpoint | Done | |
| 23 | Events endpoint and dashboard aggregates | Done | 13 events, funnel, districts, both interest buttons |
| 24 | Documents, files and job triggers | Done | |
| 25 | Review API and audit log | Done | Audit log cannot be edited or deleted |
| 26 | Assumptions and users | Done | |
| 27 | Order creation, payment status and emails | Done | |
| 28 | Queue infrastructure | Done | A job interrupted by a worker restart stays "running" until cleared by hand |
| 29 | Publish approved records | Done | One button. Earlier versions are kept; switching back is a manual step |
| 30 | Transactional email setup | Done | Mail account set on the server; the support address is a placeholder |

## AI extraction

| # | Row | Status | Note |
|---|---|---|---|
| 31 | Extraction schema and prompts | Done | |
| 32 | PDF text and table extraction | Done | Scanned pages are flagged, never read |
| 33 | Listings and Monstat normalisation | Done, no data on the live site | Import and its review run from the command line and the API; there is no screen for them. Rates can be typed on the Financial assumptions screen |
| 34 | Extraction worker | Done | |
| 35 | Prompt tuning against sample plans | Partly evidenced | Two plans measured: Novi Grad 100 % of its cells, Stara Varoš 100 % on pages 1–23 of 57; the rest was not read and no baseline is recorded. The two plans' live values were read from their tables without the model |

## GIS

| # | Row | Status | Note |
|---|---|---|---|
| 36 | Sample assessment: vector vs scanned PDFs | Done | |
| 37 | Vector PDF geometry extraction | Done | Run from the command line |
| 38 | Georeferencing and reprojection | Done | Run from the command line. The plans are drawn in EPSG:3908, not 25834 |
| 39 | Cadastral import | Done, no data on the live site | The loader refuses to run until a source's bulk access is confirmed; none is |
| 40 | Parcel link computation | Done, no data on the live site | Needs the cadastral base |
| 41 | Choropleth cells | Done | Coverage, FAR, floors and GFA have data live; sale price has none (no market rates) |

## Data preparation

| # | Row | Status | Note |
|---|---|---|---|
| 42 | Podgorica zone definition | Done | 11 zones live. Zone types are not set and the document lists are not confirmed by the client |

## What the live site still needs (data, not code)

- Market rates for the two covered zones (Novi Grad, Stara Varoš): until then Group 2, the
  sliders and the price heatmap are empty.
- A cadastral base from a confirmed source: until then no cadastral parcels, no parcel-number
  search, no cadastral vs planned comparison.
- Zone types and zone summaries.
- The client's bank details and support address.
- The client's validation of the formula fixtures, and approval of the disclaimer and the
  Montenegrin wording.
