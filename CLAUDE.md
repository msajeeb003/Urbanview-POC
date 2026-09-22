# UrbanView — working notes for Claude

UrbanView (POC working name "Arvin View") is a public web map of Podgorica where anyone finds a
parcel (address, map click or cadastral number) and sees what the adopted planning documents allow
on it: zone, planning parameters with source links, and a low / expected / high financial
feasibility range, then orders an expert report. Behind it: an admin tool where staff upload
planning PDFs, AI extracts parameters, a reviewer checks 100% of items against the cited PDF page,
and one button publishes approved data to the map.

Podgorica-only prototype, but the ingestion/serving design must extend to other municipalities as
a **data + configuration exercise, not a rebuild** (BRD §8). Source documents: `docs/` (BRD v1.6
PDF + extracted text, BRQ, wireframe, brand SVGs).

## Repository layout (frontend / backend / database kept separate)

| Folder | Contents |
|---|---|
| `backend/` | FastAPI app (`api/`: app factory, routers under `/v1`, schemas, services), `core/` (settings, logging, errors, middleware, db, models, `engine/` feasibility formulas, redis, storage, mail, municipality profiles, seeds loader), `jobs/` (Celery: ingestion, extraction, publish), `municipalities/<id>.toml`, `tests/` (unit) and `tests/integration/` (PostGIS). Python venv: `backend/.venv`. |
| `database/` | Alembic (`alembic.ini`, `migrations/`), seed datasets (`seeds/podgorica_sample/*.geojson` for the geometry tables, `*.json` for the panel tables), compose init SQL (`docker/initdb/`), `scripts/dev_postgis.py` (portable PostGIS for Docker-less machines). |
| `frontend/` | Public map, Next.js + Mapbox GL JS + PMTiles (reserved, P2). |
| `admin/` | Staff tool, Next.js + Auth.js, roles admin / reviewer / expert (reserved, P1). |
| `packages/` | `feasibility-engine/`: the shared TypeScript feasibility engine (npm workspace of the root `package.json`) and `fixtures/feasibility-cases.json`, the fixture file both engines are held to. |
| root | `docker-compose.yml`, `Makefile`, `ruff.toml`, `package.json` (npm workspaces), `README.md`, this file. |

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
  `saleable_share` (0.30–1.00), `construction_cost_eur_m2` and `sale_price_eur_m2` ((0, 100 000])
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
  Only the publish job (`jobs.tasks.publish.publish_approved_data`, a stub today) may copy
  approved rows across; the sample's serving rows come from the seed loader.
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
  `registry_url`; `viewer_url` is reserved for the source viewer).
- **Data version.** `data_version` = `label` of the `publish_versions` row with `is_current` (at
  most one per municipality), `data_version_date` = its `published_at` as a UTC date; with no
  current row `"unpublished"` / null and the serving rows still render. Market inputs are not
  versioned with it: the panel shows their `source`, `source_date` and `effective_from`
  (`created_at` of the current row).
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
  `client_validated: false`.
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
(`backend/.env.example`). `APP_ENV` dev / staging / prod; non-dev refuses dev DB defaults and
missing S3 credentials. Secrets are `SecretStr`. No secrets in code.

**Errors.** One envelope `{"error": {"code", "message", "request_id", "details"?}}`. Raise
`core.errors.AppError` subclasses for expected client errors; unhandled exceptions become
`internal_error` (500) logged with request id. Validation errors are `validation_error` (422).

**Request context.** `RequestContextMiddleware` (outermost) sets `X-Request-ID`,
`Server-Timing`, `X-Response-Time`, logs one access line per request with route + latency.

**Rate limiting.** `RateLimitMiddleware`: per client IP, fixed window in Redis, 429 in the
envelope with `Retry-After` + `X-RateLimit-*`, `/health*` exempt, fails **open, fast** (250 ms
budget per Redis call + 5 s circuit breaker), `TRUST_PROXY_HEADERS` gates `X-Forwarded-For`.
Middleware order (outermost first): RequestContext → CORS → RateLimit → routes.

**Jobs.** Queues `default`, `gis`, `extraction`, `publish`. Nothing reaches the public map except
through `jobs.tasks.publish.publish_approved_data`.

**Tests.** Unit: `make test` (fakeredis, frozen clock, `LOCATION_RESOLVER=nodata`). Integration:
`make test-integration` with `TEST_DATABASE_URL` (skipped when unreachable); the session fixture
resets the schema, runs migrations up → base → up and loads `podgorica_sample`. Build apps with
`tests.helpers.make_app`, clients with `make_client(app, client_ip)`.

**Commands.** `make install | run | worker | migrate | migration | seed | test | test-integration
| lint | fmt | up | down | db-dev-install | db-dev-start | db-dev-stop` (Windows without make:
`cd backend && poe <task>`).
