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
| `backend/` | FastAPI app (`api/`: app factory, routers under `/v1`, schemas, services), `core/` (settings, logging, errors, middleware, db, models, redis, storage, mail, municipality profiles, seeds loader), `jobs/` (Celery: ingestion, extraction, publish), `municipalities/<id>.toml`, `tests/` (unit) and `tests/integration/` (PostGIS). Python venv: `backend/.venv`. |
| `database/` | Alembic (`alembic.ini`, `migrations/`), seed datasets (`seeds/podgorica_sample/*.geojson`), compose init SQL (`docker/initdb/`), `scripts/dev_postgis.py` (portable PostGIS for Docker-less machines). |
| `frontend/` | Public map, Next.js + Mapbox GL JS + PMTiles (reserved, P2). |
| `admin/` | Staff tool, Next.js + Auth.js, roles admin / reviewer / expert (reserved, P1). |
| `packages/` | Shared TypeScript formula engine + client-validated fixtures (reserved). |
| root | `docker-compose.yml`, `Makefile`, `ruff.toml`, `README.md`, this file. |

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
- Client validates the formula fixtures before any UI is built on them (P0 gate 3). The shared
  engine lives in `packages/formula-engine` (TypeScript); Python must pass the same fixtures.

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
reflects them. Migrations: `make migration m="..."` then `make migrate`; `alembic check` runs in
the integration tests, so models and migrations must agree.

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
