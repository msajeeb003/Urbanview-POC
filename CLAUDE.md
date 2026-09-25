# UrbanView — working notes for Claude

UrbanView (POC working name "Arvin View") is a public web map of Podgorica where anyone finds a
parcel (address, map click or cadastral number) and sees what the adopted planning documents allow
on it: zone, planning parameters with source links, and a low / expected / high financial
feasibility range, then orders an expert report. Behind it: an admin tool where staff upload
planning PDFs, AI extracts parameters, a reviewer checks 100% of items against the cited PDF page,
and one button publishes approved data to the map.

Podgorica-only prototype, but the ingestion/serving design must extend to other municipalities as
a **data + configuration exercise, not a rebuild** (BRD §8). Source documents: `docs/` (BRD v1.6
PDF + extracted text, BRQ, pilot technical scope PDF + extracted text + diagrams, POC exclusions,
wireframe, brand SVGs, specs; the client's planning PDFs in `docs/gis/source/`).

## Repository layout (frontend / backend / database kept separate)

| Folder | Contents |
|---|---|
| `backend/` | FastAPI app (`api/`: app factory, routers under `/v1`, schemas, services), `core/` (settings, logging, errors, middleware, db, models, `engine/` feasibility formulas, `geocode/` geocoding providers, `gis/` geometry assessment, redis, storage, mail, municipality profiles, seeds loader), `jobs/` (Celery: ingestion, extraction, publish), `municipalities/<id>.toml`, `tests/` (unit) and `tests/integration/` (PostGIS). Python venv: `backend/.venv`. |
| `database/` | Alembic (`alembic.ini`, `migrations/`), seed datasets (`seeds/podgorica_sample/*.geojson` for the geometry tables, `*.json` for the panel tables), compose init SQL (`docker/initdb/`), `scripts/dev_postgis.py` (portable PostGIS for Docker-less machines). |
| `frontend/` | Public map: Next.js 16 (App Router) + TypeScript + Tailwind v4 + shadcn/ui (Radix) + Mapbox GL JS + TanStack Query, npm workspace `@urbanview/frontend`. Its own `frontend/CLAUDE.md` holds the tokens, dimensions, layer list, panel field lists and frontend rules. |
| `admin/` | Staff tool, Next.js + Auth.js, roles admin / reviewer / expert (reserved, P1). |
| `packages/` | `feasibility-engine/`: the shared TypeScript feasibility engine (npm workspace of the root `package.json`) and `fixtures/feasibility-cases.json`, the fixture file both engines are held to. |
| `deploy/` | Production on one server (Hetzner Cloud): `compose.yml` (Caddy HTTPS → web / api / MinIO; worker, PostGIS, Redis internal), `Caddyfile`, `.env.example`, `server-setup.sh`, `deploy.sh` (pull + rebuild + migrate), `backup.sh`, `README.md` (step by step). |
| root | `docker-compose.yml`, `Makefile`, `ruff.toml`, `package.json` (npm workspaces: `packages/*`, `frontend`), `README.md`, this file. |

New code goes in the folder of its tier. Python runs from `backend/` (that is where `.env` is read);
migrations are invoked with `alembic -c ../database/alembic.ini …`.

## Product rules (client-owned facts — apply them, do not reinterpret)

**Identification fields on selection:** parcel number, sub-number, cadastral municipality
(KO — **mandatory**, the same parcel number recurs across KOs), street address, governing
planning document, urban block reference. **Parcel ID** is UrbanView's own numeric identifier
(`cadastral_parcels.id`), separate from those attributes (never composed from them; not a search
input).

**Cadastral vs planned:** cadastral parcels and planned urban parcels are stored, served and
labelled **separately, never merged** (`cadastral_parcels` vs `urban_parcels`). All calculations
use the **planned urban parcel area**; cadastral area only as fallback; any mismatch between the
two is **always surfaced** (`area_comparison` in the locate payload, present whenever both exist).

**Zones and documents:** a "zone" is UrbanView's internal city division (~city quarter) grouping
several planning documents (in Montenegro zones are not official bounded areas). Document status
is `adopted` / `in_progress` / `superseded`. **Only adopted documents are covered.**

**Panel Group 1 (planning, free):** Parcel ID, planning document name, land use, urban block,
urban parcel number, urban parcel area, max height/floors, max site coverage % (IZ), max FAR (II),
calculated max GFA. **Every value carries a source document reference** (document + page).

**Panel Group 2 (market, paid subscription):** estimated land value, design & documentation
costs, construction costs, market value, saleable area, potential profit, ROI. **All as
low / expected / high ranges**, never single figures.

**Formulas (client-owned, deterministic):**
- Max GFA = FAR × plot area
- Max coverage area = site coverage % × plot area
- Potential profit = (Max GFA × 0.70 × market value per m²) − (land value + design & documentation costs + construction costs)
- ROI % = potential profit / (land value + design & documentation costs + construction costs) × 100
- 70% saleable share is a **visible, user-editable assumption**; users may also edit construction
  cost and selling price. Edits recalculate the ranges live; the formulas never change.
- Client validates the formula fixtures before any UI is built on them (P0 gate 3). The fixtures
  live in `packages/feasibility-engine/fixtures` next to the shared TypeScript engine; the Python
  copy `backend/core/engine/shared.py` must reproduce the same fixtures exactly.

**Market data sources:** Realitica, Estitor listings + Monstat statistics.
**Planning sources:** eRegistri (lamp.gov.me), eKatastar, eMapa, Geoportal UZN. URLs in
`backend/municipalities/podgorica.toml`.

**AI and review:** 100% of AI-extracted planning values are expert-reviewed before publishing;
extracted values start as `pending_review` and reach the map only via the publish job.
**AI never generates financial arithmetic.**

**Uncovered locations are not errors (BRD S6, UX rule "No error state for uncovered areas"):**
any endpoint that resolves a location outside coverage returns **200** with `covered: false`,
`zone: null`, `planning_document: null`, the base coordinates echoed in `query`, and a neutral
message. Never 404/500, never an error envelope. A parcel reference that matches nothing is also a
200 (`coverage.reason = parcel_not_found`). Malformed input (lat 95, missing KO) is still a 422.

**Analytics events:** `map_loaded`, `search_performed`, `parcel_selected`, `layer_toggled`,
`panel_viewed`, `financials_viewed`, `source_reference_opened`, `order_started`,
`checkout_completed`, `return_visit`, `sessions_per_user`, `market_data_interest`, `ai_interest`
(enum in `backend/api/schemas/analytics.py`).

**Performance target:** under 2 seconds from query to populated panel. Requests slower than
`SLOW_REQUEST_MS` are logged at WARNING with the route template.

## Location resolution (`backend/api/services/locate_sql.py`, `resolver.py`)

- `GET /v1/locate?lat=&lng=` and `GET /v1/locate/parcel?ko=&number=&sub=` run **one PostGIS
  statement** (CTEs, single round trip) and return the same `LocationResolution` payload:
  cadastral parcel (with geometry + centroid), primary planned urban parcel and all matching
  ones, urban block, zone (with its document list), governing document, `calculation_basis`,
  `area_comparison`, `centroid`.
- Governing document = the **adopted** document whose coverage contains the point; when several
  do (a DUP inside a PUP) the **most specific** (smallest coverage) wins.
- Planned urban parcels = those containing the point, plus those overlapping the cadastral parcel
  by ≥ `LOCATE_MIN_OVERLAP_M2` (1 m²) and ≥ `LOCATE_MIN_OVERLAP_FRACTION` (2%) of its area, so the
  correspondence is shown even when the click lands in land the plan takes for roads, and
  digitising slivers are ignored. Order: point match, governing document, largest overlap.
- Parcel lookup: `ko` is required and case-insensitive; `number` may be `1042/3`; the reference
  point is `ST_PointOnSurface` of the parcel; `centroid` is where the map pans.
- Points outside the municipality bounds (profile) short-circuit to `outside_municipality`.
- Every access path is index-backed; `tests/integration` proves it with `EXPLAIN` under
  `enable_seqscan = off`, checks one statement per call, and measures latency.

## Information panel (`backend/api/services/panel.py`, `panel_sql.py`, `panel_text.py`)

- `GET /v1/panel?type=zone|document|cadastral|urban&id=<int>` returns one payload per type
  (`api/schemas/panel.py`, discriminated by `type`; the field-by-field contract is
  `docs/specs/panel-payload.md`, the reference for the frontend tickets). Optional overrides
  `saleable_share` ((0, 1]), `construction_cost_eur_m2` and `sale_price_eur_m2` ((0, 100 000])
  apply to every panel with a feasibility block (`urban`, `cadastral`).
- Errors: unknown `type`, missing `id` or an out-of-range override → 422 `validation_error`;
  unknown `id` for the type → **404** `not_found` with details `{type, id}` (a missing *entity*
  looked up by primary key is an error, unlike an uncovered *location*); without PostGIS
  (`LOCATION_RESOLVER=nodata`) → 503 `service_unavailable`. A parcel with no adopted governing
  document is still 200 with `covered: false`, null planning / market / assumptions / feasibility
  and a neutral `coverage_note_en` / `_me`.
- **Serving vs staging.** The panel reads the serving tables only: `planning_parameter_values`
  (approved, published values; every row cites `source_page` of its document and a
  `publish_version_id`), `financial_assumptions`, `publish_versions`. The review queue
  `planning_parameter_extractions` (`review_state` pending_review / approved / rejected /
  amended) is never referenced by the public API: pending or rejected values are simply absent.
  Only the publish job (`jobs.tasks.publish.publish_approved`, see "Publish pipeline") copies
  approved rows across, one complete serving set per `publish_versions` row; the panel reads
  the rows of the **current** version only (`_values` filters on `publish_version_id`), so a
  rollback is a pointer flip and with no current version nothing is served. The sample's
  serving rows are version 1 from the seed loader.
- One PostGIS statement per panel type (`panel_sql.PANEL_SQL`, CTEs + `jsonb_build_object`,
  locate's style), nothing cached between requests. Governing document, cadastral ↔ planned
  parcel links and the overlap thresholds are locate's (`LOCATE_MIN_OVERLAP_*`, handed to
  `PanelService` in `api/app.py`), so both endpoints share `calculation_basis` (`urban` |
  `cadastral`) and agree on which planned parcel corresponds to a cadastral parcel. A cadastral
  panel with an urban basis embeds the **primary** link's planning and feasibility (largest
  overlap, then smallest planned area, then lowest id); the other links sit in `urban_parcels`
  (`split: true`).
