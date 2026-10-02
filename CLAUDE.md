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
| `backend/` | FastAPI app (`api/`: app factory, routers under `/v1`, schemas, services), `core/` (settings, logging, errors, middleware, db, models, `engine/` feasibility formulas, `geocode/` geocoding providers, `gis/` geometry assessment, `cadastre/` cadastral base loader, `extraction/` the AI extraction contract, redis, storage, mail, municipality profiles, seeds loader), `jobs/` (Celery: ingestion, extraction, publish), `municipalities/<id>.toml`, `tests/` (unit) and `tests/integration/` (PostGIS). Python venv: `backend/.venv`. |
| `database/` | Alembic (`alembic.ini`, `migrations/`), seed datasets (`seeds/podgorica_sample/*.geojson` for the geometry tables, `*.json` for the panel tables), compose init SQL (`docker/initdb/`), `scripts/dev_postgis.py` (portable PostGIS for Docker-less machines). |
| `frontend/` | Public map and the staff console (`/admin/*`, Auth.js magic links, roles admin / reviewer / expert): Next.js 16 (App Router) + TypeScript + Radix (shadcn/ui primitives) + Mapbox GL JS + TanStack Query, styled by the wireframe stylesheet (no Tailwind), npm workspace `@urbanview/frontend`. Its own `frontend/CLAUDE.md` holds the tokens, dimensions, layer list, panel field lists and frontend rules. |
| `packages/` | `feasibility-engine/`: the shared TypeScript feasibility engine (npm workspace of the root `package.json`) and `fixtures/feasibility-cases.json`, the fixture file both engines are held to. |
| `deploy/` | Production on one server (Hetzner Cloud): `compose.yml` (Caddy HTTPS → web / api / MinIO; worker, PostGIS, Redis internal), `Caddyfile`, `.env.example`, `server-setup.sh`, `deploy.sh` (pull + rebuild + migrate), `README.md` (step by step). |
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
**The planned parcel's area is the one its plan states** (the published parcel-level
`planned_parcel_area_m2`), else the drawn parcel's (`urban_parcels.area_m2`): product owner,
2026-10-02, after Novi Grad's drawing closed block-sized shapes for about half its parcels (UP 18:
1 387 m² in the plan, 59 201 m² as drawn). One rule in the panels (`basis_area_m2`,
`api.services.panel._plan_area`, `parcel_panel.basis_view`), the tiles' `max_gfa_m2` and the
heatmaps (`jobs.publish_layers.PLAN_AREA`), so the order price too; the drawn area stays in
`areas.urban_parcel_area_m2` with `stated_vs_geometry_delta_pct`, and the map's panel says both
when they differ by 2 % or more. An area stated at block / zone / document scope is not a
parcel's area; an uncovered parcel has no served value and keeps the drawn area.

**Zones and documents:** a "zone" is UrbanView's internal city division (~city quarter) grouping
several planning documents (in Montenegro zones are not official bounded areas). Document status
is `adopted` / `in_progress` / `superseded`. **Only adopted documents are covered.**

**Panel Group 1 (planning, free):** Parcel ID, planning document name, land use, urban block,
urban parcel number, urban parcel area, max height/floors, max site coverage % (IZ), max FAR (II),
calculated max GFA. **Every value carries a source document reference** (document + page).

**Panel Group 2 (market, paid subscription):** estimated land value, design & documentation
costs, construction costs, market value, saleable area, potential profit, ROI. **All as
low / expected / high ranges**, never single figures. The POC sells no subscription: Group 2 is
shown to everyone (see "POC scope").

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

**Market data sources:** Realitica, Estitor listings + Monstat statistics. Each profile source
states how its data reaches UrbanView today (`[[sources]]` `format`, `integration`
linked | manual_upload | file_import | reference_copy | access_pending | access_confirmed |
not_connected, `integration_note`; a cadastral source's integration follows its
`[cadastre.sources.<id>].access`): the console says "Linked" only for a live connection (A1
check 2026-09-29).
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
Outside coverage the public map is the base map alone (BRD S6 "zone geometry absent", the POC
plan; S6 check 2026-09-29): the tiles carry no heatmap cell or block there and cadastral parcels
say `covered: false` (`backend/core/coverage.py`: the covered rules in SQL for the publish
catalogue, the heatmap job and the zone index); the map shows a neutral pill for 2.6 s and then
the pin's note "no adopted plan published here yet". Uncovered searches still count as location
demand: the analytics districts place them by their point.