- **Field dictionary** `planning_fields` (product-wide, no `municipality_id`, seeded by migration
  0003, never by the seed loader): 13 rows in `sort_order`, the 11 Group 1 fields (`land_use`,
  `max_site_coverage_pct` IZ %, `max_far` II, `max_height_m`, `max_floors`, `building_line_m`,
  `setback_neighbours_m`, `parking_requirement`, `min_green_area_pct`, `planned_parcel_area_m2`,
  `utilities`) then the computed `max_gfa_m2` (BGP) and `max_coverage_area_m2`, which are never
  stored. Columns `label_en` / `label_me` (Montenegrin labels provisional until the client
  confirms), `abbreviation`, `unit` (a value row's `unit` overrides it), `value_type` text |
  number. All 13 are always present in `planning.fields`: a value resolves parcel-level
  (`scope: parcel`) → document-level row of the parcel's document (`scope: document`,
  `fallback: true`) → `not_stated`; a cadastral-basis panel uses the governing document's
  document-level rows (`fallback: false`). A stated 0 is a real 0. Every stated value carries
  `source` (document id + name, `page`, `bbox` in PDF points with origin bottom-left, `note`,
  `registry_url`, `value_id` and `viewer_url = /v1/source/value/{value_id}`, the one-click way to
  the cited page: see "Source viewer").
- **Data version.** `data_version` = `label` of the `publish_versions` row with `is_current` (at
  most one per municipality), `data_version_date` = its `published_at` as a UTC date; with no
  current row `"unpublished"` / null and no planning values (they belong to a version). Values
  may also carry a `block_id` or `zone_id` scope (migration 0011; the panel resolves parcel →
  document only, the block / zone scopes feed the heatmap cells). Market inputs have
  their own version history (`financial_assumptions.version`, one `is_current` row per zone):
  the panel shows their `source`, `source_date`, `effective_from` and `market_inputs.version`
  (`{id, version, zone_id, effective_from}`; also `assumptions.market_version`, so a panel or
  a feasibility answer states which assumptions version produced its figures) plus
  `market_inputs.ranges` (per rate `{expected, low, high, kind}`: `absolute` when the row has
  admin bounds for that rate, else `multiplier` = expected × range factors).
- **Engine: one shared implementation.** The TypeScript package `packages/feasibility-engine`
  (`calculate` / `recalculate`, `FORMULA_VERSION = "poc-1"`, `ENGINE_VERSION`) and its Python
  copy `core/engine/shared.py` are both held to
  `packages/feasibility-engine/fixtures/feasibility-cases.json` with exact equality and
  byte-identical JSON (`tests/test_feasibility_shared.py`; `npm run check -w
  @urbanview/feasibility-engine`). Pure and deterministic, no municipality knowledge, no label
  text: numbers in, numbers and reason codes out (`area_unknown`, `far_not_stated`,
  `coverage_not_stated`, `requires_gfa`, `no_market_data` {zone_name},
  `no_market_data_zone_unknown`, `total_cost_zero`); `panel_text.py` owns every en / me string.
  Engine keys: `max_gfa`, `max_coverage_area`, `saleable_area`, `construction_costs`,
  `land_value`, `design_and_documentation_costs`, `total_cost`, `market_value`,
  `potential_profit`, `roi_pct`. `core/engine/feasibility.py` is the panel-facing adapter: it
  feeds the zone's market row as per-m² inputs with multiplier bounds (`range_low_factor` /
  `range_high_factor`) and maps the result to the panel's 7 fields (`max_gfa_m2`,
  `max_coverage_area_m2`, `saleable_area_m2`, `construction_cost_eur`, `revenue_eur`,
  `profit_eur`, `roi_pct`) + 4 cost rows (`land_value_eur`, `design_documentation_eur`,
  `construction_cost_eur`, `total_cost_eur`); it adds no formula. Ranges
  (`pessimistic-pairing-v1`): every money input carries admin-supplied bounds (multiplier or
  absolute, nothing hardcoded); cost rows at their own bounds, revenue at the price bounds,
  profit low = revenue low − cost high, ROI low = profit low / cost high; the three areas are
  `deterministic`; always low ≤ expected ≤ high. Rounding on outputs only: 2 decimals for areas,
  euros and ROI, half away from zero on the shortest decimal representation (identical in both
  languages). The fixtures are the contract (`client_validated: false`: P0 gate 3 is still open;
  never regenerate them from an engine; change formulas only with a new `FORMULA_VERSION` and
  updated fixtures on both sides). Every payload says `formula_version: "poc-1"`,
  `client_validated: false`. Both areas can be inputs: `selectCalculationBasis` /
  `select_calculation_basis` apply the planned-first rule, and `planned_area`,
  `cadastral_area`, `max_height_m`, `max_floors`, `land_use` travel in `planning` as validated
  context. Drift guards: `fixtures/assumption-dependencies.json` (which figures one edited
  assumption may change, both engines), `tests/test_feasibility_cross_engine.py` (680
  generated inputs through the built TS bundle and the Python copy, byte-identical JSON), and
  `frontend/src/lib/feasibility.test.ts` (the fixtures through the package as the map imports
  it). The frontend depends on the workspace package; the root `prepare` script builds it.
- **Engine inputs on the panel.** Cadastral and urban payloads of `GET /v1/panel` carry `engine`
  (null without feasibility): `engine_version`, `formula_version`, `range_derivation`,
  `deterministic`, the exact `inputs` the shared engine ran on, `edit_keys` (assumption →
  engine edit key, `core.engine.feasibility.EDIT_KEYS`) and `field_keys` (figure → engine result
  key, `SHARED_KEY`). The public map's sandbox runs `recalculate(inputs, edits)` in the browser;
  `tests/integration/test_feasibility_route.py` proves the built TS bundle on those inputs equals
  the payload without edits and `POST /v1/feasibility` with them.
- **`POST /v1/feasibility`** (`api/routers/v1/feasibility.py`, `api/services/feasibility.py`):
  server-side recalculation from edited assumptions (`construction_cost_per_m2`,
  `selling_price_per_m2`, `saleable_share` in (0, 1]). It calls the panel service with the edits
  as overrides, so it reads the same serving tables and runs the same engine as `GET /v1/panel`
  (field-for-field equality is tested); the response carries the panel's feasibility and
  assumptions blocks, `planning_inputs`, `engine` (`engine_version`, `formula_version`,
  `range_derivation`, `deterministic: true`), `data_version` and the disclaimer. Uncovered parcel
  → 200 with `covered: false`; unknown parcel → 404; no AI on this path.