**Analytics events (13):** `map_loaded`, `search_performed`, `parcel_selected`, `layer_toggled`,
`panel_viewed`, `financials_viewed`, `source_reference_opened`, `order_started`,
`checkout_completed`, `return_visit`, `sessions_per_user` (BRD §6.2's eleven), `market_data_interest`,
`ai_interest` (the pilot scope's two intent buttons). One list: `core.models.analytics.AnalyticsEvent`
(the API validates against it, `ck_analytics_events_name` is built from it). The Group 2 check's
`assumption_edited` was removed on 2026-10-01 (not in the plan's list; migration 0038).

**Performance target:** under 2 seconds from query to populated panel. Requests slower than
`SLOW_REQUEST_MS` are logged at WARNING with the route template.

## POC scope (audit 2026-09-28, POC estimation v2)

- **Not built as working features:** AI assistant / chat (no fab, chat panel or quota: the
  parcel panel's intent button "Ask about this site" records `ai_interest` and says so in a
  toast); subscriptions and any paywall (Group 2 and the price heatmap are shown to everyone; the
  intent button "Unlock full market data" records `market_data_interest` and unlocks nothing: the
  pilot scope's two intent buttons, intent check 2026-09-28); hosted card checkout
  (bank transfer only); a zone editor (zones are drawn in QGIS, `core/zones`); ownership /
  restitution map layers (no toggles, no tile layers; the cadastral flags stay null until a
  confirmed eKatastar extract loads them; the tiles do not carry them); the planned-traffic
  layer (not extracted from the drawings, not staged, published or drawn); 3D / AR.
- **Accepted deviations from the plan's stack:** one `public` schema, staging and serving told
  apart by table (`planning_parameter_extractions`, `staging_geometry`, `geometry_batches`,
  `staging_zone_documents` vs `planning_parameter_values`, `parcel_links`, `layer_features`,
  `choropleth_cells`); serving data is per `publish_versions` row (earlier versions are kept for a
  manual pointer flip by an operator; there is no rollback endpoint), but entity geometry is
  upserted in place, the current version's heatmap cells and links can be
  recomputed in place, and only `audit_log` and `analytics_events` (0030) are append-only at the
  database level (published planning values refuse UPDATE, 0034). The server runs
  the Python copy of the engine, held byte-identical to the TypeScript package's fixtures. PMTiles
  come from the private MinIO bucket through Caddy with signed links, no CDN. Auth.js signs staff
  in through a Credentials provider over the backend's magic-link tokens.
- **Removed by the POC conformance pass (2026-09-29, the 220 h plan's funded rows are the
  scope):** the tablet / phone layout, the "Free" badge and the `tier` markers, the legal pages,
  the publish rollback endpoint and buttons, the Calculation engine tab (engine proposals) and
  the Planning rules tab (zone parameter sets, the zone panel's typical parameters), the
  assumptions preview, the ownership / legal-burden map layers, topology QA beyond geometry
  validity, the e-mail bounce endpoint and the card-provider payment seam, Flower, the backup
  script and the CI workflow; the analytics page is plain tables (funnel, districts, intent
  counts). Migration 0035 drops `engine_proposals`, `zone_parameter_sets` and the
  `rolled_back_*` columns.
- **Public-app check (2026-09-30):** routes the map never called are gone (`GET
  /v1/cadastral-municipalities`: the KO list comes from the profile; `GET
  /v1/orders/{reference}/status`: the confirmation data endpoint serves the order page; `GET
  /v1/zones/{zone_id}/panel`: the zone panel reads `/v1/panel?type=zone`); the rail shows exactly
  the plan's seven toggleable cards (base map and document coverage are drawn without a card);
  the public urban panel shows the pilot scope's Group 1 fields (parking, green area and
  utilities stay extracted and reviewed, not shown); the market approval derives a zone's first
  range factors from the reviewed ranges instead of a fixed 0.86 / 1.15.
- **Orders and admin check (2026-09-30):** admin routes and screens no funded row needs are
  gone: the Overview tab and `GET /v1/admin/overview`, bulk approval (`POST
  /v1/admin/review/bulk-approve`, `POST /v1/admin/geometry/bulk-approve`: the plan's review is per
  item), `GET /v1/admin/email-log*` (an order's e-mails are on its detail), `GET
  /v1/admin/jobs/costs` (each job carries its cost), the separate PDF pre-processing trigger
  (`POST /v1/admin/files/{id}/jobs/preprocess` and job type `preprocess_file`: the extraction
  and geometry jobs run the stage first) and the `/order/<ref>` redirect. Migration 0036 adds
  `formula_versions` and drops the `ai_check` / `preprocess_file` job types and the `email_log`
  bounce columns. Table `app_secrets` (the removed AI settings page's) was dropped by 0038 once
  the server's settings held `ANTHROPIC_API_KEY`. The nav is Documents, AI review queue, Publish, Financial
  assumptions, Orders, Analytics, Audit log (+ Users in the account menu, back to the map).
- **API surface and schema check (2026-09-30):** `tests/test_api_surface.py` holds the exact
  route list, each route with the plan item that needs it (a route outside it fails the
  suite). Removed: `GET /v1/admin/files` and `/files/{id}` (no screen read them; the upload
  reply, the document detail and `GET /v1/admin/jobs?file_id=` carry the same facts), the
  pasted portal listings import (`POST /v1/admin/market/listings`, `core/market/listings.py`,
  the `listings` import kind and range basis, `MARKET_MIN_LISTINGS` / `MARKET_LISTINGS_*`:
  v2 funds Monstat and the client's ranges, portal listings are the pilot's), `GET
  /v1/admin/market/coverage` (+ `python -m core.market coverage`), `GET
  /v1/admin/review/market-inputs/{id}` (the list carries every field, a decision answers the
  item), the review queue's dead `market_data` entity, the first order form's
  `contact_person` / `registered_address` columns, and the PostGIS image's extra extensions
  (`postgis_tiger_geocoder`, `postgis_topology`, `fuzzystrmatch`): migration 0037. Fixed:
  locate's zone list shows current document versions only; `GET /v1/source/value/{id}`
  serves the current version's values only (an earlier version's id is a 404). Accepted
  deviations, deferred (a redesign or a client decision, not a smallest diff): the entity
  tables (`zones`, `planning_documents`, `urban_blocks`, `urban_parcels`, `cadastral_parcels`)
  carry no version (upserted in place, above); planning values attach to parcels, blocks,
  zones or documents because the client's plans state them per urban parcel (the pilot's
  `parameter_set` is per block); blocks carry no `document_id` (matched by label and
  overlap); no `sample_size` per market input.
- **Workers check (2026-09-30):** removed the market import's LLM mapping (`core/market/llm_map.py`,
  `MARKET_NORMALISE_LLM` / `MARKET_MODEL` / `MARKET_EFFORT` / `MARKET_MAX_TOKENS` /
  `MARKET_LLM_MAX_ROWS`: the profile's rules map Monstat and the client's range sheets, a sheet
  they cannot map is reported), the unused `system.ping` task and the hand-labelled extraction
  sample (`core/extraction/sample.py`, `check-sample`, `eval --sample`: the corpus replaces it).
  Accepted deviations: jobs are `pipeline_jobs` (cost in EUR, statuses + retrying / cancelled);
  the schema and the model adapter live in `core/extraction/` (no `packages/schema`,
  `packages/llm`); an approved market input writes an effective-dated assumptions version at
  once (the pilot's `serving.market_input` on publish; the v2 stack keeps assumptions in
  `public`); the height heatmap is the maximum floor count (the plans state floors, not metres);
  runtime thresholds are `Settings` (`LOCATE_MIN_OVERLAP_*`, `LINK_*`) and place data the
  profile (CRS, layer patterns), not one file. Prompt set 1.0 was deleted on 2026-10-01 (1.1 is
  the only set; `eval --dry-run`, which replayed 1.0-shaped answers, went with it).
- **Analytics / audit brief and excess sweep (2026-10-01):** the analytics endpoint returns the
  brief's aggregates (funnel to paid, orders by status, top zones + uncovered hits, repeat
  sessions, intent counts; zeros on an empty range), `POST /v1/events` judges rows one by one
  (13 events: `assumption_edited` gone), the audit list is admins-only and filters action +
  actor. Removed as outside the funded rows: OCR (Tesseract option), the server-side refusal
  fallback, page images (`page_images_rendered`, 0038), prompt set 1.0 and `eval --dry-run`, the
  planned-traffic extraction, topology QA (georef `parcel_overlaps`, zone overlaps / gaps), the
  synthetic 10 000-parcel volume and the p95 / latency tests, the ownership flags in the tiles,
  `app_secrets` (0038), the unaudited `core.staff add|token|revoke`, the payment-provider seam,
  `CELERY_TASK_ALWAYS_EAGER`, the frontend's Tailwind setup, the unfunded wireframe CSS (AI,
  plans, locks, badges, card payment, KPI tiles, tablet / phone layouts) and screenshots, the
  third intent button ("Ask about this document"), the review item's audit widget, dead exports
  and copy that promised later phases, subscriptions or a learning engine. The urban panel shows
  the plan's Group 1 only (building line and setbacks are extracted, not shown). Server note:
  production's `deploy/.env` still carries the placeholder bank beneficiary / IBAN (the client's
  account is needed; `deploy/.env.example` now says `change-me`).
- **Open (S3 check):** a separate `land_use_code` in Group 1 and a `sample_size` per market
  input: neither is in the data yet (`docs/specs/frontend-design.md` §10 item 22).
- **Tester's report on the public map (2026-10-02, 23 items):** fixed in code: the panel's one
  urban area and its drawn-shape note (4), the parcel panels in Montenegrin (5), the land-use
  layer coloured per parcel (6, `core.land_use`; needs a publish to rebuild the tiles), search by
  planned parcel number (7, `GET /v1/locate/urban-parcel`), the compact button stack (9), the
  shortcut hint per system (10), the methodology's English (11), no internal ids in the panel
  (12), a document's `source` never an import name (13, migration 0039), "Not stated" in words
  (14), one market-data line (15), the published version's number instead of its label (16), the
  market-interest message in the string table (17), the order reference without a doubled "UP"
  (18), the reopened `?order=` link shows the status (19), the price legend's unit (20), a
  legend code named beside itself (21, the profile's `[extraction.land_use_codes]`), the section
  source chip opens a cited value so a cell is framed (23). **Novi Grad parcel shapes (2), fixed in the extraction rules
  2026-10-02:** the parcel of an existing building is the building's outline
  (`OBJEKTI-POSTOJECI` / `OBJEKTI-NOVI`, with the numbered vertices), which the rules never read,
  so its label fell into the open ground and took the streets around it; the rules now read
  those layers as fallback linework with `absorb: false` (`LayerRule`: unlabelled fallback
  pieces are left out instead of joining a parcel), `absorb_along` (the other segments of a
  labelled building, outlined by the building layers, still join it: UP 26, 44, 56),
  `vertex_marks` (two parcels inside one building outline are cut along the shortest line
  between two of the plan's numbered vertices that puts their labels on different sides:
  UP 14 / 15, UP 90 / 91) and `gap_mm: 1`. 93 parcels, UP 14 and UP 90 for the first time; of
  the 90 with a stated area 85 are within 2 % of it and 89 within 10 % (36 and 42 of 88
  before); UP 66 measures 589 m² for the 497 m² stated (the building as drawn). The rules also
  state the datum operation (`georef.transform`, see "Georeferencing"), so a run lands where the
  published data is. It reaches the map the way the plan did the first time: the extraction's
  GeoPackage staged in the worker with `python -m core.gis.georef apply … --stage` (the
  console's geometry job answers a vector PDF with that instruction), the geometry review,
  Publish; then the prepared values once more for the parcels drawn since (UP 14, UP 90: see
  "Prepared planning values") and Publish. **Server settings, not code:** SMTP (3) was set on
  the server on 2026-10-02 (a Brevo account on port 587, sender `noreply@urbanview.io`) and the
  server switched to `APP_ENV=prod`, the only difference from `staging` being that e-mails go
  to every address instead of the allow-list: order e-mails and staff sign-in links are sent
  (until then the confirmation said honestly that no e-mail went out). The console's open
  access was switched off the same day (`ADMIN_OPEN_ACCESS_TOKEN` emptied and its entry taken
  out of `ADMIN_API_TOKENS`): every `/admin` page asks for the e-mailed sign-in link again and
  the admin API answers 401 without a staff token. **Still open:** the support address the
  e-mails name (`ORDER_SUPPORT_EMAIL` is the example placeholder), the client's bank name and
  SWIFT (8: `ORDER_BANK_*` in `deploy/.env`), the codes U, SR and TS of Stara Varoš are named
  as the legend's UK, SKR and a transformer station (product owner 2026-10-02; the client's
  planner still confirms). The pin behind the legend (22): on a small
  window the open legend covers the map's middle, so a flown-to place now lands beside it
  (`lib/map/camera.ts` `legendOffset`).
- **Run-time check and audit against the 42 funded rows (2026-10-02):** every row has code
  behind it (prompt tuning is evidenced on two plans, Stara Varoš on pages 1–23 only, no
  baseline). Fixed: the site's own calls no longer share one rate-limit bucket ("Rate
  limiting"); a published figure the engine cannot use no longer answers 500 and cannot be
  approved as extracted; UP F3360/1 of Stara Varoš is served as the plan prints it, IZ 120 %
  (the index 1.2; it was 1.2 %: `prepared.served_number`); review items read before their
  parcel existed are linked at publish; the zone panel counts a plan's urban parcels while no
  cadastral parcel is loaded; the staff order search finds first names and the drawer lists
  land value, design & documentation and total cost; an e-mail that cannot be built ends
  `failed`, not `queued`; one unused sign-in link per staff address per minute
  (`MAGIC_LINK_MIN_INTERVAL_SECONDS`); the land-use layer leaves out staged polygons outside
  live coverage; "Open PDF" works after its link expired; `parcela 1042` is a parcel
  reference. **Known, not changed:** a job whose worker restarts mid-run stays `running` and
  blocks Publish until cleared by hand (do not deploy while a job runs); a published value can
  be replaced, not withdrawn; market import and its review have routes and a CLI but no
  console screen (figures are entered on Financial assumptions); retention after a manual
  pointer flip can prune the version just left; explicit nulls on `PATCH /v1/admin/users/{id}`
  and `PUT /v1/admin/assumptions/{id}` answer 500; job cost is recorded on success only;
  `/v1/locate` states the drawn planned area; the orders list shows the newest 200.
- **Tester's report on expert orders (2026-10-02, 13 items):** fixed in code (2026-10-03): the
  reopened `?order=` confirmation of a closed order says so ("Report delivered" / "Order
  refunded", 1; it said "Order confirmed … your payment has been received" for a delivered or
  refunded order; the order page `/orders/<reference>` was right all along, and it carries no
  download link on purpose: the reference alone opens it, the report link is e-mailed); the
  console's bar after sign-in (2: behind the reverse proxy the redirect out of the sign-in action
  did not render the layout again, so the first page had no tabs and no account menu; the
  sign-in page now loads the console as a new page); the console's exact times on the
  municipality's clock, named on every screen (8: "placed 2026-10-02 18:07 (Podgorica time)";
  they were bare UTC); a refund's amount, date and bank reference in the order's Payment
  section (12); data versions by their number (13: `v12` in the orders queue and drawer, the
  number on the order page; the answers carry `data_version_no`). Already fixed on 2 October:
  the confirmation of an order whose e-mail is not sent says so (3: that order was placed while
  the server had no SMTP host). **Not code:** the payment-instructions e-mail of
  UV-UP-40-261002-01 was accepted by the mail provider (4: `email_log` holds its 250 reply) and
  `urbanview.io` has no DKIM record for the provider and no DMARC record (11: its DNS holds the
  provider's verification code and Google's mail records only), which is why Outlook distrusts
  the messages; the domain's owner adds the provider's DKIM records and a DMARC record. The bank
  name and SWIFT (6) and the support address (10) are the client's to give (`ORDER_BANK_*`,
  `ORDER_SUPPORT_EMAIL` in `deploy/.env`). **Open, the client's decision:** e-mails on payment
  received / not received / refunded (5: the plan funds two order e-mails), company name and PIB
  required for a legal entity (7: optional by the pilot scope's form), the expected date counted
  from the payment instead of the order (9: `expected_by` is set when the order is placed; the
  e-mail says "if paid today").
- **POC data without the model (2026-10-01, product owner):** the planning values of the two
  POC plans are prepared from their parameter tables by the table reader and loaded as approved
  items (see "Prepared planning values"), not AI-extracted and not reviewed item by item by an
  expert; the 100 % review rule holds for what the AI extracts from later documents.

## Location resolution (`backend/api/services/locate_sql.py`, `resolver.py`)

- `GET /v1/locate?lat=&lng=` and `GET /v1/locate/parcel?ko=&number=&sub=` run **one PostGIS
  statement** (CTEs, single round trip) and return the same `LocationResolution` payload:
  cadastral parcel (with geometry + centroid), primary planned urban parcel and all matching
  ones, urban block, zone (with its document list), governing document, `calculation_basis`,
  `area_comparison`, `centroid`.
- Governing document = the **adopted** (live, current-version) document whose coverage contains
  the point; when several do (a DUP inside a PUP) the **most specific** (smallest coverage)
  wins. The zone's document list holds the current versions only.
- Planned urban parcels = those containing the point, plus those overlapping the cadastral parcel
  by ≥ `LOCATE_MIN_OVERLAP_M2` (1 m²) and ≥ `LOCATE_MIN_OVERLAP_FRACTION` (2%) of its area, so the
  correspondence is shown even when the click lands in land the plan takes for roads, and
  digitising slivers are ignored. Order: point match, governing document, largest overlap.
- Parcel lookup: `ko` is required and case-insensitive; `number` may be `1042/3`; the reference
  point is `ST_PointOnSurface` of the parcel; `centroid` is where the map pans.
- **Planned parcel by number** `GET /v1/locate/urban-parcel?number=UP 40` (`UrbanParcelSearch`,
  2026-10-02: with no cadastral base loaded no parcel number could be searched at all): the
  planned parcels of adopted, live, current plans whose number matches without spaces, case and
  the profile's parcel abbreviation (`urban_parcel_key`: "UP 40" = "up40" = "40"), each with its
  document, zone and a point inside it (`ST_PointOnSurface`), at most 8 (the same number can
  exist in several plans). Always 200; none is an empty list. The search box asks it only for
  text that starts with the abbreviation (a bare number stays a cadastral reference).
- Points outside the municipality bounds (profile) short-circuit to `outside_municipality`.
- Every access path is index-backed (GiST on the geometries, the unique KO + number index);
  `tests/integration` checks one statement per call. There is no load or latency testing in the
  POC (the synthetic 10 000-parcel volume and the p95 / latency budgets were removed on
  2026-10-01).

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
  the pointer decides what is served and with no current version nothing is served. The sample's
  serving rows are version 1 from the seed loader.
- One PostGIS statement per panel type (`panel_sql.PANEL_SQL`, CTEs + `jsonb_build_object`,
  locate's style), nothing cached between requests. The governing document is locate's rule;
  the cadastral ↔ planned parcel links are the **published** ones of the current version
  (`parcel_links`, see "Parcel links": computed by the publish job with locate's thresholds
  `LOCATE_MIN_OVERLAP_*`), to planned parcels of documents still adopted, live and current. A
  cadastral panel with an urban basis embeds the **primary** (rank-1) link's planning and
  feasibility (largest overlap, then smallest planned area, then lowest id); every link sits in
  `urban_parcels` with its shares of either parcel and the relation; the panel states
  `relation` (same | reduced | enlarged | split | merged | none), `split` (two or more planned
  parcels each over `LINK_SPLIT_MIN_FRACTION` of the parcel), `no_urban_parcel` (covered, no
  planned parcel: the "Not defined" state) and `reduction_pct`; the urban panel's linked
  cadastral parcels carry theirs.
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
  the cited page: see "Source viewer"). A `land_use` value that is a code of the plan's legend
  (Stara Varoš prints `SS`, `MN`, `CD` …) also carries `value_name`, the name the legend gives it
  (`core.land_use.legend_name` over the profile's `[extraction.land_use_codes]`; null for a
  wording or a code the legend does not name): the value stays what the plan prints, the panel
  shows `SS · stanovanje srednje gustine`.
- **Data version.** `data_version` = `label` of the `publish_versions` row with `is_current` (at
  most one per municipality), `data_version_date` = its `published_at` as a UTC date; with no
  current row `"unpublished"` / null and no planning values (they belong to a version). Values
  may also carry a `block_id` or `zone_id` scope (migration 0011; the panel resolves parcel →
  document only, the block / zone scopes feed the heatmap cells). Market inputs have
  their own effective-dated version history (`financial_assumptions.version`; the panel reads the
  version that applies on the municipality's local date, `core.assumptions`):
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
  `construction_cost_eur`, `total_cost_eur`); it adds no formula. A published figure the engine
  refuses (a negative index, a site coverage above 100 %: UP F3360/1 of Stara Varoš prints the
  index 1.2) is handed over as not stated (`_usable`): the panel shows the value with its
  source, the figures that need it cannot be calculated (the adapter's own reason codes
  `far_not_usable` / `coverage_not_usable`), and no request fails on it. Ranges
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
- **Assumptions and overrides.** Precedence: query override > the zone's market version that
  applies today (`financial_assumptions`, effective-dated, see "Admin configuration API"; the
  municipality-wide `zone_id IS NULL` row never stands in for a zone since 0019) > for
  `saleable_share` that version's own `saleable_share` (migration 0023, null = none) > the
  product constant 0.70 (engine default). Overrides never rescue a missing market row: land and
  design rates would be unknown, so the money figures are `cannot_calculate` (`no_market_data` /
  `no_market_data_zone_unknown`) while GFA, coverage and saleable area still compute.
  `assumptions` echoes the numbers actually used, `overrides` (query param present) and `sources`
  (`market` | `user` | null); the parcel panel's saleable share item says `market` when the zone's
  version sets it, else `product_default`.
- **Zone and document panels** (what the public map's S3 variants show): the zone panel lists
  the zone's **current** document versions, each with `covered` (adopted, live, current, with
  coverage: the rule of locate and the tiles), `file_available` (PDF stored) and `parcel_count`
  (the parcels a visitor can open there: the cadastral parcels whose point on surface is in the
  coverage, or, while no cadastral parcel is loaded there, the plan's own urban parcels; null
  when not covered); `zone`
  carries `zone_type`, `counts.covered` the covered documents. The document panel's `document`
  carries `file_available`; its `zones` carry `zone_type`. Every `DocumentRef`
  carries `adopted_on` (migration 0016, nullable, entered at registration: `DocumentIn.adopted_on`,
  not in the future; the sample has none). The profile's `terminology.document_types_en`
  gives the English type names the map shows (`DUP — Detailed urban plan`).
- **Amendments** are linked only by `planning_documents.amends_document_id` (set at ingestion,
  never by coverage intersection): the header lists the governing document, then its
  `in_progress` amendments; the document panel lists them as `amendments_in_progress`.
- **Disclaimer** (`feasibility.disclaimer_en` / `_me`, `panel_text.DISCLAIMER`):
  `disclaimer_status = "placeholder"` until the lawyer signs the wording off
  (`client_approved`), `disclaimer_version = "poc-1"`.
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
  `GET /v1/panel` keeps its shapes (all four panel types) and reads the same published links.
- **Header:** KO, parcel number, title, address, zone, block, documents with status and role
  (`governing`, `basis` when the planned parcel's document differs, `amendment`), the two
  cadastral flags, areas (cadastral, planned = the basis planned parcel, linked total for a
  split, the plan's stated area, delta m² / %, `mismatch` + bilingual note, always surfaced) and
  `calculation_basis`: `basis` urban | cadastral, `area_m2`, `reason` planned_parcel | split |
  no_planned_parcel | not_covered | unpublished with a bilingual explanation, `relation`,
  `split`, `no_urban_parcel`, `reduction_pct`, and the `links` **from `parcel_links` of the
  current version** (rank order, filtered to documents still adopted, live and current; only for
  a covered parcel; rank 1 = the basis; each with `overlap_pct`, `share_of_urban_pct`,
  `relation`).
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
- (`GET /v1/zones/{zone_id}/panel` was removed 2026-09-30: the map's zone panel reads `/v1/panel?type=zone`; it showed) title, subtitle, summary (as stored) with its label, the
  zone's current document versions with status labels, profile type name, `covered`,
  `file_available`, counts.
- **Cache** (`PanelCache`, Redis, `PANEL_CACHE_TTL_SECONDS`, 0 = off): one entry per entity per
  data state, key `panel:{app_version}+{PANEL_PAYLOAD_FORMAT}:{m}:{kind}:{id}:{version_id}:{token}`
  (`PANEL_PAYLOAD_FORMAT` in `panel_cache.py`: bump it when a body changes for the same data, so a
  deploy never serves bodies of the previous code) where `token`
  hashes the current version's creation time, its links' computation time (a standalone
  recompute) and the state that changes panels outside a publish
  (documents' status / live / version / file columns, the market versions that apply today in the
  municipality's time zone — a scheduled version taking effect changes the key at local midnight),
  read by `STAMP_SQL` before the data (an entry can only be newer
  than its key). Market rows are immutable versions, so their ids fingerprint
  them. Strong `ETag`
  from the key; `If-None-Match` → 304 without Redis; `Cache-Control: no-cache`;
  `X-Panel-Cache` hit | miss | bypass | revalidated (CORS-exposed with `ETag`). Redis trouble →
  computed and `bypass`, Redis skipped for 5 s. 404 for an unknown id (nothing cached), 200
  `covered: false` for an uncovered parcel, 503 without PostGIS. A hit is one statement.
- **Links for every version:** the publish job writes `parcel_links` per version
  (`core/parcel_links.py`, see "Parcel links"); the seed loader computes them for the seeded
  version, migration 0013 backfilled current versions that had none and 0026 classified the
  existing ones in place.
- Tests: `tests/test_parcel_panel_unit.py` (builders on canned rows, engine equality, cache) and
  `tests/integration/test_parcel_panel_postgis.py` (sources open the cited page, missing height,
  split / fallback / cadastral / uncovered, both engines and `/v1/panel` agree, zone panel, cache
  keys after admin changes, one statement per hit, index paths for the values and gaps,
  `rejected` after a publish).

## Parcel links (`backend/core/parcel_links.py`, migrations 0011 / 0026)

- **Existing vs planned (BRD §2.2: "Show both", "Calculation basis", "Never silent").** For
  every served cadastral parcel, which planned urban parcels (of adopted, live, current-version
  documents) cover it and by how much, and the reverse: one statement per publish version.
  Candidates from the GiST `&&` / `ST_Intersects` join; intersections and areas in the
  municipality's metric CRS (the cadastre profile's `area_crs_epsg`, EPSG:25834); slivers below
  `LOCATE_MIN_OVERLAP_M2` (1 m²) or `LOCATE_MIN_OVERLAP_FRACTION` (2 %) of the cadastral parcel
  dropped; ranked like the panel's primary (largest overlap, smallest planned area, lowest id).
- **Table `parcel_links`**: per version and pair `overlap_area_m2`, `overlap_ratio_of_cadastral`,
  `overlap_ratio_of_urban`, both areas as the cadastre and the plan state them
  (`cadastral_area_m2`, `urban_area_m2`), `area_delta_m2`, `relation`, `reduction_pct`, `rank`,
  `dataset_version` (the version's label); a cadastral parcel with no planned parcel has one
  `none` row (`urban_parcel_id` null, rank 1: the map's `no_urban_parcel`, the panel's "Not
  defined"). Per version, so they follow the pointer.
- **Relation** of a cadastral parcel, on each of its rows, first match: `none`; `split` (two or
  more planned parcels each cover `LINK_SPLIT_MIN_FRACTION` 10 % of it); `merged` (its planned
  parcel covers that share of two or more cadastral parcels); `reduced` (its planned parcel
  covers less than that share of it); `same` (each covers all but `LINK_SAME_TOLERANCE` 2 % of the
  other); `enlarged` (the planned parcel larger by more than the tolerance); else `reduced`.
  `reduction_pct` = the share of the cadastral parcel in no planned parcel (taken for roads /
  public space): the client's example, cadastral 100 → planned 75, is `reduced` by 25 %.
- **Runs** as the publish job's `links` step after the geometry step (same transaction);
  standalone for QA: `python -m core.parcel_links summary | recompute [--version N] [--json]`
  (parcels per relation, parcels without a planned parcel, average reduction of the reduced
  parcels, merged planned parcels, cadastral parcels covered more than once). Every recompute
  stores its summary with the rules, metric SRID, `duration_ms` and `computed_at` on
  `publish_versions.links_summary` and logs the time; above `PARCEL_LINKS_MAX_SECONDS` (300, the
  agreed limit) it logs a warning. Coverage switched live after a publish reaches the links at the
  next publish or a `recompute`.
- Tests: `tests/integration/test_parcel_links_postgis.py` (one fixture per relation drawn in the
  metric CRS, shares of a split parcel summing to 100 %, the three panels, the QA command, a
  logged full recompute stored on the version).

## Source viewer (`api/services/source.py`, `api/routers/v1/source.py`)

- Every planning value is traceable to its document in one click: the panel's `Source` carries
  `value_id` and `viewer_url = /v1/source/value/{value_id}`. `GET /v1/source/value/{value_id}`
  and `GET /v1/source/{document_id}/page/{page}` answer with **one short-lived signed URL** into
  the private bucket (`SOURCE_URL_EXPIRES_SECONDS`, default 900): the PDF with a `#page=N`
  anchor (`kind: pdf_page`; page images were removed on 2026-10-01, 0038). Object keys never
  leave the API, the bucket stays private, responses are
  `Cache-Control: no-store`, `expires_at` says when the link dies. The value route adds the
  value's `bbox` (PDF points, origin bottom-left), `note`, field labels and the value itself, and
  opens the file the value cites (`planning_parameter_values.source_file_id`, set by the publish
  job from the item's run, migration 0022; null = the document's `file_key`). The review queue's
  page link follows the item's run file the same way
  (`source.file_id` / `file_name`, filter `file_id`); the parcel panel's `source.file_id` too.
- **Existence is decided by the database, never by probing storage** (migration 0004, set by the
  ingestion job): `planning_documents.file_key` (null = not stored) and `page_count` (null =
  unknown: any page ≥ 1 of the PDF is served). 404 `not_found` only when the document, value or
  page truly does not
  exist (`details.reason = not_stored` for a document without a file); storage or credential
  trouble is 503 `service_unavailable`. Values are read from the serving table only, the
  current version's (an earlier version's value id is a 404).
- The client emits `source_reference_opened` itself; `document_id` and `page` are in every
  response for that. The server does not emit analytics here.
- The pilot technical scope's `GET /api/documents/{id}/source?page=n` is
  `GET /v1/source/{document_id}/page/{page}` here (kept after the source viewer check of
  2026-09-28: the whole API is under `/v1`). The signed URL's path carries the object key (as
  every presigned S3 link does); the bucket stays private (unsigned GET and listing are 403).
- **The public map renders the page in the app** (`frontend/src/components/source/`): PDF.js
  reads the signed PDF link with range requests (only the cited page's bytes) and draws the
  value's `bbox` over it; "Open PDF" opens the whole document in a new tab. The bucket's CORS
  must allow `GET` with `Range` from the site origin and expose `Accept-Ranges`,
  `Content-Range` and `Content-Length`. The admin review queue reuses the same component.
  A page it cannot show says why, from the 404's `details` (`reason: not_stored` + the registry
  link, `page` / `page_count`, `value_id`); Retry only for connection or server trouble.
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
- **Provider timeout** `GEOCODER_TIMEOUT_MS` (per request phase, `httpx.Timeout`; default 1500,
  production **3000**). The public Photon instance answers in 1–3 s (1.0–2.3 s measured from the
  server, 2026-09-29): at 1500 every slower answer was a `ReadTimeout`, so search answered
  `provider_unavailable` and the map said "No match". The timeout only decides whether a slow
  answer is kept; it adds no wait when Photon is fast, repeats come from the Redis cache, and an
  answer slower than 3 s is still dropped (then the 5 s backoff). The < 2 s target for a typed
  address needs a self-hosted geocoder (`GEOCODER_BASE_URL`); parcel and zone searches never call
  one.
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

- **Roles (the pilot technical scope's, auth check 2026-09-29; `api/deps.py`, every `/v1/admin/*`
  route through `require_role`):** `admin` = everything; `reviewer` ("planning expert approving
  extractions") = the review queue (A2: read, approve, amend, reject, market inputs), publish (A4),
  read-only documents / files / jobs; `expert` ("produces paid reports") = the orders assigned to them and the
  report upload (A6), `users/me`. Reviewers have no order access; experts none to the review
  queue or the pipeline.
- **Principals** (writes on the routes below: role `admin`, `PipelinePrincipal`; the listings and
  reads of documents, files and jobs: `admin` and `reviewer`, `DocumentReaderPrincipal`; experts
  get 403): configured service tokens
  (`ADMIN_API_TOKENS`) or **staff sessions**, the users / roles model of migration 0006
  (`staff_users`: e-mail, role admin | reviewer | expert, active flag; `staff_sessions`: SHA-256
  token hashes with expiry / revocation). `api.deps.require_role` tries the config tokens, then
  `core.auth.StaffSessionAuthenticator`. The magic-link exchange creates sessions with
  `core.staff.issue_session`; users are managed on the console's Users page (audited). `python -m
  core.staff login-link --email … [--create --role admin]` prints a one-time console sign-in link
  without SMTP (audited `auth.login_link_issued`, and `user.create` with `--create`); `python -m
  core.staff list` lists the staff. (The unaudited `add` / `token` / `revoke` commands were removed
  on 2026-10-01.)
- **Files.** `POST /v1/admin/files` (multipart `file` + `kind` planning_document | gis |
  cadastral_extract) validates extension, declared type and file signature per kind, caps the
  size (`ADMIN_UPLOAD_MAX_MB`), hashes while reading and stores the object at
  `{municipality}/uploads/{kind}/{sha256}/{safe filename}`. A known checksum answers 200 with the
  existing `stored_files` record and stores nothing (`created: false`); a new file is 201.
  PDFs get a `page_count` (pypdf). The reply carries `document_ids` and recent `jobs`; there
  is no file listing (removed 2026-09-30: the document detail lists each file with its jobs).
- **Documents are versioned.** `POST /v1/admin/documents` inserts one `planning_documents` row
  per version: `lineage_id` (first version's id; legacy rows: null = itself), `version`,
  `is_current_version` (partial unique index: one current per lineage). `replaces_document_id`
  must be the current version (else 409): it is retired (coverage taken offline) and version
  n+1 inserted with the previous coverage geometry copied, `file_key` / `page_count` from the
  stored file, `licence_note`, `registered_by/at`. Previous versions and the serving values
  that cite them stay. `type` must be a key of the profile's `document_types`; `zone_id`,
  `file_id` (a planning_document PDF) and `amends_document_id` must exist (422 otherwise).
  `coverage_geom` is nullable since 0006 (registered before the geometry job ran).
- **Edit, short code, municipality (A1 check 2026-09-29, migration 0032).** `PATCH
  /v1/admin/documents/{id}` (admin, `DocumentPatchIn`) changes the current version's status,
  name, `short_code`, zone, source, registry link, adoption date or licence note; only what
  differs is written, with one `document.update` audit row (before / after of the changed fields);
  409 `not_current_version`; 422 for nothing to change, an unknown zone or clearing name / status.
  A status change counts at once in locate and the panels (adopted documents only) and in the
  tiles at the next publish. `short_code` (optional, `DUP-NG12`, trimmed) is set at registration,
  carried to a new version unless given, and names one current document (409
  `short_code_taken`). `DocumentOut` carries `municipality_id` and `short_code`, `DocumentList`
  the `municipality` {id, name}: multi-city scoping is data. `DocumentFileOut.redraw_pages`: the
  pages to redraw in QGIS (the PDF stage's raster sheets, see "PDF pre-processing"; null until
  read).
- **Files of a version** (`planning_document_files`, migration 0022): a version has any number
  of stored files, each with a `role`: `text` (read by the extraction job), `drawing` (the
  geometry job) or `both`; GIS files only as drawings. `DocumentIn.files` = `[{file_id, role}]`
  (none is fine; `file_id` alone still works = one text file). `planning_documents.file_id` /
  `file_key` / `page_count` stay the **primary** file (the first text / both PDF, re-chosen when
  files change; page images are then no longer served). `POST /v1/admin/documents/{id}/files`
  (201; 200 when every file was already on it: idempotent, the role kept), `PATCH
  .../files/{file_id} {role}`, `DELETE .../files/{file_id}` (only the current version; 409
  `items_accepted` once an item read from the file was approved / amended, `values_published`,
  `extraction_active`; its other open items are superseded, the stored file stays). Audited
  `document.file_attach`, `document.file_role`, `document.file_detach`. `DocumentOut.files[]`: per
  file its latest extraction run of the version + that run's job, `extraction_state` none |
  queued | extracting | retrying | ready_for_review | failed (the job decides while it exists) and
  `extraction_error`, the latest `process_geometry` job, review `items` read from it, scanned
  pages, `can_remove` / `remove_blocker`. `DocumentOut.state` (first match): no_files, processing,
  ready_for_review, failed, published, reviewed, not_extracted. The list filters `zone_id`,
  `state`, `job_state` (queued | running | succeeded | failed: a file's latest extraction or
  geometry job), `q` (name) and answers `total`. Tests:
  `tests/integration/test_document_files_postgis.py`.
- **Jobs.** `POST /v1/admin/documents/{id}/jobs/extract[?file_id=]` (one run per file; default
  the primary text file; 409 `drawing_file` / `not_a_pdf`) and `POST /v1/admin/files/{id}/jobs/geo`
  go through `jobs.enqueue.enqueue_job` (see "Background jobs"): one `pipeline_jobs` row
  (`extract_document` on `extraction`, `process_geometry` on `geo`; both run the PDF
  pre-processing stage first when a PDF's manifest is missing), committed, then the Celery
  message; the reply is 202 with `status_url = /v1/admin/jobs/{id}`, or **200 with the existing
  job** when an identical one is queued / running / retrying (idempotency key = type + target +
  file SHA-256; for extraction also the model and the prompt / schema versions, and a run that
  already finished answers 200 unless `?force=true`: see "Extraction job"). A dead broker marks
  the job failed and answers 503 with the job id. **The geometry job** (`jobs/geometry.py`,
  `GeometryRunner`): a GIS drawing of a current document version (GeoPackage, GeoJSON, zipped
  Shapefile: a QGIS redraw or the plan's official GIS) is read with GDAL, its contract layers
  (`plan_boundary`, `urban_parcels` with `urban_parcel_number` / `block_ref`, `urban_blocks`,
  `planned_land_use`; also the staged names `document_coverage`, `land_use`; a single-layer file
  is named by its file name) reprojected from the file's CRS (else the profile's
  `source_crs_epsg`) and staged through georeferencing's `stage_document` (snapping, validation,
  batches; source `gis_file`, method `native`, no fit); a refused dataset is recorded `invalid`
  and the job fails naming the errors. A PDF drawing gets the PDF stage (its raster sheets become
  the file's `redraw_pages`) and fails with what it needs: a QGIS redraw of the scanned pages, or
  the control-point georeferencing CLI for a vector sheet. A GIS file that is no document's
  drawing fails with that reason. These are final answers (`GeometryError`, never retried).
  Pre-processing and extraction run. Files and documents carry `preprocessing` (the manifest summary: vector / scanned pages,
  tables, chunks, sections) and `extraction` (the latest run: status, pages failed / skipped,
  items). Extraction writes to STAGING only; publishing is a separate job.
- **Coverage switch.** `PATCH /v1/admin/documents/{id}/coverage {live}` sets
  `planning_documents.coverage_live` (only the current version, only with a coverage geometry;
  409 otherwise). Location resolution and the panel take **adopted AND live** documents only
  (`locate_sql`, `panel_sql`; the urban panel's `covered` too). The sample's adopted documents
  are seeded live.
- **Audit.** Every action writes `audit_log` (actor subject + user id, action `file.upload`,
  `file.upload_duplicate`, `document.register`, `document.update`, `coverage.set_live`,
  `job.enqueue`, `job.enqueue_failed`, `zones.import` (actor `worker:import_zones`), entity type
  + id, details, request id). Listings are not audited.
- Responses are `Cache-Control: no-store`. Tests: `tests/test_admin_pipeline_unit.py` (upload
  validation, filenames, Celery dispatcher with `send_task` mocked, token helpers, role gate with
  staff sessions) and `tests/integration/test_admin_pipeline_postgis.py` (dedup, versions, jobs
  with a fake dispatcher, coverage switch vs locate, staff sessions, audit rows, job lifecycle).

## Admin configuration API (`api/services/admin_config.py`, `api/routers/v1/admin_config.py`)

- Role `admin`, every write audited (`api/services/audit.py`, shared with the pipeline API).
- **Financial assumptions** (`/v1/admin/assumptions`, migrations 0007, 0023): per zone or the
  municipality-wide row (`zone_id` null: range factors for market imports only): four rates
  (`land_rate` per m² parcel, `build_rate` / `design_rate` per m² GFA, `sale_rate` per m²
  saleable), each `{expected, low?, high?}` (absolute bounds, both or neither, `low ≤ expected ≤
  high`), the multiplier factors for rates without bounds, an optional `saleable_share` (0, 1]
  (the zone's default; null = 0.70), `source` (required: Realitica, Estitor, Monstat …),
  `source_date`, `notes`, `effective_from`. Rows are immutable **effective-dated** versions
  (`core.assumptions`): POST inserts version n+1 for the zone (`supersedes_id` = the previous
  version, `is_current` moves to it: the head of the history) applying from `effective_from`:
  today (the default, the municipality's local date, profile `timezone`) or later, at most five
  years ahead (422 otherwise; admins never backdate). A version's place on the timeline is
  `applies_from = GREATEST(effective_from, local day it was saved)` (so a market input dated to
  its reference period applies from its approval and keeps its stated date); the panels, POST
  `/v1/feasibility`, orders and the publish job's cells read, per zone, the version with the latest
  `applies_from` on or before today, the newest on a tie: a later-dated version is **scheduled**
  and needs no job to switch it on (the rule runs on the database's clock in every reader and the
  panel cache stamp). Every `AssumptionsOut` states `status` live | scheduled | superseded |
  retired, `applies_from` and `effective_to` (exclusive: the next version's `applies_from` on the
  zone's timeline, or the day it was retired; null = open-ended; `approved_by` is the version's
  `created_by`); the list answers `today` and `timezone` and by default the live and
  scheduled versions (`include_history` lists all). `POST /assumptions/batch {effective_from?,
  sets}` saves several zones in one transaction (all or nothing: the console's "Save changes"; a
  zone twice is 422); PUT creates the next version from the head with the given changes (409 on
  an older row; the date is never carried over); DELETE retires the **live** version only (409
  `not_live`; the zone then has no market figures until a later version applies — nothing is
  deleted). Each version writes `audit_log` with `before` = the previous version and `after` = the
  new one in the same flat column shape (the audit log lists exactly the figures that changed).
  The engine
  adapter turns absolute bounds into `absolute` engine bounds for that rate
  (`core/engine/feasibility.py`). Tests: `tests/integration/test_assumptions_schedule_postgis.py`
  (today's set on the next load, a future set waiting for its date and the cache key following,
  `effective_to`, audit old / new values, validation all or nothing, the formula versions).
- **Formula versions** (`GET /v1/admin/formulas`, admins; migration 0036 `formula_versions`, the
  pilot scope's `public.formula_version`): product-wide rows (the engine knows no municipality)
  with `label` (what the engine and every panel state as `formula_version`), `effective_from`,
  `is_current` (at most one: the formula the engine runs), `approval_note` (the client's approval;
  `poc-1` still awaits the fixtures' validation, P0 gate 3) and `engine_package`
  (`@urbanview/feasibility-engine`). Read-only: a new formula is a new engine release, fixtures and
  a migration adding its row, never an edit (no formula editor). The Financial assumptions
  screen names the current row (label, since when, the note on hover) next to the engine version.
- **Staff users** (`/v1/admin/users`): create (e-mail normalised to lower case, unique per
  municipality → 409; roles admin | reviewer | expert, the client's "expert reviewer" is
  `reviewer`), list with open session counts, PATCH role / display name / `is_active`
  (deactivating revokes the user's sessions; a user cannot deactivate or re-role themselves →
  409). No passwords anywhere: the magic-link login item issues sessions.
- Tests: `tests/test_admin_config_unit.py` (payload validation, adapter with absolute bounds)
  and `tests/integration/test_admin_config_postgis.py` (versions on the panel and the
  feasibility route, bounds in the ranges, users with session revocation, audit).

## Expert review and the audit trail (`api/services/review.py`, `api/routers/v1/admin_review.py`)

- 100% of AI-extracted planning information is reviewed before publication; textual accuracy
  matters as much as numerical. `GET /v1/admin/review` (roles admin and reviewer) pages
  STAGING (`planning_parameter_extractions`, migration 0008 added `entity_type`
  urban_parcel | zone | block | document (0037 dropped `market_data`: market inputs have their
  own queue), `zone_id` / `block_id`, `parameter_key` (the planning field key), `raw_text`,
  `confidence`,
  the reviewer's `amended_value_*` and `reviewed_by_user_id`). Filters: document, zone, status
  (`pending` = `pending_review`, approved, amended, rejected), entity, urban parcel, page. Each
  item: parameter labels and type, `extracted` (the AI value with unit), `amended`, `effective`
  (what would publish), `target`, `source` (document, page, bbox, note, raw text snippet,
  confidence, `extraction_method`) and a signed `link` to the cited page (null when storage or
  its credentials are unavailable: the queue still lists; same rule as the source
  viewer: `api.services.source.signed_page_link`), plus the extraction validator's `flags` and
  the item's `schema_version` / `prompt_version` (migration 0017); `?flag=low_confidence`
  filters (see "AI extraction contract"). For the admin console: `sort` pending (default: pending
  first, then file / page / parcel in natural order / field) | page (the same without the status)
  | confidence (pending first, lowest confidence first); every item carries `field_unit` (the
  dictionary's unit) and `run` (its extraction run's job id, model version, whole-run cost and
  items written); `GET /v1/admin/review/options?document_id=&field_key=` lists the wordings the
  document's items already carry for a text field, most frequent first (the land-use select of
  a correction). Each item also carries its staged `payload` (A2 check 2026-09-29), read with its
  schema version's reader: the value as printed (`stated_value` / `stated_unit`), the canonical
  value and unit, the `normalisation` rules, the counted `floors`, the `land_use_class`, the
  `table` cell (null for manual and seeded items). Tests:
  `tests/integration/test_review_queue_postgis.py`.
- **Decisions never overwrite the AI value.** `POST .../approve` accepts it (422 for a number
  that can never be the field's value: below its minimum, a percentage above 100,
  `corrections.refuse_impossible`: it is amended or rejected, never published), `.../amend`
  (`{value, unit?, note, confirm_out_of_range?}`) stores the correction alongside and sets
  `amended`, `.../reject` (`{note}` required) keeps it out and clears any correction. **A
  correction is checked with the extraction contract's rules** (`core/extraction/corrections.py`,
  A2 check 2026-09-29: "a non-numeric FAR or an unknown land use is rejected"): numbers in the
  document's conventions ("2,5", "1.906,09", "40 %") normalised to the field's canonical unit (ha →
  m² the only conversion); impossible values refused (below the field's minimum, a percentage
  above 100); a value above the field's plausible maximum (`FIELD_SPECS`: FAR 20, height 300 m …)
  only with `confirm_out_of_range`; floors in the plan's notation with the profile's tokens; a land
  use the document already uses (its items or served values) or the profile's land-use terms
  classify; texts trimmed, ≤ 500. A refusal is a 422 whose one problem names the rule (`type`
  not_a_number | below_minimum | above_maximum | out_of_range | unit_not_accepted |
  unknown_floor_notation | unknown_land_use | not_a_text | too_long, `ctx`); the audit row records
  the rules applied (`details.correction`). Notes are trimmed: the amend note and the rejection
  reason are required, a note of spaces is none (422). Any decision may be revised while the item is unpublished; an item
  with `published_value_id` is closed (409), so is a superseded one (409 `superseded`). Items of
  extraction runs carry `run_id`, `change` + `previous` (the previous run's item for the target
  and field) and `target.label` / `target.matched` (unmatched parcels stay text references);
  approving a newer reading retires the older approved item; the queue hides superseded items
  (`?include_superseded=true`) and filters `run_id`, `change` (see "Extraction job"). One
  decision per item: there is no bulk approval (the plan's "approve / amend / reject per item").
  `GET /v1/admin/review/summary` (and `DocumentOut.review`) gives pending / approved / amended /
  rejected per document and `can_publish` = no pending items and something approved; the
  publish job (separate item) checks it. Approved / amended items are eligible for publishing
  and still not served until published.
- **`audit_log` is append-only at the database level** (migration 0008: a trigger raises on
  UPDATE, DELETE and TRUNCATE for every role). Columns: actor (+ user id), action, entity type
  and id, `before` / `after` (entity-specific summaries), `note`, `details`, request id,
  created_at. Every review decision and every admin change writes a row through
  `api.services.audit.write_audit` (assumption versions with the previous
  and new figures, users, documents, coverage, jobs, files; order status joins when the
  payment item lands). `GET /v1/admin/audit` (admins only: the pilot scope's A7) lists the
  rows newest first (actor, action, entity type and id, before / after, note, created_at),
  filtered by action prefix and exact actor, 50 a page. Test fixtures never delete audit rows
  (integration tests read an entity's trail straight from `audit_log`, `tests.helpers.audit_trail`).
- **Geometry review** (`api/services/geometry_review.py`, `api/routers/v1/admin_geometry.py`,
  `core/geometry_qa.py`, migration 0033; the pilot scope's `staging.geometry_draft`, A2 check
  2026-09-29): staged geometry is reviewed like the values before the publish job may apply it.
  A draft is one staged batch (one layer of one producing run) with its `origin` (`vector_pdf`
  georeferenced sheets, `manual_qgis` QGIS redraws and the zones, `official_gis` a supplied GIS
  drawing and the cadastre), `document_id`, `dataset_version`, and validity QA computed when it is
  staged (georeferencing, zone and cadastral staging call `run_batch_qa`; the POC plan funds
  geometry validity only, no topology QA): errors `invalid_geometry` / `empty_geometry`
  (`qa_status` fail: it cannot be approved) and the producing run's own warnings (`georef.*`,
  `zones.*`, `cadastre.*`); `qa_issues` carry a sentence, counts and the feature keys.
  `GET /v1/admin/geometry` (admins, reviewers; filters status, origin,
  layer, document, dataset, QA; `include_history`; `counts` pending / approved / rejected /
  failing), `GET .../{id}`, `GET .../{id}/features` (simplified GeoJSON for the console's preview,
  each feature with the issue codes naming it), `POST .../{id}/approve` (409
  `qa_failed`; a batch staged before 0033 is checked on its first approval), `POST
  .../{id}/reject {note}` (final: status `rejected`, never published or carried; fix and stage
  again); one decision per batch, audited `geometry.approve` / `geometry.reject` (entity
  `geometry_batch`, before / after).
  `python -m core.geometry_qa check | recheck [--batch N]` prints / stores the QA. Batches published
  or superseded before 0033 have `review_state` null.
- Tests: `tests/test_review_unit.py` (payloads, notes, the staged payload, publish rule, page
  links), `tests/test_corrections_unit.py` (the correction rules) and
  `tests/integration/test_review_postgis.py` (queue payload, transitions with audit before /
  after, the contract's rules through the API, counters, append-only enforcement,
  audit listing, contract rows with flags and the flag filter),
  `tests/integration/test_geometry_review_postgis.py` (QA of invalid geometry and the run's own
  warnings, the queue and preview, decisions with audit and roles, publish waiting for and
  applying approved geometry only, per-batch decisions and the document filter).

## AI extraction contract (`backend/core/extraction/`, `docs/specs/extraction-contract.md`)

- **AI has one job: read the documents so people do not transcribe them.** It never invents a
  planning value and never does arithmetic, not even a unit conversion: the model **transcribes**
  (every field an `OutValue`: the value as printed, always a string; how the unit is printed;
  a verbatim `raw_text`; the page; optional table ref; confidence; `absent_reason` not_found |
  deferred), deterministic code types, normalises and checks it. `core.extraction.run.run_task`
  is one request: `prompts.build_prompt` -> `llm` model -> `response.RESPONSE_MODELS[task]` ->
  `validate.assemble` -> canonical `schema.ExtractionResult`; `staging.to_staging_rows` makes
  review-queue rows. The job that runs it per file: "Extraction job" below.
- **Canonical schema** (`schema.py`, `SCHEMA_VERSION = "1.0"`, JSON Schema exported to
  `core/extraction/schemas/`): entities `document` (name, `document_type` code = a profile key,
  `status` code, gazette, decision number / date ISO, `area_ha`, amendments, document-wide
  `rules`, notes), `blocks` (the plan's Blok / Zona, never UrbanView zones; `total_row` = sums),
  `urban_parcels` (number as printed + `parcel_key`, block, `planned_parcel_area_m2`, `rules`,
  `public_area_relation`, `other_conditions`), `utilities` (kind / status / scope),
  `land_use_legend`; `rules` = the Group 1 fields except the area (`fields.RULE_FIELDS`), always
  all present. A leaf is `StatedValue` (value in the canonical unit or the document's wording,
  `raw_text` **taken from the page**, `source` document + page + bbox bottom-left + table ref,
  confidence, `extraction_method` text | table | ocr, `stated` as printed, `normalisation` rules,
  `derived` floor counts, land-use `category`, `flags`) or `MissingValue` (`not_found`,
  `deferred` with its text and source, `unverified` = a model value whose text is not on the
  cited page or does not contain it; the candidate goes to `issues`). Land-use classes
  (`LandUseClass`) are product-wide; the wording stays the value.
- **Validation** (`validate.py`, `textmatch.py`): raw text on the cited page (case-, accent-,
  quote- and whitespace-insensitive, whole words; exactly one other page read -> `page_corrected`),
  value inside it, typing (`normalise.py`: numbers in the document's conventions in `Decimal`,
  ratio -> % and ha <-> m² only, `d.m.yyyy` dates, the floor notation counted with the profile's
  tokens, land use via the profile's term table or the document legend), plausible ranges
  (`fields.FIELD_SPECS`), confidence below `EXTRACTION_LOW_CONFIDENCE` (0.7). Flags never remove
  a value (`low_confidence`, `out_of_range`, `unit_assumed`, `bbox_ambiguous`,
  `found_under_other_parcel` ...); removed values are issues. Boxes come from the page's words;
  a repeated value is settled by its row label and column header, else no box. **The cited grid
  cell is tried first** (the page's own words inside the cell box): a short value (`A`, `1`) is
  not matched to its first occurrence elsewhere on the page, and a cell wrapped over several
  lines (interleaved with its neighbours in the page text) still verifies; a citation of the
  empty cell of a row a merged value spans resolves to the cell it is printed in
  (`pages.merged_origin`).
- **Prompts** (`prompts.py`, `prompt_sets/v1.1/`, `PROMPT_VERSION = "1.1"`, the only set):
  1.1 asks for **compact answers** (`compact.py`: per entity only the fields the pages state, flat
  entries without unions, `to_legacy()` turns them into the 1.0 response models the validator
  reads; the API refused 1.0's nested schema as too complex). Manifest + Jinja2
  templates (system rules, field guide, one file per task: document, block, urban_parcel,
  parameter_table keyed by urban parcel number, infrastructure, land_use_legend; context; user)
  + `responses/*.schema.json`, the structured-output schemas as sent (all objects closed, all
  properties required, no bounds). **Templates are place-neutral**: language, glossary, floor
  tokens, land-use terms and block label words come from `[extraction]` of the municipality
  profile (`core.municipality.load_extraction_profile`), document types from `[terminology]`
  (UP = urban project added for Stara Varoš). Two cached system blocks, pages in the user
  message. A changed prompt is a new directory; regenerate schemas with
  `python -m core.extraction export` (a test compares them).
- **Model** (`llm.py`): `ClaudeModel` (Anthropic SDK, `ai` extra, installed in the worker image
  only): streaming, `output_config` {format json_schema, effort}, adaptive thinking (no
  server-side refusal fallback: removed 2026-10-01, never switched on). **Default model Claude Sonnet 5** (product
  owner, 2026-09-26: Opus not required): reads the tables well, supports adaptive thinking and
  effort and caches the prompt; Haiku 4.5 has neither thinking mode nor effort and caches only
  4096+ token prompts; `claude-opus-5` stays one setting away for hard pages. Settings
  `ANTHROPIC_API_KEY`, `EXTRACTION_MODEL` (claude-sonnet-5), `EXTRACTION_EFFORT`,
  `EXTRACTION_ADAPTIVE_THINKING`, `EXTRACTION_MAX_TOKENS`, `EXTRACTION_TIMEOUT_SECONDS`,
  `EXTRACTION_LOW_CONFIDENCE`. `ModelUnavailable` /
  `ModelRateLimited` are retryable (the job maps them to `TransientError` / `RateLimited`);
  `ModelRefused`, `ModelOutputInvalid`, `ModelError` are final. `ScriptedModel` for tests.
- **Staging** (`staging.py`, migration 0017): only stated planning-field values become
  `planning_parameter_extractions` rows (`pending_review`), parcel values on their urban parcel
  (`parcel_key` lookup), block values on their block, document rules at document scope, each with
  `schema_version`, `prompt_version`, `extraction_method`, `flags` (JSONB), `payload` (the leaf +
  context, `StagedPayload`) and `job_id`; the rest is reported (`unstaged`, missing counts).
  **Stored items stay readable**: `read_payload` dispatches on the major version
  (`PAYLOAD_READERS`); keep the old reader when a major version changes.
- **Evaluation**: `cases.py` holds five never-guessed cases (FAR from coverage x floors, height
  from floors, area from GFA / FAR, a neighbour's parking rule, an adoption date from the plan's
  date). `python -m core.extraction eval` asks the configured model
  (costs tokens; a case passes only when the model returns not_found itself); accuracy on the
  client's documents is the corpus (below). Tests: `tests/test_extraction_contract.py` (schema files, prompts, normalisation,
  never-guessed, flags, staging, frozen 1.0 item) and
  `tests/test_extraction_llm.py` (request shape, reply, error classes, the wire request through
  the real SDK on a mock transport).

## PDF pre-processing (`core/extraction/preprocess.py`, `chunking.py`, `manifest.py`, `jobs/preprocessing.py`)

- Spec: `docs/specs/pdf-preprocessing.md`. `extract_pages(pdf)` (pymupdf, `PREPROCESS_VERSION`):
  per page the text blocks in reading order with boxes, the words with boxes, table grids
  (`lines_strict` -> `lines` -> a layout heuristic kept only when table-shaped; cell ids `rNcM`
  with boxes; cell text rebuilt in reading order, rotated headers included, a word or number
  wrapped in a narrow cell joined: "Površin" + "a UP", "1906.0" + "9"; leading rows without
  numbers are the header), page size / rotation, script (latin | cyrillic | mixed), and
  `scanned` (images cover ≥ half the page, ≤ 200 vector paths, no text layer or below
  `PREPROCESS_MIN_TEXT_DENSITY`), and (preprocess 1.2) `raster` by the week-1 assessment's own
  rule (`core.gis.sheets`: the largest image ≥ 60 % of the page and < 1000 vector paths, class C
  = redraw in QGIS; the assessment imports the same rule) with `largest_image_pct` and the path
  count; the manifest's `summary.redraw_pages` lists them (null for an older manifest, which
  then names its scanned pages). Boxes: PDF points, origin bottom-left, rotation undone. Text is
  kept as extracted (NFC; č ć š ž đ and Cyrillic unchanged); on pages with the AutoCAD glyph-id
  shift the shifted words are decoded with `core.gis` (block `decoded`); a word is taken as
  shifted only when it decodes to word-like casing and is not id-shaped (preprocess 1.1: Stara
  Varoš's `D3078` had been "decoded" to `aPMTU`). Table finding is skipped on drawing sheets
  (`PREPROCESS_TABLE_MAX_PATHS`, > A2). In the model's table view a merged cell (printed once
  for several rows) shows as `cM: ^rK` in the rows it spans below row K.
- **Never made up**: a scanned page is never read (OCR is outside the POC; the Tesseract option
  was removed on 2026-10-01): it stays unread, gets no chunk and is listed (`unread_pages`,
  `redraw_pages`) for a QGIS redraw or manual handling.
- **Stitching**: a header-less table with the columns of the table before it continues it and
  takes its column names (`continues`, `header_from`); its chunk shows those columns.
- **Sections** (methodology 2a–d) from headings (numbered / larger / bold / upper-case short
  blocks; running headers excepted) matched against `[extraction.sections]` of the profile
  (Cyrillic transliterated: `textmatch.fold` maps Cyrillic to its Latin spelling), plus a table's
  column names. **Chunks** (`plan_chunks`): one per page within `PREPROCESS_CHUNK_TOKEN_BUDGET`
  (chars / `PREPROCESS_CHARS_PER_TOKEN`), a table never split (alone over budget:
  `over_budget`), planning sections first (`priority` 0) with suggested extraction tasks;
  `chunk_pages` -> the contract's `PageInput` (verification `text` + `words`, grids, the `view`
  the model reads).
- **Stage** (`jobs.tasks.extraction.run_preprocess`, run first by the extraction job and by the
  geometry job for a PDF drawing; there is no separate trigger): skips the analysis
  when `stored_files.preprocess` (migration 0018) is current for the SHA-256, version and options
  key; else stores the page data as gzip JSON next to the upload and the manifest (pages,
  tables, chunk plan, `summary`) on the file record. No page images are rendered (removed
  2026-10-01: they were never served; the viewer highlights values on the PDF).
- Tests: `tests/test_extraction_preprocess.py` (a generated PDF, `tests/pdf_synthetic.py`: ruled
  table with wrapped cells, continuation, image-only page, Cyrillic, blank, glyph-shifted label;
  plus the POC documents when present) and `tests/integration/test_preprocess_postgis.py` (job
  through the API: manifest, document record, cache, force, the viewer's `#page=`, refusals).

## Extraction job (`jobs/extraction_runner.py`, `core/extraction/runs.py`, `docs/specs/extraction-job.md`)

- `POST /v1/admin/documents/{id}/jobs/extract[?force=true]` queues `extract_document` and one
  `extraction_runs` row (migration 0020) in the same transaction. **Idempotent** by document
  version + file SHA-256 + `EXTRACTION_MODEL` + `PROMPT_VERSION` + `SCHEMA_VERSION`: a run with
  that key in `ready_for_review` answers 200 with its job (nothing re-read; `force` reads again),
  an identical queued / running job answers 200 (`core.extraction.runs.run_dedupe_key`).
  Status: queued -> extracting -> ready_for_review | failed (`DocumentOut.extraction`,
  `StoredFileOut.extraction`, `JobOut.extraction_run`).
- **Pipeline** (`ExtractionRunner`): the run reads its own file (`extraction_runs.file_id`, one
  of the version's files; refused when it was taken off the version since), the manifest (the PDF
  stage first when missing or stale),
  every chunk whose plan suggests tasks, once per task (land-use legend first; chunks without
  planning content counted, not read; more than `EXTRACTION_MAX_CHUNKS` steps fail),
  `run_task` per step, targets matched to the document's urban parcels / blocks by
  `parcel_key` / `block_key`; **unmatched targets are staged as text references**
  (`target_label` as printed, `target_key`, no parcel id, flag `target_unmatched`, or
  `target_staged` for staged-but-unpublished geometry; the publish job never serves them).
  Items (`to_staging_rows(..., unmatched="stage")`) are written in one transaction at the end,
  `pending_review`, with `run_id`, `extracted_by = llm:<model version>`, prompt / schema versions,
  flags (+ `repeated_in_run`).
- **Failures:** transient model errors retried in the job with backoff
  (`EXTRACTION_CALL_RETRIES`, `EXTRACTION_RETRY_BASE_SECONDS` / `_MAX_SECONDS`), then by the job,
  which resumes from the steps checkpointed in `extraction_run_chunks` (never paid twice); an
  answer that does not fit is asked again once with the validator's error (`run_task(feedback=)`,
  `ModelOutputInvalid` carries the tokens it cost), then only that step fails and its pages are
  listed (`pages_failed`); refusals / bad requests fail the step. Every step failed, nothing
  readable, a changed or missing file: the run and the job fail. A manual retry re-reads failed
  steps only.
- **Re-extraction never deletes:** new items link to the previous reading of the target and
  field in the lineage (`previous_item_id`, `change` new | same | changed). On completion the run
  supersedes (`superseded_at`, `superseded_by_run_id`): pending items of earlier runs over the
  same file of the version (older prompt / schema / model; decisions stay) and every open item of
  older versions; the version's other files keep theirs (each file has its own runs; a file taken
  off the version had its open items superseded then); published, manual and seeded items never. Approving a
  newer reading retires the older approved item. Superseded items leave the queue, counters,
  `can_publish` and the publish job.
- **Tracking:** the run row (model version, versions, pages processed / skipped / failed,
  chunks, items written / low_confidence / unmatched / superseded, tokens, cost, summary), the
  job's progress and `result` (the summary) and cost block, `audit_log` `extraction.start` /
  `extraction.finish` (entity `extraction_run`, actor `worker:extract_document`). Nothing touches
  the serving tables. Live model runs need `ANTHROPIC_API_KEY` (`backend/.env`, git-ignored) and
  `ANTHROPIC_BASE_URL` (`anthropic_base_url`, default `https://api.anthropic.com`: the key never
  goes to a proxy the shell environment may name; the shell's own `ANTHROPIC_BASE_URL` beats
  `.env`, so run live commands with `env -u ANTHROPIC_BASE_URL`). **Since 2026-10-01 local and
  production call Anthropic directly** (`ANTHROPIC_BASE_URL=https://api.anthropic.com`, an
  `sk-ant-` service key, `claude-sonnet-5`; a never-guessed case read live through the adapter).
  From 2026-09-30 until then both ran through Cheaper Inference
  (`https://api.cheaperinference.com`, a `ci_live_` key; its Anthropic-compatible `/v1/messages`
  accepts `output_config`, adaptive thinking and effort unchanged; `claude-sonnet-5` at USD 1.40 /
  7.00 per MTok, 30 % under list; Stara Varoš p. 24 read live at 100 %): that pair is kept in each
  `.env.bak-20261001` (and as comments in the local `backend/.env`). The key and the endpoint are
  changed together: an Anthropic key must never be sent to the proxy, nor the proxy's key to
  Anthropic. Job costs are estimates from `LLM_PRICE_*`.
- Tests: `tests/test_extraction_job_unit.py` and `tests/integration/test_extraction_job_postgis.py`
  with `tests/extraction_script.Transcriber` (a scripted model that copies parameter tables).

## Extraction evaluation corpus (`core/extraction/corpus.py`, `scoring.py`, `harness.py`, `evalcli.py`, `backend/tests/corpus/`)

- The prompts are measured against the client's own planning documents: `tests/corpus/corpus.toml`
  lists them (path in the client folder mirror `docs/gis/source/`, git-ignored; SHA-256 pinned;
  per document the column map, row kinds, special cases and what it does not cover) and
  `tests/corpus/<id>/gold.json` holds **every urban parcel row × 12 fields** (the 11 Group 1
  fields + block) with printed value, canonical value, page and grid cell, block totals and the
  document identity. Corpus today: DUP Novi Grad 1 i 2 (12 pages, 103 parcels) and UP Stara
  Varoš (57 pages, 560 parcels). README: `tests/corpus/README.md`.
- **Gold sets**: `python -m core.extraction corpus label` drafts them deterministically from the
  PDF stage's grids (merged cells carried to every row they span; total rows numeric fields only;
  plan-wide totals left out), then every page is checked against the rendered PDF and recorded in
  `verification` (2026-09-26: an AI visual check of all 69 pages, every parcel row matched; a
  human reviewer's sign-off is still pending). A verified set is never redrafted without `--force`.
- **Scoring** (`scoring.py`) per gold cell: `exact`, `tolerance` (same canonical value printed
  differently, whitespace-only text differences), `wrong`, `missing` (false blank, incl.
  validator-removed values), `false_value` (**hallucinated**: a value where the document states
  none or defers, or any field of an invented parcel; must be 0), `correct_blank`; accuracy,
  stated accuracy (over the cells the document states), `wrong_page`, `wrong_cell` (the cited cell
  is not the gold cell), confidence calibration per bucket, parcels found / missed / extra,
  tokens and cost (USD list prices, `harness.PRICES_USD`).
- **Harness** (`harness.py`): the job's own pipeline (PDF stage, `plan_steps`, `read_step`,
  legend first, then steps concurrently); every model reply cached by request hash in
  `backend/.cache/extraction-eval/` (git-ignored: it holds document text), so unchanged requests
  are free and `corpus eval` without `--live` replays the cache (validator / scoring changes are
  re-measured at no cost). A run with failed model calls is `incomplete` and exits 1.
- **Log**: each run appends to `tests/corpus/results/log.jsonl` (prompt, schema and preprocess
  versions, git revision, model, effort, pages, scores, note) and rebuilds `RESULTS.md`; the full
  report with error examples goes to the cache folder. `corpus baseline` accepts a full run as
  `tests/corpus/baseline.json`; `corpus check` / `eval --check` fail on any hallucination, a new
  wrong page or accuracy half a point below the baseline.
- Results so far (Sonnet 5, effort high, prompt 1.1): Novi Grad 100 % of 609 stated cells, Stara
  Varoš pages 1-23 100 % of 1 386, no hallucinated value, no wrong page or cell. Pages 24-57 of
  Stara Varoš were not read (the API account ran out of credit); no baseline yet.
- Tests: `tests/test_extraction_eval.py` (outcomes, merged cells in the table view, cost, the
  regression check, manifest vs gold sets).

## Prepared planning values (`core/extraction/prepared.py`, `database/seeds/prepared_values/`)

- **The two POC plans are loaded without the model** (product owner, 2026-10-01: "add it to the
  code", cheaper than the AI run; BRD 2.6 lets input data be prepared by hand where that is
  faster). Later documents go through upload → AI extraction → review → publish as before.
- **Data files** `database/seeds/prepared_values/<municipality>/<corpus id>.json`
  (`PreparedDocument`): per urban parcel the stated planning values of the corpus gold set
  (deterministic table reader, column map of `corpus.toml`, checked against the page images; no
  model), each with canonical value and unit, the value as printed, page, the cell's box (PDF
  points, bottom-left) and a note (`UP 12 – <column header>`); blank and deferred cells are left
  out. A number above 1 in a column the corpus declares as ratios is that ratio (`served_number`:
  UP F3360/1 of Stara Varoš prints the site coverage index 1.2, served as 120 %, not 1.2 %; the
  build reports it, the planner should confirm it). `python -m core.extraction prepared build [--doc ID]` writes them from the gold sets and
  the PDF stage's grids (needs the source PDFs); a test holds the committed files to the gold
  sets. Today: Novi Grad 93 parcels / 450 values, Stara Varoš 560 / 2 800.
- **Building rows** (`[document.prepared] building_rows` of the corpus document,
  `merge_building_rows`; product owner, 2026-10-02): where the table lists the buildings of one
  parcel as rows of their own (Novi Grad's UP 51(a)–(c), 81(a)–(d), 85(a)–(f): one parcel each on
  the drawing), those rows are one prepared parcel. What the rows state alike is the parcel's
  value (area, land use, IZ, II: printed once over the group); a wording that differs per row
  (floors) is listed per building as printed (`(a) Po+P+3, (b) Pv, (c) P+1`), citing the
  buildings' cells together (note `UP 51(a), (b), (c) – Spratnost objekta`); a number that
  differs would be left out and reported. The gold set keeps the rows as the table prints them.
  Such a listing is text, not a floor notation: the height heatmap leaves that parcel out of its
  block's maximum, and the panel's height row wraps it.
- **Load** `python -m core.extraction prepared load --doc <corpus id> --document-id N [--by]
  [--note] [--dry-run] [--json]` (run it in the worker container): one transaction, STAGING only.
  The document must be the current version and hold a file with the data's PDF checksum (pages
  and boxes belong to that file). One `extraction_runs` row per load (`model = table-reader`, no
  job, `ready_for_review`, cost 0, `summary.data_sha256`); one item per value on its urban
  parcel (`parcel_key` match), **`approved`** with reviewer, time and note, `extracted_by =
  table-reader:<preprocess version>`, method `table`, no payload / confidence; `previous_item_id`
  and `change` against the previous reading of the target. Like a newer run it supersedes the
  pending items of earlier runs over the same file (the runner's `SUPERSEDE_SQL`), retires an
  older unpublished decision it restates, keeps a target that already holds the same approved
  value, and reports parcels without geometry (no item) and what differed. The same data file
  again never restates a parcel it placed before (a correction or rejection made since stands):
  it adds the parcels whose geometry was published in between (`PLACED_SQL`; Novi Grad's UP 14
  and UP 90 on 2026-10-02) as a further run and changes nothing when there is none
  (`already_loaded`). Audit: one `review.approve_prepared` row per load (entity
  `extraction_run`, review counters before / after, the summary). Nothing is served until the
  publish job runs.
- **Not an expert review:** the items say so in their note; the client's expert can check any
  value from its source link. Tests: `tests/test_prepared_values.py`,
  `tests/integration/test_prepared_values_postgis.py`.

## Orders (`api/services/orders.py`, `core/pricing.py`, `core/payments.py`, `api/services/order_mail.py`)

- **Guest checkout, no account, no password, no verification before purchase.** `POST /v1/orders`
  takes the location the panel showed (`{parcel_type, parcel_id}`), the pilot scope's guest form
  (first name, e-mail and telephone required for everyone, validated; last name optional; the
  purchaser type `individual` | `legal_entity`, whose company name and PIB `tax_number` are both
  optional and dropped for an individual; the first form's `contact_person` /
  `registered_address` are refused since 0031 and their columns dropped in 0037), the
  assumptions the visitor edited, an optional message and the `language` the map is in (en | me,
  optional: kept on the order, its e-mails are written in it). It
  answers 201 with the reference, the price, the turnaround, the bank-transfer instructions,
  `data_version`, `location.cadastral_parcel_id` and the public status URL
  (`ORDER_PUBLIC_BASE_URL` + `/orders/{reference}`, the public map's order page). Capped per e-mail
  address and day (`ORDER_MAX_PER_EMAIL_PER_DAY`, 429) on top of the per-IP limiter.
- **Customers** (migration 0031, the pilot's `public.customer`): `customers` holds one guest
  purchaser per e-mail address and municipality (`email`, `first_name`, `last_name`, `phone`,
  `company_name`, `company_id` = PIB), upserted by `POST /v1/orders` in the order's transaction
  (the latest name and telephone win; a company is kept until another is given); `orders.customer_id`
  points at it and the order keeps the details typed on it. The migration backfilled customers
  from the existing orders the same way.
- **Price is configuration, never logic:** `ORDER_PRICE_TIERS` (`"<max m²>:<EUR>,…,inf:<EUR>"`,
  BRD band EUR 50–200, tiers to be confirmed by the client; default = the mockup's 100 up to
  500 m² and 200 above) applied to the parcel's area basis (planned urban parcel area, else
  cadastral). `ORDER_TURNAROUND_BUSINESS_DAYS` gives `expected_by` (Mon–Fri, no holidays).
- **Reference** `UV-{KO}-{parcel}-{yymmdd}-{seq}` (`ko_short("Podgorica I") = "PODI"`), unique,
  retried on collision; an order on a planned parcel without a cadastral parcel has no KO part
  (`UV-UP-C2962-261001-01`, never `UV-UP-UP-…`). **Snapshot**: the full panel payload the visitor saw (with their edits),
  its `data_version` (label, and `orders.publish_version_id`: the `publish_versions` row with that
  label, the current one first; FK, SET NULL), market assumptions version and formula version,
  stored on the order so the expert works from what was shown even after a later publish; the
  staff detail returns it.
- **Status flow** `pending_payment → paid → in_progress → delivered`, `payment_failed` from
  pending_payment (it can still be paid: `payment_failed → paid`), `refunded` from paid /
  in_progress / delivered (a delivered order never goes back to work; the refund's amount, date
  and bank reference are on its audit row); anything else 409. `POST /v1/admin/orders/{id}/payment` (`received` → paid with
  amount / date / bank reference, also from payment_failed; `not_received` → payment_failed with
  the note, again on a failed order only records the check, 409 once paid; `refunded`),
  `.../assign` (an
  active `expert` user; only a paid order, which moves to in_progress, or one in progress:
  reassignment; 409 while the payment is due), `PATCH .../status` (delivered needs
  a report), `POST .../report` (PDF → private bucket as `stored_files.kind = expert_report`,
  sets delivered, e-mails a signed download link, `ORDER_REPORT_LINK_EXPIRES_SECONDS`). Every
  change is an `audit_log` row with before / after. Admins manage everything (reviewers have
  no order access: the pilot scope's roles); experts see and deliver only their assigned orders
  (403 otherwise).
- **Replacing a delivered report**: `POST .../report` on a `delivered` order replaces the report
  and needs a `note` (422 without one); the `order.report` audit row carries `version` (n-th
  upload) and `replaces_file_id`, no second status entry is written, and the new download link
  is e-mailed again. Earlier report files stay stored.
- **What the staff console reads** (`frontend/src/app/(shell)/admin/orders/`, see
  `frontend/CLAUDE.md` "Orders"): `GET /v1/admin/orders/{id}` adds `timeline` (the order's
  `audit_log` rows, oldest first: `order.create`, `order.payment` with `amount_eur`,
  `bank_reference`, `received_on`, `order.payment_check`, `order.assign`, `order.report`,
  `order.status`, a refund's with `refund_amount_eur`, `refunded_on`, `bank_reference`),
  `report_versions` and `location.cadastral_parcel_id` (the Parcel ID the map opens with
  `/?parcel=`, also for urban orders). The queue (`GET /v1/admin/orders`, newest first, filters
  status / assignee / search: reference, e-mail, first or last name, company, parcel) gives per order the reference, customer, `ko_and_number` and
  `planned_parcel` (from the order's columns and snapshot), `urban_parcel_id`, the `data_version`
  seen with its number `data_version_no` (`publish_versions.version_no` of the order's
  `publish_version_id`: the console writes `v12`, the label is free text typed at each publish),
  price, status, placed, `turnaround_business_days`, `expected_by`, `delivered_at` and the
  assignee. `GET /v1/admin/orders/experts` (admins; 403 for the others): the active `expert` users
  with their `open_orders` (in progress) for the assign picker.
- **Confirmation data** `GET /v1/orders/{reference}` (the pilot scope's "confirmation page data",
  public, `no-store`, reference case-insensitive): status + labels, location, pricing,
  turnaround, `payment_due` (pending_payment | payment_failed), `payment_instructions` while it is
  due (else null), `data_version` and `data_version_no` (the version's number, what the page
  shows), `status_url`; never personal data. The public map's order page
  `/orders/{reference}` (`frontend/src/app/orders/`) and the reloaded S5 confirmation read it; the
  confirmation and every order e-mail link to the page. There is no compact status variant.
- **The public map's flow** (`frontend/src/components/order/`): S4 order modal from the parcel
  panel (location carried through with the planned parcel and the data version, fee and
  turnaround from `GET /v1/orders/pricing`, inline validation with the API's rules),
  `POST /v1/orders`, S5 confirmation with the
  bank-transfer instructions on screen and `?order=<reference>` in the address bar (a reload
  shows it again from `GET /v1/orders/{reference}` with the order's status as it is now: a
  delivered or refunded order says so, `lib/order-status.ts`); `order_started` / `checkout_completed
  {order_id: <reference>, amount_eur}`. Legal pages are not in the POC plan: none is built (the
  client's lawyer supplies the copy).
- **Pricing for the panel** `GET /v1/orders/pricing` (public, configuration only, `Cache-Control:
  public, max-age=300`): `{currency, tiers: [{up_to_m2, price_eur}], turnaround_business_days}`;
  the public map shows a parcel's price by applying `core.pricing.price_for`'s rule to the panel's
  `basis_area_m2`, which is the area `POST /v1/orders` prices from.
- **E-mail**: `payment_instructions` on creation and `order_delivered` on report upload are
  `send_email` jobs queued through `api.services.email.EmailService` (see "Transactional
  e-mail"); the reply's `email_status` is `queued` (or the final state when the job already
  ran), and `suppressed` at once when the sending policy will not mail the address (no
  `SMTP_HOST`, staging without an allow-listed address: `EmailService.will_send`, the worker's
  `core.mail.policy.decide` on the same settings), so the confirmation never announces a mail
  the worker is going to drop; a queue outage marks the `email_log` row failed and the order
  stands. The staff order
  detail lists `emails` and the queue shows `email_alerts` (failed sends).
- **Payments**: `core/payments.py` holds `BankTransferProvider` (instructions from `ORDER_BANK_*`,
  no online step; hosted checkout, card providers and webhooks are not in the POC plan, so there
  is no provider seam). No card data anywhere. `customers` and `orders` are the only tables with personal data.
- Tests: `tests/test_orders_unit.py` (tiers, turnaround, references, transitions, form
  validation, e-mail templates) and `tests/integration/test_orders_postgis.py` (creation with
  snapshot and e-mail, pricing from config, the status flow with guards, expert scope, report
  delivery, snapshot immutability after a republish, public status without personal data) and
  `tests/integration/test_orders_console_postgis.py` (experts list and its 403, a replaced report
  with its note, version and second e-mail, payment and refund details in the timeline).

## Transactional e-mail and staff login (`core/mail/`, `jobs/tasks/email.py`, `api/services/email.py`, `api/services/auth.py`)

- **Only the job sends.** `EmailService.queue(template, to, order_id | user_id, language?)`
  inserts an `email_log` row (`queued`) and one `send_email` job (email queue) whose payload
  carries ids only (and the language of a sign-in link). The worker (`jobs.tasks.email.deliver`) loads the row, resolves the recipient and the
  facts from the order or the staff user at send time (`core.mail.repository`), renders the
  template, applies the sending policy, sends over SMTP and records the outcome on the row:
  `sent` with `provider_message_id` (the id in the provider's 250 reply, else our
  `Message-ID`) and `provider_response`, `suppressed` with `suppressed_reason`, `failed` with
  `error` after the retries (`attempts` counted). Transient SMTP trouble (connection, timeout,
  4xx) is `TransientError` → backoff retries (`JOB_MAX_ATTEMPTS`); authentication failures,
  5xx and refused recipients are permanent. A message that cannot be built (the recipient was
  deactivated since, the report link cannot be signed) ends `failed` too, never left `queued`.
  Bodies are never stored, the subject is.
- **Templates** (`core/mail/templates/*.j2`, Jinja2, `StrictUndefined`, HTML auto-escaped):
  `payment_instructions` (reference, location, price, beneficiary / IBAN / bank / SWIFT /
  amount / payment reference, turnaround + expected date, status URL, support inbox),
  `order_delivered` (reference, location, signed download link + expiry, support inbox),
  `magic_link` (login URL, expiry minutes, single-use note). Wording provisional until the
  client approves it. Contexts come from `api/services/order_mail.py`; `core.mail.render` checks
  the required keys.
- **One language per message** (product owner, 2026-10-02; before, every message carried
  Montenegrin then English): `render(template, context, language=)` with the app's languages
  (`core.mail.templates.LANGUAGES`: en | me), every template holding both wordings. The language
  is the app's at the moment the e-mail is asked for (`mail_language`, first that is known):
  the job payload's `language` (a sign-in link: `POST /v1/auth/magic-link {email, language?}`,
  the console's language cookie), else the order's (`orders.language`, migration 0040, from
  `POST /v1/orders {…, language?}`: the map's language at the order, so the e-mail that brings
  the report days later is in it too), else `MAIL_DEFAULT_LANGUAGE` (en: orders placed before
  0040, the CLI). Anything but en / me in a request is a 422.
- **Transport and policy** (`core/mail/smtp.py`, `core/mail/policy.py`): `SMTP_HOST` / `PORT` /
  `USERNAME` / `PASSWORD` / `USE_TLS` (STARTTLS) / `USE_SSL` (465) / `FROM` / `TIMEOUT_SECONDS`,
  `MAIL_REPLY_TO` (default `ORDER_SUPPORT_EMAIL`), `MAIL_APP_NAME`, `MAIL_DEFAULT_LANGUAGE`.
  No `SMTP_HOST` → every row
  `suppressed (no_smtp_host)`. `APP_ENV=staging` mails only `MAIL_ALLOWLIST` (addresses or
  `@domain`; empty = nothing goes out); a non-empty allow-list is enforced in every environment.
  Dev: compose's Mailpit (SMTP 1025, inbox http://localhost:8025); without Docker
  `python -m core.mail.devsink` (SMTP 1025, each message an `.eml` file in the temp folder's
  `urbanview-mail`) with `SMTP_USE_TLS=false`. Deliverability check:
  `python -m core.mail.testsend --template payment_instructions --to you@… [--lang me]` sends
  fixture data through the real provider (DKIM / SPF / DMARC are the provider account's job).
- **Log**: the `email_log` rows (queued | sent | suppressed | failed). Orders show
  `email_alerts` in the queue and `emails` in the detail; there is no separate log route and no
  bounce tracking (provider webhooks are not in the POC plan; 0036 dropped the bounce columns).
- **Magic-link login** (`api/routers/v1/auth.py`, public): `POST /v1/auth/magic-link {email,
  language?}` always answers 202 with the same neutral message, **before** anything is looked up: the lookup,
  the audit row and the e-mail job run after the response (Starlette background task; a failure
  there is logged, never shown), so neither the body nor the time taken tells a staff address
  from any other (auth check 2026-09-29); an active staff address gets a `magic_link`
  e-mail whose token the job mints (`staff_login_tokens`: sha256 hash, `MAGIC_LINK_EXPIRES_SECONDS`
  = 900, `used_at`), link `{ADMIN_BASE_URL}/login?token=…`. `POST /v1/auth/magic-link/exchange
  {token}` consumes it once and returns a staff session bearer token (`staff_sessions`,
  `STAFF_SESSION_DAYS`, default 1: as long as the console's sign-in, `AUTH_SESSION_MAX_AGE` 24 h;
  `core.staff token --days` for scripts, default 1) with the user; 401 for unknown / used /
  expired. While a link e-mailed within `MAGIC_LINK_MIN_INTERVAL_SECONDS` (60) is still unused,
  asking again sends no second e-mail (same 202; audited with `details.reason = link_pending`).
  Audited
  `auth.magic_link_requested`, `auth.login`. Without SMTP (a fresh server) `python -m core.staff
  login-link --email … [--create --role admin]` mints the same single-use token from the command
  line (no `email_log` row) and prints the link once; audited `auth.login_link_issued` (actor
  `cli`); `tests/integration/test_staff_login_link_postgis.py`.
- **The admin console** (`frontend/`, `/admin/*`, see `frontend/CLAUDE.md`) signs staff in with
  these links through Auth.js (`ADMIN_BASE_URL` = site + `/admin`, so links open
  `/admin/login?token=…`): `GET /v1/admin/users/me` (every staff role) answers the principal
  (`id`, `email`, `display_name`, `role`, `subject`, `via` session | token) the console takes its
  role from; `POST /v1/auth/sign-out` (bearer) revokes that staff session (204 whatever the token,
  audited `auth.logout`). There is no Overview dashboard (not in the POC plan): the console opens
  on Documents (Orders for an expert). After the link is exchanged the sign-in page loads the
  console as a new page (never a redirect out of the server action: behind the reverse proxy
  that left the bar without its tabs and account menu until the next page load, 2026-10-03).
  Tests: `tests/integration/test_admin_console_postgis.py`.
  Temporary: with `ADMIN_OPEN_ACCESS_TOKEN` set in the web container (an admin entry of
  `ADMIN_API_TOKENS`) the console skips the sign-in and serves every visitor as that admin, for as
  long as the server cannot mail the links (`frontend/CLAUDE.md`, "Open access"); off on the live
  server since 2026-10-02, when its SMTP account was set.
- Tests: `tests/test_mail_unit.py` (every template against fixture data, policy, MIME, provider
  ids, the job body on the in-memory repository) and `tests/integration/test_mail_postgis.py`
  (through the API with eager Celery and a transport double: log rows with provider ids, jobs,
  magic-link round trip, staging allow-list, retries then failure).

## Analytics (`api/services/analytics.py`, `api/routers/v1/events.py`, `admin_analytics.py`)

- The prototype is a validation instrument: `POST /v1/events` ingests batches (1 to 100) of the
  13 product events into `analytics_events` (migration 0005; model `core/models/analytics.py`,
  whose `AnalyticsEvent` is the one list: the API's enum and `ck_analytics_events_name` (0038);
  indexes on municipality + name + time, session, zone). Each event: `name`, anonymous
  client-generated `session_id`, optional anonymous persistent `client_id` (repeat usage),
  optional `event_id` (retried batches are de-duplicated, never errors), tz-aware `occurred_at`
  (not in the future), and a small flat `properties` object.
- **Rows are judged one by one** (`EventBatch`'s wrap validator): a malformed row (unknown name,
  malformed id, bad or personal properties, a timestamp in the future) is rejected and listed in
  the reply's `rejected` (`index` + `problems`: `loc`, `msg`, `type`; the sent values are never
  echoed) while the valid rows are stored (202). Only a malformed batch (no `events` list, 0 or
  more than 100 rows, unknown top-level keys) or a batch whose every row is malformed is a 422.
- **Append-only** at the database level (migration 0030, as `audit_log`: a trigger raises on
  UPDATE, DELETE and TRUNCATE for every role); the API only inserts. The map's `session_id` and
  `client_id` are UUID v4 (the pilot scope's `session_id uuid`; the id rule `^[A-Za-z0-9_-]{8,64}$`
  still accepts older 32-hex ids). The pilot technical scope's names map onto these:
  `POST /api/events` = `POST /v1/events`, `public.analytics_event` = `analytics_events`,
  `event_name` = `name`, `zone_id` = the `zone_id` column, `planned_parcel_id` =
  `properties.urban_parcel_id`, `layer_key` = `properties.layer_id`, `lat` / `lng` / `props` =
  `properties` (kept after the analytics check of 2026-09-28).
- **Never personal data.** Unknown names, malformed ids, nested / oversized properties, a
  denylist of keys (name, email, phone, ip, user_agent, address …) and string values that look
  like an e-mail or IP address reject the row. Request IPs are never stored.
  Known properties are typed (`parcel_id`, `zone_id`, `document_id`, `page` … positive ints;
  `search_kind` ∈ address | click | parcel_number, `result` ∈ address | zone | parcel, booleans
  `matched` / `recent` / `visible` / `on`; `panel_type`; `amount_eur` ≥ 0; `lat` −90…90 and
  `lng` −180…180, where a search landed, which the map sends rounded to 4 decimals; `sessions`;
  `coverage` ∈ covered | no_parcel | uncovered | failed on `search_performed`: what a point
  search, map click, parcel lookup or zone pick found, so an outside-coverage hit (S6) is
  `uncovered`) and some are required (`search_performed.search_kind`, `layer_toggled.layer_id`,
  `source_reference_opened.document_id + page`, `checkout_completed.amount_eur`). `zone_id` and
  `parcel_id` are copied into columns for grouping (no FKs).
- `GET /v1/admin/analytics?from=&to=` (role `admin`; `[from, to)`, default last 30 days, max
  366; `from` = `to` is an empty range, `from` after `to` a 422) returns, grouped in SQL (one
  statement per aggregate, percentages in Python, 1 decimal, 0 when there is nothing to divide
  by, so an empty range answers zeros): the **funnel** map_loaded → parcel_resolved
  (`parcel_selected`) → panel_opened (`panel_viewed`) → order_started → order_submitted
  (`checkout_completed`) → paid (the order its `order_id` names has `orders.paid_at`), a session
  counting at a step when it emitted that step's event and every earlier one in the range, with
  the conversion from the previous step and from the start; **orders by status** (the orders
  placed in the range, every status in flow order with count and summed price; no customer
  data); **top zones** (`search_performed` + `parcel_selected` by zone: the event's `zone_id`,
  else the smallest zone containing its `properties.lat` / `lng`, so a search outside coverage,
  which locate answers with `zone: null`, counts for the district it was made in (BRD §2.10
  location demand; S6 check 2026-09-29); each row carries `covered` (`core.coverage.ZONE_COVERED`)
  and `uncovered_searches`; a hit in no zone is the `zone_id: null` row); **uncovered hits**
  (`search_performed` with `coverage: uncovered` grouped by `lat` / `lng` at 3 decimals, ≈ 110 m,
  the 20 most frequent); **repeat sessions** (anonymous visitors (`client_id`) with 3+ sessions in
  the range and their sessions); **intent counts** (`market_data_interest` / `ai_interest` events
  and sessions). No names or e-mails. The admin console shows it on `/admin/analytics` as plain
  tables (the pilot scope's A7, admins; see `frontend/CLAUDE.md`). Responses are
  `Cache-Control: no-store`.
- **Role gate** (`core/auth.py`, `api.deps.require_role`): `ADMIN_API_TOKENS` =
  `token:role[:subject],...` (roles admin | reviewer | expert; validated at startup). The admin
  tool's server side sends `Authorization: Bearer <token>`; missing / unknown → 401 with
  `WWW-Authenticate: Bearer`, wrong role → 403 with `required_roles`. No tokens configured =
  every staff route answers 401. Constant-time comparison; tokens are never logged.
- Tests: `tests/test_events_ingest.py` (the 13 names, row-by-row validation, storage,
  de-duplication with a fake repository), `tests/test_admin_analytics.py` (gate, assembly of
  every aggregate from canned rows, zeros, ranges), `tests/integration/test_analytics_postgis.py`
  (SQL of every aggregate on a crafted event set and a paid order, zeros, index use).

## Background jobs (`jobs/`, `api/services/jobs.py`, `api/routers/v1/admin_jobs.py`)

- **One job system.** Every long-running task is a `pipeline_jobs` row (migration 0010: `type`
  extract_document | process_geometry | publish_approved | send_email | import_market_data |
  refresh_heatmaps | import_zones (0032; 0036 dropped preprocess_file and ai_check), `kind`
  family, `queue`,
  `target_type` document | file | publish_run | email + `target_id`, `payload`, `status` queued |
  running | retrying | succeeded | failed | cancelled, `attempts` / `max_attempts`,
  `manual_retries`, `next_retry_at`, `dedupe_key`, `wall_time_ms`, `llm_model`,
  `llm_tokens_in/out`, `estimated_cost_eur`) delivered to a worker as `(job_id, municipality_id)`.
  Celery app `jobs/celery_app.py`: queues `default`, `extraction` (LLM, PDF pre-processing),
  `geo` (geometry),
  `publish`, `email`, routed by task module; tests run tasks inline by setting Celery's own
  `task_always_eager` on `celery_app.conf`.
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
- **Tasks** (`jobs/tasks/`): `extract_document` (extraction queue),
  `process_geometry` (geo), `publish_approved` (publish; one active run per municipality),
  `send_email` (email; payload `{template, to, context}`, `to` a reference resolved at send time,
  never a stored address), `import_zones` (geo; a zone GeoPackage from QGIS). `process_geometry`
  stages a document's GIS drawing and answers a PDF drawing with what it needs (see "Staff
  pipeline API" Jobs); `extract_document` runs (see "Extraction job").
- **API** (reads: roles `admin` and `reviewer`; retry: `admin`): `GET /v1/admin/jobs` (filters `type`, `status`, `target=document:12`
  | `file:` | `publish_run:` | `email:`, `document_id`, `file_id`; `total`),
  `GET /v1/admin/jobs/{id}` (the status URL), `POST /v1/admin/jobs/{id}/retry` (failed / cancelled →
  queued, attempts reset, `manual_retries` + 1, audited `job.retry`, re-dispatched; 409 when not
  retryable or while a job for the same key is active). Every `JobOut` carries a `cost` block.
- **Run it.** `make worker` / `poe worker` (`-Q default,extraction,geo,publish,email`),
  compose `worker` service from
  `backend/Dockerfile.worker`. Tests: `tests/test_jobs_unit.py` (backoff, cost, keys, lifecycle
  on the memory store, a real task in eager mode) and `tests/integration/test_jobs_postgis.py`
  (SQL store, idempotent API, listing, retry, cost per job, eager task through the API).

## Publish pipeline (`jobs/publish_pipeline.py`, `jobs/publish_layers.py`, `jobs/tiles.py`, `api/services/publish.py`)

- **One button.** `POST /v1/admin/publish` (roles admin, reviewer; body `{label?, notes?}`)
  refuses with 409 `reason = pending_review` and `details.documents` (id, name, pending count)
  and `details.geometry` (staged batches waiting for the geometry review) while any document has
  items pending review or any staged geometry waits for a decision; otherwise it queues one
  `publish_approved` job
  (202; 200 with the active job while one is queued / running: key
  `publish_approved:publish_run:-`, `max_attempts = 1`). `GET /v1/admin/publish` is the status
  screen: `current` (label, who, when, counts, layers, signed archive link), `versions` (every
  version, newest first, with its `version_no` and tiles key `archive_key`), `active_job` /
  `last_job` with `progress` (`step` + one entry per step: pending |
  running | done | failed, timestamps, detail), `can_publish`, `blockers`,
  `geometry_blockers`, `keep_versions`.
- **The job** (`PublishPipeline.run`, one database transaction from preflight to flip, so
  visitors see the previous version until the commit and a failure leaves nothing behind: an
  archive already uploaded is deleted again):
  `preflight` (pending items or geometry pending review = hard failure; superseded items never
  count or publish) →
  `version` (new `publish_versions` row, not current, numbered `version_no` 1, 2, 3 … per
  municipality, 0034) → `values` (the previous version's `planning_parameter_values` carried
  forward for current document versions, overridden by approved / amended items: amended value
  wins, unit `COALESCE(amended, extracted)`, every row cites the item's page; items closed with
  `published_value_id` and `published_version_id`; items without a page / value or with a
  parcel–document mismatch are listed in `result.skipped_items` and stay open; first the step
  links the items read before their parcel or block existed (text references with a
  `target_key`) to the parcels and blocks the document has now, `items_linked`: without it
  they could never be served;
  expert-rejected fields that nothing replaced become
  `planning_value_gaps` rows of the version, counted as `values_rejected`, so the parcel panel can
  say `rejected` without reading staging)
  → `geometry` (the staged batches a reviewer approved, see below) → `links` (`parcel_links`:
  cadastral ↔ planned
  overlaps with locate's thresholds, `rank 1` = the panel's primary: largest overlap, smallest
  planned area, lowest id, the relation of every cadastral parcel, `none` rows; the step reports
  `cadastral_unmatched`, the parcels per relation and the recompute time, stored on the version
  as `links_summary`) → `cells` (the heatmaps, `core.choropleth`, see "Heatmaps") → `export`
  (one newline-delimited GeoJSON file per catalogue layer) → `tiles` (tippecanoe per layer with
  its own zoom range, `tile-join` into one PMTiles archive: one source layer per map layer,
  independent visibility) → `upload` (`{m}/tiles/{version_id}/{label}.pmtiles`, private bucket)
  → `flip` (`is_current`, archive columns, `counts`, `layers`, `duration_ms`, audit
  `publish.complete`, commit) → `prune` (retention, best effort). The result carries counts,
  layers, skipped items, pruned versions and the duration; progress is written per step.
- **Layer catalogue** (`jobs.publish_layers.LAYERS`, data not code): `zones` (with `zone_type`
  and `covered` = the zone has an adopted, live, current document with coverage; the map colours
  covered zones by type and draws no other zone), `zone_labels` (one `ST_PointOnSurface` point per
  zone, same properties; point layers are built with `--drop-rate=1` so no label is thinned out),
  `document_coverage`, `urban_blocks` (inside coverage only), `urban_parcels` (with the effective parameters:
  parcel → block → zone → document scope, plus `max_gfa_m2`, and `zone_id` = the plan's zone,
  else the block's: the urban panel's rule, so a click names its zone), `cadastral_parcels` (with
  `has_urban_parcel`, `no_urban_parcel`, `relation`, `reduction_pct`,
  `primary_urban_parcel_id`, `overlap_fraction`, `area_delta_m2`, the
  `zone_id` / `zone_type` of the zone containing the parcel's point on surface, and `covered` =
  that point lies in a live coverage, locate's rule: the map draws covered parcels only),
  `land_use` (every planned parcel with a published land use: `name` = the plan's wording,
  `category` = the map's colour group res | com | mix | pub | grn that `core.land_use` derives
  from it with the profile's `land_use_terms` and `land_use_codes`, passed to the layer's SQL as
  `:land_use_categories`; plus the staged generic `layer_features` polygons that are not one of
  those parcels (a parcel's staged polygon is keyed `<document id>|<urban parcel number>`, as
  georeferencing stages it, and matched by that key, so a parcel is drawn once: from its
  published land use, else from its staged polygon), with their own `category`, else the class
  of their name or code; an unclassed wording has no `category` and takes the layer's neutral
  colour; 2026-10-02: before, every
  feature was the neutral colour because nothing wrote a category), `heat_coverage`,
  `heat_far`, `heat_height`,
  `heat_gfa` (every covered urban block), `heat_sale_price` (every covered zone): the heatmaps;
  outside coverage the archive carries no cell (S6: the base map alone). Empty
  layers are left out of the build but listed with 0 features.
- **Zone type** (migration 0014): `zones.zone_type` res | com | mix | pub | grn (CHECK) or null
  (not classified: drawn neutral, never a guessed colour); from the seed or the staged `zones`
  feature's `zone_type` property (invalid values ignored).
- **Staged geometry** (`geometry_batches` + `staging_geometry`, the GIS ingestion contract in
  `STAGED_LAYERS`): one batch per (file, layer) with `status = staged`; features carry a natural
  `feature_key` and JSON `properties`. Since 0033 a batch carries `origin`, `qa_status` /
  `qa_issues` and a reviewer's decision (`review_state` pending_review | approved | rejected, see
  "Geometry review"); the geometry step applies approved batches only and a rejected batch leaves
  the staged pool (status `rejected`), so the land-use carry-forward never picks it up. Entity layers are upserted by natural key so UrbanView
  ids stay stable (cadastral: KO + number + sub-number; urban parcels: document + number, block
  by `block_ref`; blocks: `block_ref`; zones: `name`; `document_coverage`: `document_id` →
  `coverage_geom`); no deletes. The generic layer (`land_use`; planned traffic is an MVP layer,
  never staged) is copied into `layer_features` for the version (the newest staged batch wins, older ones `superseded`;
  layers without a new batch are carried forward). Batches end `published` with
  `published_version_id`.
- **Versions.** Values, `layer_features`, `parcel_links`, `choropleth_cells` /
  `choropleth_classes` and the
  archive are per version; entity geometry is upserted in place (a geometry rollback needs a
  re-ingest: documented limitation). There is no rollback endpoint or button (not in the POC
  plan): earlier versions keep their rows and archive so an operator can point `is_current` at
  one by hand. Published values never change:
  a trigger refuses UPDATE on `planning_parameter_values` (0034). Retention
  `PUBLISH_KEEP_VERSIONS` (3, ≥ 2): after a publish, versions beyond the newest N lose their
  archive object and derived rows (`archive_pruned_at`); version rows and values stay for history.
  Never the current or the
  previous version.
- **Tiles pointer.** `GET /v1/tiles/current` (public, `no-store`): `status` published |
  unpublished, `data_version`, `version_id`, `version_no`, `published_at`, the tiles key
  `archive_key`, one signed `archive_url`
  (`TILES_URL_EXPIRES_SECONDS`, PMTiles range requests) + `expires_at`, `layers`, `min_zoom`,
  `max_zoom`, `cell_classes` (the heatmaps' stored classes per layer) and
  `heatmaps_refreshing`. The seeded version has no archive (`archive_url: null`).
- **Run it.** The worker image builds tippecanoe 2.79 (`backend/Dockerfile.worker`,
  `TIPPECANOE_BIN` / `TILE_JOIN_BIN`); `TILES_MIN_ZOOM` / `TILES_MAX_ZOOM` clamp the catalogue's
  per-layer ranges; `PUBLISH_TMP_DIR` for the scratch files. Tests: `tests/test_publish_unit.py`
  (catalogue, the heatmap layers, labels, tippecanoe commands) and
  `tests/integration/test_publish_postgis.py` (through the API with eager Celery, a fake tile
  builder and storage: refusal naming the document, the amended value in the panel and the tile
  layer, cells and links, staged geometry, idempotency, retention). Tests wanting a
  clean pointer reset version 1 to current and delete newer versions.

## Heatmaps (`backend/core/choropleth.py`, migration 0027)

- **The surfaces of BRD §2.1**, one tile source-layer each: `coverage`, `far`, `height`, `gfa` per
  urban block from the published planning values, `sale_price` per zone from the assumptions. A
  parcel's value is its effective one (parcel → block → zone → document, the tiles' precedence),
  so a block-level figure the plan states applies to every parcel of the block. Rules per field:
  coverage % (IZ) and FAR (II) = **area-weighted mean** over the block's planned parcels that
  state them (weights: the planned parcel areas, as the plan states them, else as drawn:
  `jobs.publish_layers.PLAN_AREA`); height = the **maximum** floors above ground,
  parsed from the plan's notation with the profile's `[extraction.floor_notation]` tokens
  (`P+4` = 5, `P+5+Pk` = 7; `label` keeps the notation; a text the tokens do not read, such as
  a per-building listing of a prepared parcel, is not counted); GFA = **sum** of FAR x planned
  parcel area (the engine's formula). Sale price = the expected €/m² of the zone's assumptions version
  that applies today, exactly as stored, with low / high (absolute bounds, else expected x
  factors).
- **`choropleth_cells`** (per version, layer and cell): `value`, `value_low` / `value_high`,
  `value_band`, `unit`, `label`, `parcel_count`, `source_kind` planning | assumptions,
  `dataset_version`, `assumptions_id` / `assumptions_version`. Only covered land has cells
  (`core.coverage`: blocks whose point on surface lies in a live coverage, zones with a live
  document; a market set for an uncovered zone gives no cell, and the staleness check that queues
  `refresh_heatmaps` looks at covered zones only). A covered block or zone without a value has no
  row (absence is data); the tiles still carry it, without `value`, so the map draws it with the
  "no data" hatch, never as zero; an uncovered one is not in the tiles at all.
- **`choropleth_classes`** (per version and layer), stored with the cells so the legend and the
  tiles agree: quintile breaks of the version's values for the planning layers (rounded,
  de-duplicated), the profile's fixed bands `price_band_breaks_eur_m2` (1300 / 1700 / 2100) for the
  sale price with 0 = not saleable; min / max / mean, count, `null_count` (blocks / zones without
  a value). `value_band` = the cell's legend row: the number of breaks <= value (sale price: 0 =
  not saleable, then 1 + that number); the tiles carry `band` (and `band_low` / `band_high` for
  the sale price) and the map colours by it. `GET /v1/tiles/current` serves the stored classes as
  `cell_classes`.
- **Runs** as the publish job's `cells` step after the parcel links. The **`refresh_heatmaps`**
  job (publish queue) recomputes the current version's cells and rebuilds its archive in place
  (same key: signed links stay valid, PMTiles readers reload on the new ETag), audited
  `heatmaps.refresh`. It is queued when the sale-price cells no longer match the assumptions that
  apply today: right after an admin saves or retires a market set (`AdminConfigService`'s hook),
  and when `GET /v1/tiles/current` finds them stale (a scheduled set took effect at midnight; the
  pointer then says `heatmaps_refreshing: true`); one per version, not again within 10 minutes of a
  failed one; only for a version with tiles. QA: `python -m core.choropleth summary | recompute |
  refresh [--version N] [--layer L] [--json]` (min / max / mean, counts and breaks per layer; a
  recompute does not rebuild the tiles, `refresh` queues the job that does). Breaks are rounded
  before they are compared with the minimum, so no legend class is empty.
- Tests: `tests/test_choropleth_unit.py` (the rules per field, breaks, bands, classes, sale
  range) and `tests/integration/test_choropleth_postgis.py` (two blocks and two zones through the
  publish job: values, no cell without data, bands from the stored classes in the tiles and the
  pointer, a market set saved for today rebuilding the sale-price heatmap and the tiles, the QA
  command).

## Zones and their planning documents (`backend/core/zones/`, `data/zones/`)

- **What a zone is:** UrbanView's own internal division of a municipality (roughly a city quarter;
  not an official area), grouping the planning documents that apply to it. Drawn in QGIS with the
  client; the reference structure for Podgorica is mondarchitects.com/site-check (a manual copy of
  the rendered page in `data/zones/podgorica/source/`), confirmed against eRegistri. Process and
  folder layout: `data/zones/README.md`.
- **Files drive everything:** `data/zones/<m>/zones.toml` (`core.zones.config.load_zone_config`:
  editing CRS EPSG:25834, file names, the reference capture and its zone slugs, eRegistri URLs /
  jqGrid column indexes / type names, tolerances, base map); a new municipality is a new folder.
  Product-wide vocabulary in `core/zones/schema.py`: zone types `residential | commercial | mixed |
  public_institutional | green_recreation` in QGIS = `res | com | mix | pub | grn` in
  `zones.zone_type`, colours = the map palette (`--z-*`); document status `adopted | in_progress |
  superseded`; `ZONE_FIELDS` (`zone_id` slug, `name`, `zone_type`, `general_planning_summary`,
  `notes`, `no_adopted_plan`) and `DOCUMENT_FIELDS` (`zone_id`, `document_name`, `document_type` =
  a profile document type key (DUP, PUP, PGR, UP, LSL, DSL, PPPN, DPP, PPCG), `status`,
  `eregistri_reference` = the registry's document id, `source_url`, `adoption_date`, `notes`,
  `poc_coverage`, `confirmed`, plus read-only review aids).
- **CLI** `python -m core.zones [--municipality m] seed | template | validate | import | report |
  datasets` (`poe zones`, `poe import-zones`): `seed` (`seed.py`) builds `zones.csv` and
  `zone_documents.csv` from the capture, matched to the eRegistri snapshot (status suggestions,
  every row `confirmed = false`, confirmed lists never overwritten without `--force`);
  `template` (`template.py`, `qgis.py`) writes the GeoPackage (`gpkg.py`: a dependency-free
  GeoPackage writer / reader, default QML styles in `layer_styles`) with the zones, the document
  table and reference layers from PostGIS (KO boundaries, cadastral parcels, document coverage,
  current zones) and the `.qgz` (categorized symbology, forms with value maps / value relation,
  constraints, the zones -> documents relation, a "Zone review" atlas layout);
  `data/zones/qgis/build_project.py` rebuilds the project with PyQGIS inside QGIS; `validate`
  (`validate.py`, no database) checks geometry validity, slugs, names, types (no topology QA:
  overlaps and gaps between zones are QGIS's job, whose project snaps with "avoid overlap";
  removed 2026-10-01), >= 1 adopted document per zone unless
  `no_adopted_plan`, each document in exactly one zone (eRegistri id, else name + listed year),
  document types / statuses / dates; errors refuse the import.
- **Import** (`staging.py`, migration 0021): one `zone_datasets` row per import (`dataset_version`
  `<prefix>-<yyyymmdd>-<n>`, source checksums, validation report, report), the zones as a `zones`
  batch in `staging_geometry` (reprojected to 4326 in PostGIS, keyed by `zone_key`), the documents
  in `staging_zone_documents` matched to registered `planning_documents` (eRegistri id, registry
  link in `source_url`, then folded name; each taken once). A newer import supersedes the staged
  dataset and its batch. **Through the API** (A1 check 2026-09-29): `POST /v1/admin/zones/import
  {file_id, dry_run?}` (admin; the GeoPackage uploaded first as a `gis` file, 409
  `not_a_geopackage` otherwise) queues `import_zones` on the worker (the `gis` extra): read,
  validated with `data/zones/<m>/zones.toml` when the server has it (else the defaults), staged
  with its report, audited `zones.import`; a dry run validates only; errors fail the job naming
  them; the console's "Zones from QGIS" card drives it. The CLI also exports `zones.geojson` (4326) and the normalised
  `zone_documents.csv` to version (in the private data repository: `.gitignore` keeps `data/`
  out of this public one).
- **Publish:** once a reviewer approved the zones batch in the geometry review (origin
  `manual_qgis`, QA validity), the publish job's `geometry` step upserts zones by
  `zone_key` (new column; a legacy
  row with the same name takes the key once; the dataset's attributes replace the zone's) and then
  `apply_zone_datasets` updates matched documents (zone, status, type, source, registry id,
  adoption date; never the registered name or files) or registers new ones (no file, not live, no
  coverage), marking the dataset `published`. Only adopted + live documents with coverage cover
  anything, so in-progress and superseded rows are recorded without coverage.
- **Report** (`report.py`): per zone the type, area, documents by status, POC documents, matched vs
  new, and cadastral / planned urban parcels whose point on surface lies inside (the zone panel's
  rule), with parcels outside every zone, database zones the dataset does not name and registered
  documents missing from the list; `reports/<dataset_version>/report.md` (with a sign-off table),
  `zones.csv`, `documents.csv`, `report.json`: the client's sign-off gate before estimation.
- Tests: `tests/test_zones_seed.py`, `tests/test_zones_validate.py`, `tests/test_zones_template.py`
  and `tests/integration/test_zones_postgis.py` (staging, reprojection, matching, report counts,
  zones upsert and document apply, rolled back).

## Cadastral base (`backend/core/cadastre/`, migration 0024, `docs/gis/cadastral-base.md`)

- **Only confirmed sources.** Adapters behind one interface (`adapters.ADAPTERS`, chosen by
  `[cadastre] source` / `--source`): `uzn_geoportal`, `emapa` (parcels: an export file, or a WFS
  layer snapshotted to a local GeoPackage) and `ekatastar` (ownership / legal burdens: a delivered
  attribute export only, never the web application). Each checks `[cadastre.sources.<id>]` of the
  profile: `access = "confirmed"` needs `access_basis` and `licence_note`, else it raises
  `AccessNotConfirmed` with the reason (CLI exit 2). No scraping code, no fallback. Every
  acquisition records source, method, retrieval date, basis, licence, SHA-256 and size.
- **Import** `python -m core.cadastre import --file EXPORT [--ownership FILE] [--retrieved-on]`
  (`poe cadastre`, `make cadastre ARGS=...`): ogr2ogr (`ogr.py`: `$OGR2OGR`, PATH or the portable
  bundle) writes EPSG:4326 GeoJSONSeq (CRS kept as metadata; the profile's `source_crs` only when
  the export has none; `transform` = ogr2ogr `-ct` for datum shifts), `normalise.py` maps fields
  (KO name / code with `ko_names`, number, sub-number, "1234/5" split, address), `dataset.py` loads
  a temp table, repairs geometries (`ST_MakeValid`, counted) and validates: missing KO / number /
  polygon, unrepairable geometry, duplicate (KO, number, sub-number) are errors (dataset `invalid`,
  nothing staged; `on_duplicate = "merge"` unions parts); outside the extent, grid coverage below
  `min_coverage`, profile KOs missing, < 1 m² parcels are warnings. A valid dataset is staged as a
  `cadastral_parcels` batch (`feature_key = lower(ko_name)|number|sub`, `area_m2` in EPSG:25834,
  `geom_hash`) and a `cadastral_municipalities` batch (delivered boundaries, else derived), with a
  diff against the previous version (published first): added / removed / geometry_changed /
  attributes_changed / unchanged, other KOs `out_of_scope`. Removing more than
  `mass_change_threshold` of the previous parcels of its KOs is refused without
  `--accept-large-change`. `cadastral_datasets` rows (staged | invalid | published | superseded)
  and batches are never deleted. Reports `report.md` / `report.json` / `diff.csv` in
  `data/cadastre/<m>/<version>/`.
- **Publish** applies it once a reviewer approved its two batches in the geometry review
  (origin `official_gis`, QA validity) (`UPSERT_SQL`,
  `dataset.apply_cadastral_datasets`): parcels upserted by
  (KO, number, sub-number), stable ids; parcels of the dataset's KOs missing from it get
  `retired_at` / `retired_dataset_version` (kept for references, never served: locate, tiles,
  links, panel counts, overview and zone reports filter `retired_at IS NULL`); KO table refreshed;
  dataset published, the previous one superseded.
- **Ownership flags** `public_ownership` / `restitution_or_legal_burden` are nullable: null = not
  loaded (only a confirmed eKatastar extract sets them, from explicit values; never derived). The
  API answers null (`bool | None`); no map layer shows them and the tiles do not carry them (not
  in the POC plan). The seeded sample states its flags. Flags belong to the dataset that loaded
  them.
- **KOs** `cadastral_municipalities` (name, code, boundary `delivered | derived_from_parcels`,
  parcel count; unique per municipality on `lower(ko_name)`); the KO list the search box uses comes from the profile
  (public, `max-age=300`) lists KOs with parcels for the search dropdown; the seed loader derives
  the sample's (`refresh_derived_kos`). The KO + number lookup stays `/v1/locate/parcel`.
- Tests: `tests/test_cadastre_unit.py` (profile, access gate, adapters, mapping, ogr2ogr on the
  sample, report) and `tests/integration/test_cadastre_postgis.py` (the sample extracts in
  `tests/cadastre/`: GeoPackage import with a KO layer, publish, lookup at the right spot,
  re-import diff, retiring, refusals, access gate, ownership layers unavailable).

## Georeferencing (`backend/core/gis/georef/`, migration 0025, `docs/gis/georeferencing.md`)

- **From the extraction's local frame to EPSG:4326.** Control points per document in
  `<rules stem>.points.csv` next to the extraction rules (`id, sheet, x_pt, y_pt, easting,
  northing, source grid | label | table | cadastre | corner | manual, note, enabled`; positions in
  the sheet's PDF points, origin bottom-left; disabled, never deleted). `fit.py`: Helmert (vector
  sheets) or affine (scanned / redrawn) from the document's local frame (`pt × scale × 0.0254/72 +
  offset_m`, so one transform serves every sheet; per-sheet `page_to_crs` derived) to the plan CRS
  (`georef.crs` in the rules, else the profile's `source_crs_epsg`: **EPSG:3908** for Podgorica,
  not 25834); residuals, RMSE overall and per sheet, leave-one-out outliers, a Helmert scale warning
  (> 1 % = wrong sheet scale); rejected above `georef.max_rmse_m` (0.5) or under `min_points` (4).
  The stored `<stem>.transform.json` carries the enabled points' SHA-256: `apply` refuses it once
  the points change. `grid.py`: the grid crosses of `georef.grid_layer_regex` (MREZA) become exact
  control points from one seed within ±50 m (a seed 100 m off shifts everything: the staging's
  cadastral checks catch it). `apply.py`: the transform on every layer, then ogr2ogr (`-s_srs` plan
  CRS, `-t_srs EPSG:4326`, `-ct` from `georef.transform` / `--ct` for a better datum operation than
  PROJ's ±10 m EPSG:3965) into one GeoPackage; `digest` (dataset label excluded) is reproduced by
  a re-run. Without a stated operation PROJ chooses one, and versions choose differently: Novi
  Grad's rules state the one its live data was converted with (EPSG:9486 as a PROJ pipeline,
  ending in the axis swap ogr2ogr needs; it reproduces the served coordinates to 3 mm, while
  PROJ 8.2's choice, EPSG:3964, lands 2.8 m away). Redrawn sheets: the same layers and columns,
  `--frame local | sheet:<id>`, `--redrawn`.
- **Stage** (`stage.py`, PostGIS, one transaction, the CLI commits): planned parcels and blocks
  snapped to the served cadastral parcels in the cadastre's `area_crs_epsg` (`ST_Snap`, vertices
  within `georef.snap_tolerance_m` 0.5 m; farther ones stay: re-parcelling is intended); per
  feature `vertices` / `snapped_vertices` / `snapped_ratio`; `<doc>.snap-log.csv` lists moved
  vertices and near misses (1–3 tolerances); the mean vector to the cadastre over the close
  vertices is the overlay check (`systematic_offset_m`). Validation: errors `outside_extent`
  (profile bounds), `no_cadastral_overlap` (cadastral parcels around, zero overlap), `no_features`
  -> dataset `invalid`, nothing staged, exit 1; warnings `no_cadastral_base`,
  `unnumbered_parcels`, `repeated_parcel_numbers`, `systematic_offset` (> half the
  tolerance over ≥ 5 vertices), `no_common_vertices`. Batches: `document_coverage` (key = document
  id), `urban_parcels` (`<doc>|UP <n>`, the profile's `urban_parcel.abbreviation`),
  `urban_blocks` (the plan's label; publish matches a block by label AND overlap, since labels
  recur across plans), `land_use` (generic: the batch carries the other documents' features from
  the newest staged batch or the current version; a re-run replaces the document's own; the
  plan's traffic network is not extracted). Every feature has `document_id`, `dataset_version`.
- **Record** `georef_datasets` (`geo-<doc>-<yyyymmdd>-<n>`, staged | invalid | published |
  superseded, source extraction | manual_redraw | gis_file (0032: a GIS drawing staged by the
  geometry job, method `native`, `rmse_m` null), CRS, method, transform JSON with residuals,
  `rmse_m`, `max_residual_m`, `points_used`, `sheets` per-sheet RMSE, `snap` (+ overlap),
  `validation`, `batches`, `output_sha256`, `gpkg_key`); a newer staged run supersedes the
  document's staged one; its batches are staged with their origin and QA and wait for the
  geometry review; the publish job's geometry step publishes a dataset once its batches are
  approved and applied (`apply_georef_datasets`, the previous published one superseded). `DocumentOut.
  georeference` (admin) is the latest run: RMSE vs limit, per-sheet table, snapping, overlap
  share, offset, warning codes; the console's document page shows it.
- CLI `python -m core.gis.georef points | add | disable | enable | grid | fit | apply [--stage]
  | datasets | show` (exit 1 = fit above the threshold / dataset refused, 2 = input / GDAL error).
  Tests: `tests/test_georef_unit.py` (a synthetic sheet with a known truth: zero RMSE and the
  truth's parameters, a bad point found and the fit rejected, affine vs Helmert, grid crosses
  from a rough seed, GDAL output against an analytic UTM inverse, redrawn-sheet frame, the CLI)
  and `tests/integration/test_georef_postgis.py` (CLI through staging on seeded parcel 1001:
  snapped / near miss / re-parcelled vertices, dataset row, admin summary, publish, re-run digest,
  refusals, systematic offset, land-use carry-forward).

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
  BRQ's "not strictly"). `docs/wireframe/wireframe.css` is the global stylesheet, byte-identical
  in `frontend/src/styles/wireframe.css` (`npm run design:check`): the mock's CSS minus the rules
  of the components the POC does not build (AI assistant, subscription plans, locks and paid
  states, badges, card payment, KPI tiles, the phase strip, dependency notes, the 860 / 760 px
  tablet and phone layouts; removed 2026-10-01; `wireframe-decoded.html` keeps the original).
  Its late override passes set the effective sizes and the 8 px radius on every classed
  element. There is no Tailwind (it was imported but unused). Keep the mock's class names and markup per component (templates in
  `docs/wireframe/wireframe.js`), copy its inline SVG icons and copy strings. Fonts: Schibsted
  Grotesk 400–800 and JetBrains Mono 400 / 500 / 700, self-hosted.
- Acceptance: `docs/wireframe/screens/<state>.png` (one per state the POC builds, 1440×900 and
  the 1100 px width; `urban.png` shows Group 2 unlocked) and `docs/wireframe/computed-styles.json`
  (effective styles of 202 selectors);
  `python docs/wireframe/make_screens.py [state…] [--dump]` regenerates both with headless
  Chrome / Edge.
- Allowed deviations only (spec §9): Mapbox + PMTiles instead of the SVG city; API data (all 13
  planning fields, each with its `source` chip); market data shown to everyone (no LOCKED
  chips, no "Choose your access" modal: the POC has no subscription); no planned-traffic,
  ownership or restitution cards; no AI assistant (the intent button "Ask about this site");
  no card fields; bilingual text; no tablet / phone layout (not in the POC plan). The admin view
  is the wireframe's overlay inside the frontend
  (the admin console: every tab is built, see `frontend/CLAUDE.md`). Anything else is an open item
  (spec §10), not a redesign.
- Scope is unsettled: `docs/UrbanView_POC_Exclusions.docx.md` (265 h POC) excludes screens that
  the build plan and this backend include; ask before building screens it excludes.

## Conventions

**Municipality isolation (BRD §8).** Everything place-specific is data or configuration:
bounds, centre, CRS, KO list, time zone (`timezone`: the local date effective-dated assumptions
switch on), planning terminology (IZ/II/KO/UP, DUP/PUP/PGR) and data-source
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

**Library versions.** The server runs the versions the tests ran on. `backend/constraints.txt`
names the one version of every Python library in use, the libraries' own dependencies included
(77 lines, the worker image's list; first written 2026-10-03 from `pip freeze` in the live
containers: until then every image build installed the newest release, and the server ran
SQLAlchemy 2.1, FastAPI 0.142 and Starlette 1.7 while the tests ran on 2.0, 0.141 and 1.6).
`pyproject.toml` keeps the ranges the code accepts. Both images install with `pip install -c
constraints.txt` and end with `python -m core.pins --strict` (`core/pins.py`: a library installed
and not listed stops the build and names the line to add; a stopped build leaves the running
containers alone). `make install` installs the same versions (dev, gis and ai extras) and
`tests/test_pins.py` fails when the test environment and the file disagree, so a test run always
says which versions it ran on. Raising a version: `deploy/README.md`, "Library versions" (change
the line, install, test, deploy). The map's libraries are fixed by `package-lock.json` (`npm
ci`). Not fixed: the base images (`python:3.11-slim`, `node:22-bookworm-slim`) and their Debian
packages (`gdal-bin`), which follow their tags.

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
Behind the proxy a request without `X-Forwarded-For` did not come through it: it is the site's
own server (page rendering, the staff console's server-side calls) and is not counted
(2026-10-02: those calls shared one bucket, and the Documents page's 3 s auto-refresh while a job
ran used all of it, so the console answered "The data service did not answer").
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
| lint | fmt | gis-assess | cadastre | up | down | db-dev-install | db-dev-start | db-dev-stop` (Windows without make:
`cd backend && poe <task>`). `python -m core.pins` (from `backend/`): the installed libraries
against `constraints.txt`.