- **Assumptions and overrides.** Precedence: query override > current market row
  (`financial_assumptions`: the parcel's zone, else the municipality-wide `zone_id IS NULL` row)
  > the product constant 0.70 for `saleable_share` (engine default; no per-zone saleable share).
  Overrides never rescue a missing market row: land and design rates would be unknown, so the
  money figures are `cannot_calculate` (`no_market_data` / `no_market_data_zone_unknown`) while
  GFA, coverage and saleable area still compute. `assumptions` echoes the numbers actually used,
  `overrides` (query param present) and `sources` (`market` | `user` | null).
- **Zone and document panels** (what the public map's S3 variants show): the zone panel lists
  the zone's **current** document versions, each with `covered` (adopted, live, current, with
  coverage: the rule of locate and the tiles), `file_available` (PDF stored) and `parcel_count`
  (cadastral parcels whose point on surface is in the coverage; null when not covered); `zone`
  carries `zone_type`, `counts.covered` the covered documents. The document panel's `document`
  carries `file_available`; its `zones` carry `zone_type` and `typical` (land use, FAR,
  coverage, height, floors of the zone's current parameter set, or null). Every `DocumentRef`
  carries `adopted_on` (migration 0016, nullable, entered at registration: `DocumentIn.adopted_on`,
  not in the future; the sample has none). The profile's `terminology.document_types_en`
  gives the English type names the map shows (`DUP — Detailed urban plan`).
- **Amendments** are linked only by `planning_documents.amends_document_id` (set at ingestion,
  never by coverage intersection): the header lists the governing document, then its
  `in_progress` amendments; the document panel lists them as `amendments_in_progress`.
- **Disclaimer** (`feasibility.disclaimer_en` / `_me`, `panel_text.DISCLAIMER`):
  `disclaimer_status = "placeholder"` until the lawyer signs the wording off
  (`client_approved`), `disclaimer_version = "poc-1"`.
- **Tier markers.** `planning.tier = "free"`; `market_inputs`, `assumptions` and `feasibility`
  carry `tier = "paid"`. The POC serves the paid blocks without entitlement checks; the marker
  is the boundary a later gate uses.
- Numbers are raw JSON numbers, never formatted strings (`_pct` 0–100, `_share` 0–1, areas
  1 decimal, euros whole); the client formats per language. Code: router
  `api/routers/v1/panel.py`, dependency `api/deps.py` (`PanelServiceDep`), models
  `core/models/panel.py`, migration `0003_panel_schema`, seeds
  `database/seeds/podgorica_sample/*.json` (loader `core/seeds.py`, `TABLES` in FK order).

## Display-shaped parcel and zone panels (`api/services/parcel_panel.py`, `parcel_panel_sql.py`, `panel_cache.py`)

- `GET /v1/parcels/{cadastral_parcel_id}/panel` answers everything the panel shows for a parcel
  in one response, in display order, with bilingual labels (field dictionary + `panel_text`, the
  frontend hard-codes none; contract `docs/specs/panel-payload.md` section 13). One PostGIS
  statement (`PARCEL_PANEL_SQL`), pure builders (`build_parcel_panel`), the shared engine.
  `GET /v1/panel` stays as it was (live links, all four panel types).
- **Header:** KO, parcel number, title, address, zone, block, documents with status and role
  (`governing`, `basis` when the planned parcel's document differs, `amendment`), the two
  cadastral flags, areas (cadastral, planned = the basis planned parcel, linked total for a
  split, the plan's stated area, delta m² / %, `mismatch` + bilingual note, always surfaced) and
  `calculation_basis`: `basis` urban | cadastral, `area_m2`, `reason` planned_parcel | split |
  no_planned_parcel | not_covered | unpublished with a bilingual explanation, and the `links`
  **from `parcel_links` of the current version** (rank order, filtered to documents still
  adopted, live and current; only for a covered parcel; rank 1 = the basis).
- **Group 1:** the 11 stored fields; a value resolves parcel → block → zone → document (the
  same precedence as the `urban_parcels` tile layer) and always carries `source` {document id +
  name, page, bbox, `file_id` (stored_files, null for seeded documents), note, registry URL,
  `value_id`, `viewer_url`}; a missing value is null with `reason` `not_in_document`,
  `rejected` (a `planning_value_gaps` row: the expert rejected the extracted value and nothing
  replaced it) or `unpublished` (no current version) — never a default. The computed max GFA and
  max coverage area follow in `computed` with their inputs.
- **Market** (null without a market row): the sale price per m² low / expected / high with
  scope (zone or municipality-wide row), source, source date, version. **Assumptions:** five
  items (construction cost, saleable share `share` 0–1, sale price, land and design rates) with
  value, low / high, source (`market` | `product_default`), `editable` and `engine_edit_key`, plus
  `formula_version` and the market version. **Group 2:** the 7 figures (land value, design &
  documentation, construction, market value `revenue_eur`, saleable area, profit, ROI) as the
  shared engine computed them, each with its `engine_key`; `status` ok | partial | unavailable;
  `input_flags` for missing FAR / coverage / height / floors (`used_by_formulas`, `affects`).
  **`engine.inputs`** are the exact engine inputs: `calculate(inputs)` in the browser gives the
  same figures, `recalculate(inputs, edits)` applies a visitor's edits (the TS engine and the
  Python copy are held to the shared fixtures; the integration test runs the built TS bundle).
- `GET /v1/zones/{zone_id}/panel`: title, subtitle, summary (as stored) with its label, the
  zone's current document versions with status labels, profile type name, `covered`,
  `file_available`, counts, typical parameters.
- **Cache** (`PanelCache`, Redis, `PANEL_CACHE_TTL_SECONDS`, 0 = off): one entry per entity per
  data state, key `panel:{app_version}:{m}:{kind}:{id}:{version_id}:{token}` where `token`
  hashes the current version's creation time and the state that changes panels outside a publish
  (documents' status / live / version / file columns, current market rows, current zone parameter
  sets), read by `STAMP_SQL` before the data (an entry can only be newer than its key). Market
  and zone parameter rows are immutable versions, so their ids fingerprint them. Strong `ETag`
  from the key; `If-None-Match` → 304 without Redis; `Cache-Control: no-cache`;
  `X-Panel-Cache` hit | miss | bypass | revalidated (CORS-exposed with `ETag`). Redis trouble →
  computed and `bypass`, Redis skipped for 5 s. 404 for an unknown id (nothing cached), 200
  `covered: false` for an uncovered parcel, 503 without PostGIS. A hit is one statement.
- **Links for every version:** the publish job writes `parcel_links` per version
  (`core/parcel_links.py`, shared SQL); the seed loader computes them for the seeded version
  and migration 0013 backfilled current versions that had none (defaults 1 m² / 2 %).
- Tests: `tests/test_parcel_panel_unit.py` (builders on canned rows, engine equality, cache) and
  `tests/integration/test_parcel_panel_postgis.py` (sources open the cited page, missing height,
  split / fallback / cadastral / uncovered, both engines and `/v1/panel` agree, zone panel, cache
  keys after admin changes, one statement per hit, index-backed plans, p95 < 200 ms cold and warm,
  `rejected` after a publish).

## Source viewer (`api/services/source.py`, `api/routers/v1/source.py`)

- Every planning value is traceable to its document in one click: the panel's `Source` carries
  `value_id` and `viewer_url = /v1/source/value/{value_id}`. `GET /v1/source/value/{value_id}`
  and `GET /v1/source/{document_id}/page/{page}` answer with **one short-lived signed URL** into
  the private bucket (`SOURCE_URL_EXPIRES_SECONDS`, default 900): the rendered page image
  (`{municipality}/planning-documents/{id}/pages/NNNN.png`, `kind: page_image`, PNG) when
  `planning_documents.page_images_rendered`, otherwise the PDF with a `#page=N` anchor
  (`kind: pdf_page`). Object keys never leave the API, the bucket stays private, responses are
  `Cache-Control: no-store`, `expires_at` says when the link dies. The value route adds the
  value's `bbox` (PDF points, origin bottom-left), `note`, field labels and the value itself.
- **Existence is decided by the database, never by probing storage** (migration 0004, set by the
  ingestion job): `planning_documents.file_key` (null = not stored), `page_count` (null =
  unknown: any page ≥ 1 of the PDF is served; page images need a known count),
  `page_images_rendered`. 404 `not_found` only when the document, value or page truly does not
  exist (`details.reason = not_stored` for a document without a file); storage or credential
  trouble is 503 `service_unavailable`. Values are read from the serving table only.
- The client emits `source_reference_opened` itself; `document_id` and `page` are in every
  response for that. The server does not emit analytics here.
- **The public map renders the page in the app** (`frontend/src/components/source/`): PDF.js
  reads the signed PDF link with range requests (only the cited page's bytes) and draws the
  value's `bbox` over it; "Open PDF" opens the whole document in a new tab. The bucket's CORS
  must allow `GET` with `Range` from the site origin and expose `Accept-Ranges`,
  `Content-Range` and `Content-Length`. The admin review queue reuses the same component.
- Sample data: `make seed` (`--upload-files`) uploads placeholder PDFs generated by
  `core.seeds.placeholder_pdf` for the sample documents with a `file_key` (no page images, so
  the sample always answers `pdf_page`). Each cited value of `planning_parameter_values.json`
  is printed in a frame at its `source_bbox` on its page (`sample_citations`), so the viewer's
  highlight visibly lands on the value.
- Local storage without Docker: `moto_server` (in the venv, `pip install "moto[server]"`, not a
  project dependency) on port 9100 with `S3_ENDPOINT_URL=http://127.0.0.1:9100`,
  `S3_USE_PATH_STYLE=true`; it keeps everything in memory, so re-run `make seed` (it re-uploads
  the placeholder PDFs) and set the bucket CORS again after a restart.
- Tests: `tests/test_source.py` (mocked storage + in-memory repository),
  `tests/test_placeholder_pdf.py` (annotated placeholder pages) and
  `tests/integration/test_source_postgis.py` (SQL repository, seeded files, panel → viewer).

## Geocoding (`backend/core/geocode/`, `api/services/geocode.py`, `api/routers/v1/geocode.py`)

- `GET /v1/geocode?q=` is a thin proxy for the search box: free text → a compact list of
  suggestions `{label, address, lat, lng, kind}` (`kind`: address | street | place | poi |
  other) plus the normalised `query` echoed for stale-reply detection, nothing else. Selecting
  a suggestion is the client's next call, `GET /v1/locate?lat=&lng=`: no parcel is resolved
  here and nothing touches the planning database. Deterministic, no AI.
- **The provider is configuration.** `GEOCODER_PROVIDER` picks an adapter from
  `core.geocode.PROVIDERS`: `photon` (default; OSM data, built for search-as-you-type) or
  `nominatim` (for a self-hosted instance via `GEOCODER_BASE_URL`; the public one forbids
  autocomplete and allows one call per second). A Google adapter is a new module implementing
  `GeocodeProvider` (`search(query, limit=, scope=)` → `GeocodeHit`s, `aclose()`), a registry
  entry and the literal in `core.config`. Every adapter maps its own taxonomy onto the compact
  vocabulary (`classify`, `compose_hit` in `core.geocode.base`); the service never sees
  provider fields.
- **Scope comes from the municipality profile, never from code:** `bounds` → viewbox / bbox,
  `country` → country filter, `center` → location bias, `locale` → label language
  (`GEOCODER_LANGUAGE` overrides; Photon only honours en/de/fr/it). Hits outside the box or
  carrying another country code are dropped server-side whatever the provider returned;
  duplicates collapse; at most `GEOCODER_MAX_RESULTS` (8) are returned.
- **Usage policy.** Identifying `User-Agent` (`<app>/<version> (+GEOCODER_CONTACT)`); Redis
  cache per normalised query (`geocode:v1:{provider}:q:{casefolded}`,
  `GEOCODER_CACHE_TTL_SECONDS`); minimum spacing between provider calls
  (`GEOCODER_MIN_INTERVAL_MS`, default = the provider's policy: nominatim 1000, photon 0)
  enforced with a Redis `SET NX PX` slot shared by all replicas (in-process fallback when Redis
  is down; a request waits at most `GEOCODER_THROTTLE_WAIT_MS` for a slot); a failure backoff
  (`GEOCODER_FAILURE_BACKOFF_SECONDS`) so a down provider is not hit once per keystroke; and the
  global per-IP rate limit.
- **Search never dead-ends.** Provider failure, throttling and Redis outages all answer 200
  with `results: []`; the CORS-exposed `X-Geocode-Status` header (hit | miss | too_short |
  throttled | provider_unavailable) says why. Queries shorter than `GEOCODER_MIN_QUERY_LENGTH`
  (2) answer empty without calling anyone. Only a missing, empty or over-long (200 chars) `q`
  is a 422.
- Tests: `tests/test_geocode.py` (fake provider through the app) and
  `tests/test_geocode_providers.py` (adapters against `httpx.MockTransport`).
- **Zone index** `GET /v1/zones` (`api/services/zone_index.py`, public, `Cache-Control:
  public, max-age=300`, 503 without PostGIS): every zone with `zone_type`, `covered` (the tiles'
  rule, `jobs.publish_layers.ZONE_COVERED`), `bbox`, a label point and a simplified outline
  (`ST_SimplifyPreserveTopology`, 0.00005° ≈ 5 m, GeoJSON MultiPolygon). The search box matches
  zone names against it and places geocoder hits in their zone ("Address · Centar", ⚠ outside
  coverage) without the geocoder touching the planning database; `/v1/locate` stays the
  authority after a pick. Tests: `tests/test_zone_index.py`.

## Staff pipeline API (`api/services/admin.py`, `api/routers/v1/admin_pipeline.py`, `core/auth.py`, `core/staff.py`)

- **Principals** (role `admin` on every route below): configured service tokens
  (`ADMIN_API_TOKENS`) or **staff sessions**, the users / roles model of migration 0006
  (`staff_users`: e-mail, role admin | reviewer | expert, active flag; `staff_sessions`: SHA-256
  token hashes with expiry / revocation). `api.deps.require_role` tries the config tokens, then
  `core.auth.StaffSessionAuthenticator`. The magic-link login item creates sessions with
  `core.staff.issue_session`; until then `python -m core.staff add|token|revoke|list`.
- **Files.** `POST /v1/admin/files` (multipart `file` + `kind` planning_document | gis |
  cadastral_extract) validates extension, declared type and file signature per kind, caps the
  size (`ADMIN_UPLOAD_MAX_MB`), hashes while reading and stores the object at
  `{municipality}/uploads/{kind}/{sha256}/{safe filename}`. A known checksum answers 200 with the
  existing `stored_files` record and stores nothing (`created: false`); a new file is 201.
  PDFs get a `page_count` (pypdf). Listings carry `document_ids` and recent `jobs`.
- **Documents are versioned.** `POST /v1/admin/documents` inserts one `planning_documents` row
  per version: `lineage_id` (first version's id; legacy rows: null = itself), `version`,
  `is_current_version` (partial unique index: one current per lineage). `replaces_document_id`
  must be the current version (else 409): it is retired (coverage taken offline) and version
  n+1 inserted with the previous coverage geometry copied, `file_key` / `page_count` from the
  stored file, `licence_note`, `registered_by/at`. Previous versions and the serving values
  that cite them stay. `type` must be a key of the profile's `document_types`; `zone_id`,
  `file_id` (a planning_document PDF) and `amends_document_id` must exist (422 otherwise).
  `coverage_geom` is nullable since 0006 (registered before the geometry job ran).
- **Jobs.** `POST /v1/admin/documents/{id}/jobs/extract` and `POST /v1/admin/files/{id}/jobs/geo`
  go through `jobs.enqueue.enqueue_job` (see "Background jobs"): one `pipeline_jobs` row
  (`extract_document` on `extraction`, `process_geometry` on `geo`), committed, then the Celery
  message; the reply is 202 with `status_url = /v1/admin/jobs/{id}`, or **200 with the existing
  job** when an identical one is queued / running / retrying (idempotency key = type + target +
  file SHA-256). A dead broker marks the job failed and answers 503 with the job id. The task
  bodies are stubs until the AI / GIS items land, so today a run ends `failed` with a clear
  "not implemented" error. Extraction writes to STAGING only; publishing is a separate job.
- **Coverage switch.** `PATCH /v1/admin/documents/{id}/coverage {live}` sets
  `planning_documents.coverage_live` (only the current version, only with a coverage geometry;
  409 otherwise). Location resolution and the panel take **adopted AND live** documents only
  (`locate_sql`, `panel_sql`; the urban panel's `covered` too). The sample's adopted documents
  are seeded live; synthetic bulk documents are live.
- **Audit.** Every action writes `audit_log` (actor subject + user id, action `file.upload`,
  `file.upload_duplicate`, `document.register`, `coverage.set_live`, `job.enqueue`,
  `job.enqueue_failed`, entity type + id, details, request id). Listings are not audited.
- Responses are `Cache-Control: no-store`. Tests: `tests/test_admin_pipeline_unit.py` (upload
  validation, filenames, Celery dispatcher with `send_task` mocked, token helpers, role gate with
  staff sessions) and `tests/integration/test_admin_pipeline_postgis.py` (dedup, versions, jobs
  with a fake dispatcher, coverage switch vs locate, staff sessions, audit rows, job lifecycle).

## Admin configuration API (`api/services/admin_config.py`, `api/routers/v1/admin_config.py`)

- Role `admin`, every write audited (`api/services/audit.py`, shared with the pipeline API).
- **Financial assumptions** (`/v1/admin/assumptions`, migration 0007): per zone or the
  municipality-wide default (`zone_id` null): four rates (`land_rate` per m² parcel,
  `build_rate` / `design_rate` per m² GFA, `sale_rate` per m² saleable), each
  `{expected, low?, high?}` (absolute bounds, both or neither, `low ≤ expected ≤ high`), the
  multiplier factors for rates without bounds, `source` (required: Realitica, Estitor,
  Monstat …), `source_date`, `notes`. Rows are immutable versions: POST inserts version n+1 for
  the zone and flips the previous current row off (`supersedes_id`); PUT creates the next
  version from the current row with the given changes (409 on a non-current row); DELETE
  retires the current row (the panel falls back to the default row or reports
  `no_market_data`); `include_history` lists earlier versions. The engine adapter turns absolute
  bounds into `absolute` engine bounds for that rate (`core/engine/feasibility.py`).
- **Zone parameter sets** (`/v1/admin/zone-parameters`, table `zone_parameter_sets`): the
  typical planning values of a zone (`land_use`, `max_far`, `max_site_coverage_pct` 0–100,
  `max_height_m`, `max_floors`; at least one) with a source document reference
  (`source_document_id` must exist, `source_page` needs it, `source_note`), `verified_on` (not
  in the future) and `verified_by`. Same versioning (POST / PUT / DELETE, one current per zone).
  The current row is the zone panel's `typical_parameters` (with the source document name and
  registry link and a bilingual note that a parcel's own document values take precedence);
  the sample seeds one for zone Centar (`zone_parameter_sets.json`).
- **Staff users** (`/v1/admin/users`): create (e-mail normalised to lower case, unique per
  municipality → 409; roles admin | reviewer | expert, the client's "expert reviewer" is
  `reviewer`), list with open session counts, PATCH role / display name / `is_active`
  (deactivating revokes the user's sessions; a user cannot deactivate or re-role themselves →
  409). No passwords anywhere: the magic-link login item issues sessions.
- Tests: `tests/test_admin_config_unit.py` (payload validation, adapter with absolute bounds)
  and `tests/integration/test_admin_config_postgis.py` (versions on the panel and the
  feasibility route, bounds in the ranges, zone panel, users with session revocation, audit).

## Expert review and the audit trail (`api/services/review.py`, `api/routers/v1/admin_review.py`)

- 100% of AI-extracted planning information is reviewed before publication; textual accuracy
  matters as much as numerical. `GET /v1/admin/review` (roles admin, reviewer, expert) pages
  STAGING (`planning_parameter_extractions`, migration 0008 added `entity_type`
  urban_parcel | zone | block | document | market_data, `zone_id` / `block_id`, `parameter_key`
  (planning field key, or a market rate key with `field_key` null), `raw_text`, `confidence`,
  the reviewer's `amended_value_*` and `reviewed_by_user_id`). Filters: document, zone, status
  (`pending` = `pending_review`, approved, amended, rejected), entity, urban parcel, page. Each
  item: parameter labels and type, `extracted` (the AI value with unit), `amended`, `effective`
  (what would publish), `target`, `source` (document, page, bbox, note, raw text snippet,
  confidence) and a signed `link` to the cited page (same rule as the source viewer:
  `api.services.source.signed_page_link`).
- **Decisions never overwrite the AI value.** `POST .../approve` accepts it, `.../amend`
  (`{value, unit?, note?}`, typed to the parameter: numbers stay numbers, texts texts) stores
  the correction alongside and sets `amended`, `.../reject` (`{note}` required) keeps it out
  and clears any correction. Any decision may be revised while the item is unpublished; an item
  with `published_value_id` is closed (409). `POST /v1/admin/review/bulk-approve` approves many
  pending items (ids, document page or urban parcel), one audit row per item.
  `GET /v1/admin/review/summary` (and `DocumentOut.review`) gives pending / approved / amended /
  rejected per document and `can_publish` = no pending items and something approved; the
  publish job (separate item) checks it. Approved / amended items are eligible for publishing
  and still not served until published.
- **`audit_log` is append-only at the database level** (migration 0008: a trigger raises on
  UPDATE, DELETE and TRUNCATE for every role). Columns: actor (+ user id), action, entity type
  and id, `before` / `after` (entity-specific summaries), `note`, `details`, request id,
  created_at. Every review decision and every admin change writes a row through
  `api.services.audit.write_audit` (assumptions and zone parameter versions with the previous
  and new figures, users, documents, coverage, jobs, files; order status joins when the
  payment item lands). `GET /v1/admin/audit` (admin, reviewer) filters by entity type / id,
  actor, action prefix and time. Test fixtures never delete audit rows.
- Tests: `tests/test_review_unit.py` (payloads, typed corrections, publish rule, page links) and
  `tests/integration/test_review_postgis.py` (queue payload, transitions with audit before /
  after, bulk approval, counters, append-only enforcement, audit listing).

## Orders (`api/services/orders.py`, `core/pricing.py`, `core/payments.py`, `api/services/order_mail.py`)

- **Guest checkout, no account, no password, no verification before purchase.** `POST /v1/orders`
  takes the location the panel showed (`{parcel_type, parcel_id}`), the purchaser type
  (`individual`: first name required, last name optional; `legal_entity`: company name, PIB /
  VAT number, contact person and invoice address required, and the contact person is the name
  the e-mails address when no first name is given), e-mail and telephone (validated), the
  assumptions the visitor edited, and an optional message. It answers 201 with the reference,
  the price, the turnaround, the bank-transfer instructions and the public status URL
  (`ORDER_PUBLIC_BASE_URL` + `/orders/{reference}`, the public map's order page). Capped per e-mail
  address and day (`ORDER_MAX_PER_EMAIL_PER_DAY`, 429) on top of the per-IP limiter.
- **Price is configuration, never logic:** `ORDER_PRICE_TIERS` (`"<max m²>:<EUR>,…,inf:<EUR>"`,
  BRD band EUR 50–200, tiers to be confirmed by the client; default = the mockup's 100 up to
  500 m² and 200 above) applied to the parcel's area basis (planned urban parcel area, else
  cadastral). `ORDER_TURNAROUND_BUSINESS_DAYS` gives `expected_by` (Mon–Fri, no holidays).
- **Reference** `UV-{KO}-{parcel}-{yymmdd}-{seq}` (`ko_short("Podgorica I") = "PODI"`), unique,
  retried on collision. **Snapshot**: the full panel payload the visitor saw (with their edits),
  its `data_version`, market assumptions version and formula version, stored on the order so
  the expert works from what was shown even after a later publish; the staff detail returns it.
- **Status flow** `pending_payment → paid → in_progress → delivered`, `refunded` from paid /
  in_progress; anything else 409. `POST /v1/admin/orders/{id}/payment` (`received` → paid with
  amount / date / bank reference, `not_received` → note only, `refunded`), `.../assign` (an
  active `expert` user; a paid order moves to in_progress), `PATCH .../status` (delivered needs
  a report), `POST .../report` (PDF → private bucket as `stored_files.kind = expert_report`,
  sets delivered, e-mails a signed download link, `ORDER_REPORT_LINK_EXPIRES_SECONDS`). Every
  change is an `audit_log` row with before / after. Admins and reviewers manage everything;
  experts see and deliver only their assigned orders (403 otherwise).
- **Public status** `GET /v1/orders/{reference}/status`: status, location and turnaround only;
  the public map's order page `/orders/{reference}` (`frontend/src/app/orders/`) reads it, and
  the confirmation and every order e-mail link there.
- **The public map's flow** (`frontend/src/components/order/`): S4 order modal from the parcel
  panel (location carried through, fee and turnaround from `GET /v1/orders/pricing`, inline
  validation with the API's rules), `POST /v1/orders`, S5 confirmation with the bank-transfer
  instructions on screen; `order_started` / `checkout_completed {order_id: <reference>,
  amount_eur}`.
- **Pricing for the panel** `GET /v1/orders/pricing` (public, configuration only, `Cache-Control:
  public, max-age=300`): `{currency, tiers: [{up_to_m2, price_eur}], turnaround_business_days}`;
  the public map shows a parcel's price by applying `core.pricing.price_for`'s rule to the panel's
  `basis_area_m2`, which is the area `POST /v1/orders` prices from.
- **E-mail**: `payment_instructions` on creation and `order_delivered` on report upload are
  `send_email` jobs queued through `api.services.email.EmailService` (see "Transactional
  e-mail"); the reply's `email_status` is `queued` (or the final state when the job already
  ran); a queue outage marks the `email_log` row failed and the order stands. The staff order
  detail lists `emails` and the queue shows `email_alerts` (bounced / failed).
- **Payments**: `core/payments.py` is the provider seam. The POC ships `BankTransferProvider`
  (instructions from `ORDER_BANK_*`, no online step); a card provider (Stripe vs Paddle is
  unverified in the BRD) implements `PaymentProvider` (checkout URL + webhook → `PaymentEvent`).
  No card data anywhere. `orders` is the only table with personal data.
- Tests: `tests/test_orders_unit.py` (tiers, turnaround, references, transitions, form
  validation, e-mail templates) and `tests/integration/test_orders_postgis.py` (creation with
  snapshot and e-mail, pricing from config, the status flow with guards, expert scope, report
  delivery, snapshot immutability after a republish, public status without personal data).

## Transactional e-mail and staff login (`core/mail/`, `jobs/tasks/email.py`, `api/services/email.py`, `api/services/auth.py`)

- **Only the job sends.** `EmailService.queue(template, to, order_id | user_id)` inserts an
  `email_log` row (`queued`) and one `send_email` job (email queue) whose payload carries ids
  only. The worker (`jobs.tasks.email.deliver`) loads the row, resolves the recipient and the
  facts from the order or the staff user at send time (`core.mail.repository`), renders the
  template, applies the sending policy, sends over SMTP and records the outcome on the row:
  `sent` with `provider_message_id` (the id in the provider's 250 reply, else our
  `Message-ID`) and `provider_response`, `suppressed` with `suppressed_reason`, `failed` with
  `error` after the retries (`attempts` counted). Transient SMTP trouble (connection, timeout,
  4xx) is `TransientError` → backoff retries (`JOB_MAX_ATTEMPTS`); authentication failures,
  5xx and refused recipients are permanent. Bodies are never stored, the subject is.
- **Templates** (`core/mail/templates/*.j2`, Jinja2, `StrictUndefined`, HTML auto-escaped):
  `payment_instructions` (reference, location, price, beneficiary / IBAN / bank / SWIFT /
  amount / payment reference, turnaround + expected date, status URL, support inbox),
  `order_delivered` (reference, location, signed download link + expiry, support inbox),
  `magic_link` (login URL, expiry minutes, single-use note). Montenegrin first, then English,
  in one message; wording provisional until the client approves it. Contexts come from
  `api/services/order_mail.py`; `core.mail.render` checks the required keys.
- **Transport and policy** (`core/mail/smtp.py`, `core/mail/policy.py`): `SMTP_HOST` / `PORT` /
  `USERNAME` / `PASSWORD` / `USE_TLS` (STARTTLS) / `USE_SSL` (465) / `FROM` / `TIMEOUT_SECONDS`,
  `MAIL_REPLY_TO` (default `ORDER_SUPPORT_EMAIL`), `MAIL_APP_NAME`. No `SMTP_HOST` → every row
  `suppressed (no_smtp_host)`. `APP_ENV=staging` mails only `MAIL_ALLOWLIST` (addresses or
  `@domain`; empty = nothing goes out); a non-empty allow-list is enforced in every environment.
  Dev: compose's Mailpit (SMTP 1025, inbox http://localhost:8025); without Docker
  `python -m core.mail.devsink` (SMTP 1025, each message an `.eml` file in the temp folder's
  `urbanview-mail`) with `SMTP_USE_TLS=false`. Deliverability check:
  `python -m core.mail.testsend --template payment_instructions --to you@…` sends fixture data
  through the real provider (DKIM / SPF / DMARC are the provider account's job).
- **Log and bounces**: `GET /v1/admin/email-log` (admin, reviewer; filters `order_id`,
  `user_id`, `status`, `template`), `GET /v1/admin/email-log/{id}`,
  `POST /v1/admin/email-log/{id}/bounce {reason}` (sent → bounced, audited `email.bounce`; the
  provider's bounce webhook will call the same method). Orders show `email_alerts` in the queue
  and `emails` in the detail.
- **Magic-link login** (`api/routers/v1/auth.py`, public): `POST /v1/auth/magic-link {email}`
  always answers 202 with the same neutral message; an active staff address gets a `magic_link`
  e-mail whose token the job mints (`staff_login_tokens`: sha256 hash, `MAGIC_LINK_EXPIRES_SECONDS`
  = 900, `used_at`), link `{ADMIN_BASE_URL}/login?token=…`. `POST /v1/auth/magic-link/exchange
  {token}` consumes it once and returns a staff session bearer token (`staff_sessions`,
  `STAFF_SESSION_DAYS`) with the user; 401 for unknown / used / expired. Audited
  `auth.magic_link_requested`, `auth.login`.
- Tests: `tests/test_mail_unit.py` (every template against fixture data, policy, MIME, provider
  ids, the job body on the in-memory repository) and `tests/integration/test_mail_postgis.py`
  (through the API with eager Celery and a transport double: log rows with provider ids, jobs,
  magic-link round trip, staging allow-list, bounces on the order, retries then failure).

## Analytics (`api/services/analytics.py`, `api/routers/v1/events.py`, `admin_analytics.py`)

- The prototype is a validation instrument: `POST /v1/events` ingests batches (≤ 100) of the
  13 product events into `analytics_events` (migration 0005; model `core/models/analytics.py`;
  CHECK on the name; indexes on municipality + name + time, session, zone). Each event:
  `name`, anonymous client-generated `session_id`, optional anonymous persistent `client_id`
  (repeat usage), optional `event_id` (retried batches are de-duplicated, never errors),
  tz-aware `occurred_at` (not in the future), and a small flat `properties` object.
- **Never personal data.** Unknown names, malformed ids, nested / oversized properties, a
  denylist of keys (name, email, phone, ip, user_agent, address …) and string values that look
  like an e-mail or IP address reject the whole batch with 422. Request IPs are never stored.
  Known properties are typed (`parcel_id`, `zone_id`, `document_id`, `page` … positive ints;
  `search_kind` ∈ address | click | parcel_number, `result` ∈ address | zone | parcel, booleans
  `matched` / `recent` / `visible` / `on`; `panel_type`; `amount_eur` ≥ 0; `sessions`)
  and some are required (`search_performed.search_kind`, `layer_toggled.layer_id`,
  `source_reference_opened.document_id + page`, `checkout_completed.amount_eur`). `zone_id` and
  `parcel_id` are copied into columns for grouping (no FKs).
- `GET /v1/admin/analytics?from=&to=` (role `admin`; `[from, to)`, default last 30 days, max
  366) returns: funnel conversion per step (map_loaded → search_performed / parcel_selected →
  panel_viewed → financials_viewed → order_started → checkout_completed; a session counts at a
  step when it emitted one of its events in the range, conversions are session ratios), orders
  and revenue (from `order_started` / `checkout_completed` events: distinct `order_id`,
  `amount_eur` summed once per order, by product; an orders table replaces this later), most
  searched districts (`search_performed` + `parcel_selected` by `zone_id`, joined to zone
  names), repeat usage (`return_visit` sessions / sessions; sessions per `client_id` against the
  prototype target of 3+, plus what `sessions_per_user` events report), `market_data_interest`
  / `ai_interest` counts, and panel views reaching financials (distinct (session, parcel) pairs
  with `panel_viewed` that also have `financials_viewed` for the same parcel, plus the session
  view). One SQL statement per aggregate; assembly (percentages, 1 decimal) in Python.
  Responses are `Cache-Control: no-store`.
- **Role gate** (`core/auth.py`, `api.deps.require_role`): `ADMIN_API_TOKENS` =
  `token:role[:subject],...` (roles admin | reviewer | expert; validated at startup). The admin
  tool's server side sends `Authorization: Bearer <token>`; missing / unknown → 401 with
  `WWW-Authenticate: Bearer`, wrong role → 403 with `required_roles`. No tokens configured =
  every staff route answers 401. Constant-time comparison; tokens are never logged.
- Tests: `tests/test_events_ingest.py` (validation, storage, de-duplication with a fake
  repository), `tests/test_admin_analytics.py` (gate, assembly of every aggregate from canned
  rows, ranges), `tests/integration/test_analytics_postgis.py` (SQL of every aggregate on a
  crafted event set, index use).

## Background jobs (`jobs/`, `api/services/jobs.py`, `api/routers/v1/admin_jobs.py`)

- **One job system.** Every long-running task is a `pipeline_jobs` row (migration 0010: `type`
  extract_document | process_geometry | publish_approved | send_email, `kind` family, `queue`,
  `target_type` document | file | publish_run | email + `target_id`, `payload`, `status` queued |
  running | retrying | succeeded | failed | cancelled, `attempts` / `max_attempts`,
  `manual_retries`, `next_retry_at`, `dedupe_key`, `wall_time_ms`, `llm_model`,
  `llm_tokens_in/out`, `estimated_cost_eur`) delivered to a worker as `(job_id, municipality_id)`.
  Celery app `jobs/celery_app.py`: queues `default`, `extraction` (LLM), `geo` (geometry),
  `publish`, `email`, routed by task module; `CELERY_TASK_ALWAYS_EAGER=true` runs tasks inline
  (tests only).
- **Base task** `jobs.base.JobTask`: a task function `(self, job_id, municipality_id)` hands its
  body `work(job: JobContext) -> JobResult | dict | None` to `self.execute(...)`, which runs
  `run_job_async` on a store (`SqlJobStore` in workers, `MemoryJobStore` in unit tests;
  `configure_job_store`): queued → running (attempt counted) → succeeded with `result`, wall time
  and the cost the body reported (`JobResult.cost`; `jobs.cost.cost_for(model, tokens_in,
  tokens_out)` prices from `LLM_PRICE_EUR_PER_MTOK_INPUT/OUTPUT` or `LLM_PRICE_TABLE`); a
  transient error (`TransientError`, `RateLimited`, `TimeoutError`, `ConnectionError`) with
  attempts left → `retrying` with `next_retry_at` and a self re-delivery after
  `JOB_RETRY_BASE_SECONDS × 2^(attempt-1)` (capped at `JOB_RETRY_MAX_SECONDS`); the last
  transient failure or any other exception → `failed` with `error` (hard failures are never
  retried); a re-delivered message for a finished job is skipped. `JOB_MAX_ATTEMPTS` (3) is
  stamped on the row at enqueue time. The lifecycle never raises: the row is the source of
  truth, Celery's result backend only mirrors it.
- **Enqueue** (`jobs.enqueue.enqueue_job`, `JOB_TYPES`): insert + commit, then `apply_async` on
  the registered task (honours eager mode; `send_task` fallback), then the task id on the row; a
  broker failure → row failed + 503. **Idempotent:** the partial unique index
  `uq_pipeline_jobs_active_dedupe` (municipality, `dedupe_key`, while queued / running /
  retrying) makes the same key return the existing job; keys are `type:target_type:target_id`
  plus `sha256:<file checksum>` for document / file work, so the same content never runs twice.
  Task modules stay import-light (the API imports them to dispatch).
- **Tasks** (`jobs/tasks/`): `extract_document` (extraction queue), `process_geometry` (geo),
  `publish_approved` (publish; one active run per municipality), `send_email` (email; payload
  `{template, to, context}`, `to` a reference resolved at send time, never a stored address).
  All four are stubs that fail with a clear "not implemented" error until their items land.
  `system.ping` is the broker smoke test.
- **API** (role `admin`): `GET /v1/admin/jobs` (filters `type`, `status`, `target=document:12`
  | `file:` | `publish_run:` | `email:`, `document_id`, `file_id`; `total`),
  `GET /v1/admin/jobs/{id}` (the status URL), `GET /v1/admin/jobs/costs` (tokens, estimated
  cost and wall time summed per target), `POST /v1/admin/jobs/{id}/retry` (failed / cancelled →
  queued, attempts reset, `manual_retries` + 1, audited `job.retry`, re-dispatched; 409 when not
  retryable or while a job for the same key is active). Every `JobOut` carries a `cost` block.
- **Run it.** `make worker` / `poe worker` (`-Q default,extraction,geo,publish,email`),
  `make flower` / `docker compose --profile monitoring up flower` (http://localhost:5555; set
  `FLOWER_BASIC_AUTH` before exposing it), compose `worker` service from
  `backend/Dockerfile.worker`. Tests: `tests/test_jobs_unit.py` (backoff, cost, keys, lifecycle
  on the memory store, a real task in eager mode) and `tests/integration/test_jobs_postgis.py`
  (SQL store, idempotent API, listing, retry, costs, eager task through the API).

## Publish pipeline (`jobs/publish_pipeline.py`, `jobs/publish_layers.py`, `jobs/tiles.py`, `api/services/publish.py`)

- **One button.** `POST /v1/admin/publish` (roles admin, reviewer; body `{label?, notes?}`)
  refuses with 409 `reason = pending_review` and `details.documents` (id, name, pending count)
  while any document has items pending review; otherwise it queues one `publish_approved` job
  (202; 200 with the active job while one is queued / running: key
  `publish_approved:publish_run:-`, `max_attempts = 1`). `GET /v1/admin/publish` is the status
  screen: `current` (label, who, when, counts, layers, signed archive link), `versions` (newest
  first), `active_job` / `last_job` with `progress` (`step` + one entry per step: pending |
  running | done | failed, timestamps, detail), `can_publish`, `blockers`, `keep_versions`.
- **The job** (`PublishPipeline.run`, one database transaction from preflight to flip, so
  visitors see the previous version until the commit and a failure leaves nothing behind):
  `preflight` (pending items = hard failure) → `version` (new `publish_versions` row, not
  current) → `values` (the previous version's `planning_parameter_values` carried forward for
  current document versions, overridden by approved / amended items: amended value wins, unit
  `COALESCE(amended, extracted)`, every row cites the item's page; items closed with
  `published_value_id`; items without a page / value or with a parcel–document mismatch are
  listed in `result.skipped_items` and stay open; `market_data` items are not published yet;
  expert-rejected fields that nothing replaced become `planning_value_gaps` rows of the version,
  counted as `values_rejected`, so the parcel panel can say `rejected` without reading staging)
  → `geometry` (staged batches, see below) → `links` (`parcel_links`: cadastral ↔ planned
  overlaps with locate's thresholds, `rank 1` = the panel's primary: largest overlap, smallest
  planned area, lowest id; `cadastral_unmatched` counted) → `cells` (`heatmap_cells` per block
  and zone: area-weighted coverage % and FAR, max height, GFA / saleable area / market value
  sums through `core.engine.feasibility.compute_feasibility` with the zone's current
  assumptions, `sale_rate_eur_m2`, `price_band` = tercile of the zone sale rates) → `export`
  (one newline-delimited GeoJSON file per catalogue layer) → `tiles` (tippecanoe per layer with
  its own zoom range, `tile-join` into one PMTiles archive: one source layer per map layer,
  independent visibility) → `upload` (`{m}/tiles/{version_id}/{label}.pmtiles`, private bucket)
  → `flip` (`is_current`, archive columns, `counts`, `layers`, `duration_ms`, audit
  `publish.complete`, commit) → `prune` (retention, best effort). The result carries counts,
  layers, skipped items, pruned versions and the duration; progress is written per step.
- **Layer catalogue** (`jobs.publish_layers.LAYERS`, data not code): `zones` (with `zone_type`
  and `covered` = the zone has an adopted, live, current document with coverage; the map colours
  covered zones by type and mutes the rest), `zone_labels` (one `ST_PointOnSurface` point per
  zone, same properties; point layers are built with `--drop-rate=1` so no label is thinned out),
  `document_coverage`, `urban_blocks`, `urban_parcels` (with the effective parameters:
  parcel → block → zone → document scope, plus `max_gfa_m2`), `cadastral_parcels` (with
  `has_urban_parcel`, `primary_urban_parcel_id`, `overlap_fraction`, `area_delta_m2`, and the
  `zone_id` / `zone_type` of the zone containing the parcel's point on surface),
  `public_ownership`, `legal_burdens` (cadastral flags as their own layers), `land_use`,
  `traffic_network` (generic `layer_features`), `block_cells`, `zone_cells` (heatmaps). Empty
  layers are left out of the build but listed with 0 features.
- **Zone type** (migration 0014): `zones.zone_type` res | com | mix | pub | grn (CHECK) or null
  (not classified: drawn neutral, never a guessed colour); from the seed or the staged `zones`
  feature's `zone_type` property (invalid values ignored).
- **Staged geometry** (`geometry_batches` + `staging_geometry`, the GIS ingestion contract in
  `STAGED_LAYERS`): one batch per (file, layer) with `status = staged`; features carry a natural
  `feature_key` and JSON `properties`. Entity layers are upserted by natural key so UrbanView
  ids stay stable (cadastral: KO + number + sub-number; urban parcels: document + number, block
  by `block_ref`; blocks: `block_ref`; zones: `name`; `document_coverage`: `document_id` →
  `coverage_geom`); no deletes. Generic layers (`land_use`, `traffic_network`) are copied into
  `layer_features` for the version (the newest staged batch wins, older ones `superseded`;
  layers without a new batch are carried forward). Batches end `published` with
  `published_version_id`.
- **Versions and rollback.** Values, `layer_features`, `parcel_links`, `heatmap_cells` and the
  archive are per version; entity geometry is upserted in place (a geometry rollback needs a
  re-ingest: documented limitation). `POST /v1/admin/publish/rollback {version_id?}` flips
  `is_current` to the given version (default the one before the current), audited
  `publish.rollback`; 409 when nothing is earlier, the target is current or its archive was
  pruned; 404 for an unknown id. Retention `PUBLISH_KEEP_VERSIONS` (3, ≥ 2): after a publish,
  versions beyond the newest N lose their archive object and derived rows
  (`archive_pruned_at`); version rows and values stay for history. Never the current or the
  previous version.
- **Choropleth classes** (`api/services/cell_classes.py`): the tile pointer carries
  `cell_classes` for the current version's cells, computed on read in one statement (cells are
  immutable per version): per block metric (`max_far`, `max_site_coverage_pct`, `max_height_m`,
  `max_gfa_m2`) quintile breaks over non-null values, rounded and de-duplicated; per zone sale
  rate (`sale_rate_eur_m2` and its `_low_` / `_high_` bounds, migration 0015: the market row's
  absolute bounds, else expected × range factors) the profile's fixed bands
  (`price_band_breaks_eur_m2`, Podgorica 1300 / 1700 / 2100; 0 = not saleable), quantiles when
  none are configured. `null_count` = cells drawn as no data. The public map colours and its
  legend use the same classes.
- **Tiles pointer.** `GET /v1/tiles/current` (public, `no-store`): `status` published |
  unpublished, `data_version`, `version_id`, `published_at`, one signed `archive_url`
  (`TILES_URL_EXPIRES_SECONDS`, PMTiles range requests) + `expires_at`, `layers`, `min_zoom`,
  `max_zoom`. The seeded version has no archive (`archive_url: null`).
- **Run it.** The worker image builds tippecanoe 2.79 (`backend/Dockerfile.worker`,
  `TIPPECANOE_BIN` / `TILE_JOIN_BIN`); `TILES_MIN_ZOOM` / `TILES_MAX_ZOOM` clamp the catalogue's
  per-layer ranges; `PUBLISH_TMP_DIR` for the scratch files. Tests: `tests/test_publish_unit.py`
  (catalogue, cell aggregation, price bands, labels, tippecanoe commands) and
  `tests/integration/test_publish_postgis.py` (through the API with eager Celery, a fake tile
  builder and storage: refusal naming the document, the amended value in the panel and the tile
  layer, cells and links, staged geometry, rollback, idempotency, retention). Tests wanting a
  clean pointer reset version 1 to current and delete newer versions.

## GIS track: geometry assessment (`backend/core/gis/`, `docs/gis/`)

- **Week-1 gate 1** (BRD "Geometry extraction: to be determined by sample assessment"):
  `python -m core.gis.assess ../docs/gis/source/catalog.toml --out ../docs/gis/assessment`
  (`make gis-assess`, `poe gis-assess`). Needs the `gis` extra (pymupdf, shapely) and GDAL's
  `ogrinfo` for DWG / DXF / SHP / GeoPackage (PATH, `$OGRINFO`, else the portable PostGIS bundle's
  `pgsql/bin`: GDAL 3.9, no PDF reading, CAD driver reads DWG R2000 only). The gate summary is
  `docs/gis/geometry-assessment.md`; `docs/gis/assessment/` is the generated evidence
  (`report.md`, CSVs per document / page / layer type / PDF layer / georeferencing / GIS file,
  `assessment.json`, `previews/*.png` re-drawn from the extracted layers). A full run inspects
  for about 15 minutes; `--cache FILE` (a pickle, keep it outside the repo) stores the inspection
  and `--report-only --cache FILE` rebuilds effort, decisions and the report in seconds after the
  rules, the effort model or the catalog's document facts change.
- **Inputs:** the client's files in `docs/gis/source/` (their Drive folder structure) and
  `catalog.toml`: one `[[document]]` per planning document (registry facts, `status`, `kind` plan
  | base_map, `parameters` table + `parcel_id_pattern`, `alternative_group`, reviewer
  `observations`) with `files` (`shows` = the layer types the sheet carries, optional `layers`
  override verified by eye, `expected` counts, `sheet_set` for 10a + 10b, `coordinate_table`).
  New GIS / CAD files dropped next to the catalog are inspected too.
- **Inspector** (`inspect_pdf.py`, measures only): per page size / paper / rotation, text layer,
  images with effective dpi, vector paths, and per PDF layer (OCG = the CAD layer) the geometry
  form: `rings`, `lines`, `dashes` (plotted dash / dot linetypes), `hatch`, `ribbons` (wide
  polylines plotted as outlines), `tessellation` (solid hatches as triangles), `glyphs` (text drawn
  as vectors), `text`. AutoCAD's glyph-id shift (codes 29 below the letter, South Slavic letters
  remapped) is decoded (`decode_glyph_ids`, `looks_glyph_shifted`). Scale is never taken from the
  viewport `/Measure` alone: `resolve_scale` checks the stated scale, the viewport and the grid
  crosses, which must come out a round number of metres apart (the Novi Grad PDF's viewport
  says 1:776, the grid proves 1:1000). Grid labels are matched to the profile's CRS candidates.
- **Classification** (`assessment.py`): per sheet and layer type A (layers identify it and the
  sample closes it), B (unlayered, or the layer holds only part of the features), C (scanned).
  `sample.py` buffers linework, unions it and counts the holes as faces (closes dotted lines and
  ribbons alike), compared with expected counts (parameter table parcels, id labels as text,
  catalog). Effort model `EFFORT` (stated assumptions), georeferencing method per sheet
  (GeoPDF > grid labels > grid-cross lattice > coordinate table > cadastral match > manual),
  go / no-go per document for ticket 07 and whether native DWG / DXF is needed.
- **Place knowledge is profile data** (`[gis]` in `municipalities/<id>.toml`, read by
  `core.municipality.load_gis_profile` into `GisProfile`; the served `MunicipalityProfile` ignores
  it, so `GET /v1/municipality` and the OpenAPI stay unchanged): CRS candidates with coordinate
  ranges, CAD layer patterns for plan sheets and base maps, sheet titles, keywords. The Podgorica plans are drawn in **EPSG:3908** (MGI 1901 / Balkans zone 6;
  vertex tables write the easting as X); `source_crs_epsg` says so. EPSG:3965, the only
  MGI 1901 → WGS 84 transformation for Montenegro, is good to 10 m: georeferencing fits its own
  datum shift on common points.
- Tests: `tests/test_gis_assessment.py` (synthetic A / B / C / table sheets, glyph-id decoding,
  viewport vs grid scale, lattice, dotted boundaries, decisions, GDAL inspection when `ogrinfo`
  exists, the CLI end to end).

## Frontend design (`docs/specs/frontend-design.md`, `docs/wireframe/`)

- **Built in `frontend/`** (shell, shared components, typed API client, analytics); details in
  `frontend/CLAUDE.md`. The API's OpenAPI document is exported with
  `python -m api.export_openapi` (backend) and turned into TypeScript types with
  `npm run api:types` (frontend); never hand-write API types.
- **The public map reproduces the wireframe exactly** (product owner, 2026-09-24; overrides the
  BRQ's "not strictly"). Port `docs/wireframe/wireframe.css` verbatim as the global stylesheet
  (its late override passes set the effective sizes and the 8 px radius on every classed
  element), keep the mock's class names and markup per component (templates in
  `docs/wireframe/wireframe.js`), copy its inline SVG icons and copy strings. Fonts: Schibsted
  Grotesk 400–800 and JetBrains Mono 400 / 500 / 700, self-hosted.
- Acceptance: `docs/wireframe/screens/<state>.png` (one per state, 1440×900 plus three narrow
  widths) and `docs/wireframe/computed-styles.json` (effective styles of 257 selectors);
  `python docs/wireframe/make_screens.py [state…] [--dump]` regenerates both with headless
  Chrome / Edge.
- Allowed deviations only (spec §9): Mapbox + PMTiles instead of the SVG city; API data (all 13
  planning fields, each with its `source` chip); market data locked behind the wireframe's
  "Choose your access" modal, whose "Subscribe" records `market_data_interest` / `ai_interest`
  and, for market data, unlocks the figures for the session (no checkout); the AI fab and panel are a UI shell that records
  `ai_interest`; no card fields; bilingual text; at ≤ 860 px the rail collapses into a drawer and
  the panel becomes a bottom sheet. The admin view is the wireframe's overlay inside the frontend
  (tab placeholders for now). Anything else is an open item (spec §10), not a redesign.
- Scope is unsettled: `docs/UrbanView_POC_Exclusions.docx.md` (265 h POC) excludes screens that
  the build plan and this backend include; ask before building screens it excludes.

## Conventions

**Municipality isolation (BRD §8).** Everything place-specific is data or configuration:
bounds, centre, CRS, KO list, planning terminology (IZ/II/KO/UP, DUP/PUP/PGR) and data-source
URLs come from `backend/municipalities/<id>.toml` via `core.municipality.load_profile`; never
hard-code them. Every domain table and every Celery task carries `municipality_id`; object keys
are `{municipality_id}/{kind}/{name}`; the formula engine knows no municipality.

**Database.** Async SQLAlchemy 2 + asyncpg + GeoAlchemy2. Models in `backend/core/models/`
(import each module in `core/models/__init__.py`). Geometry `geometry(MultiPolygon, 4326)` with
`spatial_index=False` on the column and an explicit GiST `Index` named `idx_<table>_<column>` in
`__table_args__` (`core.models.planning.gist_index`), mirrored by `op.create_index(...,
postgresql_using="gist")` in the migration; expression indexes are written the way PostgreSQL
reflects them. Partial unique indexes are `Index(..., unique=True, postgresql_where=text("..."))`
on the model and `op.create_index(..., unique=True, postgresql_where=sa.text("..."))` in the
migration (Alembic 1.20 compares the key columns, not the WHERE text: keep both identical by
hand). CHECK constraints (`CheckConstraint(..., name="ck_<table>_<rule>")`) and enums
(`postgresql.ENUM(..., create_type=False)` created / dropped explicitly with `checkfirst=True`;
model `Enum(..., values_callable=...)`) are declared in both model and migration because Alembic
does not compare them; column comments are compared, so they match too. Migrations: `make
migration m="..."` then `make migrate`; `alembic check` runs in the integration tests, so models
and migrations must agree.

**Configuration.** `core.config.Settings` (pydantic-settings), env vars or `backend/.env`
(`backend/.env.example`). `APP_ENV` dev / staging / prod; non-dev refuses dev DB defaults, missing
S3 credentials and `ADMIN_API_TOKENS` shorter than 24 characters. Secrets are `SecretStr`. No
secrets in code. `S3_PUBLIC_ENDPOINT_URL` (optional) is the address signed links are made for
(browsers), while `S3_ENDPOINT_URL` is where the API and worker read and write (on the server:
`https://files.<domain>` vs `http://minio:9000`).

**Deployment.** `deploy/README.md`: one Hetzner server, `docker compose -f deploy/compose.yml
--env-file deploy/.env up -d --build`, updates with `deploy/deploy.sh`. The frontend image
(`frontend/Dockerfile`, context = repo root, its own `Dockerfile.dockerignore`) builds Next.js with
`NEXT_OUTPUT=standalone`; `NEXT_PUBLIC_*` are build args. Caddy terminates HTTPS and sets
`X-Forwarded-For` (`TRUST_PROXY_HEADERS=true`).

**Errors.** One envelope `{"error": {"code", "message", "request_id", "details"?}}`. Raise
`core.errors.AppError` subclasses for expected client errors; unhandled exceptions become
`internal_error` (500) logged with request id. Validation errors are `validation_error` (422).

**Request context.** `RequestContextMiddleware` (outermost) logs a well-formed `X-Session-ID`
(the public map's anonymous analytics session id, sent on every browser call) on the access
line and in `scope.state`; it sets `X-Request-ID`,
`Server-Timing`, `X-Response-Time`, logs one access line per request with route + latency.

**Rate limiting.** `RateLimitMiddleware`: per client IP, fixed window in Redis, 429 in the
envelope with `Retry-After` + `X-RateLimit-*`, `/health*` exempt, fails **open, fast** (250 ms
budget per Redis call + 5 s circuit breaker), `TRUST_PROXY_HEADERS` gates `X-Forwarded-For`.
Middleware order (outermost first): RequestContext → CORS → RateLimit → routes.

**Jobs.** Queues `default`, `extraction`, `geo`, `publish`, `email`; every task is a
`jobs.base.JobTask` over a `pipeline_jobs` row (see "Background jobs"). Nothing reaches the
public map except through `jobs.tasks.publish.publish_approved` (see "Publish pipeline").

**Tests.** Unit: `make test` (fakeredis, frozen clock, `LOCATION_RESOLVER=nodata`). Integration:
`make test-integration` with `TEST_DATABASE_URL` (skipped when unreachable); the session fixture
resets the schema, runs migrations up → base → up and loads `podgorica_sample`. Build apps with
`tests.helpers.make_app`, clients with `make_client(app, client_ip)`.

**E-mail.** Never send from the API process: queue a `send_email` job through `EmailService`;
job payloads carry ids, never addresses or bodies; `email_log` is the record.

**Commands.** `make install | run | worker | migrate | migration | seed | test | test-integration
| lint | fmt | gis-assess | up | down | db-dev-install | db-dev-start | db-dev-stop` (Windows without make:
`cd backend && poe <task>`).
