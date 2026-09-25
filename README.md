# UrbanView (POC working name: Arvin View)

A public web map of Podgorica where anyone finds a parcel (address, map click or cadastral number)
and sees what the adopted planning documents allow on it, plus an admin pipeline that turns
planning PDFs into reviewed, published map data. Product rules and repo conventions: `CLAUDE.md`.
Source documents (BRD v1.6, BRQ, wireframe, brand assets): `docs/`.

## Repository layout

| Folder | What it is | Stack |
|---|---|---|
| `backend/` | Public API, location resolution, jobs | FastAPI, SQLAlchemy 2 (async), Celery, Redis, S3 |
| `database/` | Schema migrations, seed datasets, DB tooling | Alembic, PostgreSQL 16 + PostGIS |
| `frontend/` | Public map: shell, shared components, typed API client, analytics (see `frontend/README.md`) | Next.js 16, TypeScript, Tailwind v4, shadcn/ui, Mapbox GL JS, TanStack Query |
| `admin/` | Admin / review tool (reserved, build plan P1) | Next.js, Auth.js |
| `packages/` | `feasibility-engine`: the shared feasibility formula engine and its fixtures (npm workspace; the backend runs a Python copy held to the same fixtures) | TypeScript |
| `docs/` | BRD, BRQ, pilot technical scope, POC exclusions, wireframe (+ per-state screenshots), brand SVGs, specs (`specs/panel-payload.md`, `specs/frontend-design.md`) | |

## Quick start

```bash
cp backend/.env.example backend/.env
make install                 # backend/.venv with dev extras
make up                      # postgres/postgis, redis, minio, migrations, api (:8000), worker  (Docker)
make seed                    # load the Podgorica sample dataset + placeholder PDFs into MinIO
make test                    # unit tests, no services needed
make test-integration        # PostGIS tests (TEST_DATABASE_URL, default = compose's urbanview_test)
```

No Docker? `make db-dev-install && make db-dev-start` sets up a portable PostgreSQL 16 + PostGIS
under your local app-data folder (see `database/README.md`), then:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/urbanview_test make test-integration
```

Without `make`: `cd backend` and use `poe run | test | migrate | seed | worker | flower` (tasks
in `backend/pyproject.toml`).

- API docs: http://localhost:8000/docs
- Health: `GET /health`, `GET /health/ready`
- Municipality profile: `GET /v1/municipality`
- Location resolution: `GET /v1/locate?lat=&lng=`, `GET /v1/locate/parcel?ko=&number=&sub=`
- Display-shaped panels: `GET /v1/parcels/{id}/panel` (header with calculation basis,
  Group 1 with a source on every value, market, assumptions, Group 2 and the engine inputs;
  cached per data version, ETag) and `GET /v1/zones/{id}/panel`
- Information panel: `GET /v1/panel?type=zone|document|cadastral|urban&id=` (optional
  `saleable_share`, `construction_cost_eur_m2`, `sale_price_eur_m2` overrides; contract in
  `docs/specs/panel-payload.md`)
- Feasibility recalculation: `POST /v1/feasibility` with `{parcel_id, type, assumptions}`; same
  serving data and the same shared engine as the panel (`packages/feasibility-engine`)
- Search-box autocomplete: `GET /v1/geocode?q=` (OSM geocoder behind config, scoped to the
  municipality; the client follows a pick with `/v1/locate?lat=&lng=`)
- Source viewer: `GET /v1/source/value/{value_id}` (the panel's `Source.viewer_url`) and
  `GET /v1/source/{document_id}/page/{page}`: a short-lived signed URL to the cited PDF page in
  the private bucket, plus the value's box
- Analytics: `POST /v1/events` (batches of the 13 anonymous product events) and
  `GET /v1/admin/analytics?from=&to=` (funnel, orders, districts, repeat usage, interest; bearer
  token with the `admin` role from `ADMIN_API_TOKENS`)
- Staff pipeline API (role `admin`; config tokens or staff sessions from `python -m core.staff`):
  `POST /v1/admin/files` (de-duplicated uploads), `POST /v1/admin/documents` (versioned
  registration), `GET /v1/admin/files|documents`, `POST /v1/admin/documents/{id}/jobs/extract`,
  `POST /v1/admin/files/{id}/jobs/geo` (idempotent per target + file checksum),
  `GET /v1/admin/jobs` (+ `/{id}`, `/{id}/retry`, `/costs`: status, attempts, LLM cost),
  `POST /v1/admin/publish` (everything approved → new serving version → PMTiles archive →
  pointer flip; refused while items are pending review), `GET /v1/admin/publish` (status,
  per-step progress), `POST /v1/admin/publish/rollback` (pointer flip back, no recompute),
  public `GET /v1/tiles/current` (signed archive URL + data version),
  `GET /v1/admin/email-log` (+ `/{id}/bounce`), public `POST /v1/auth/magic-link` (+
  `/exchange`) for the staff login,
  `PATCH /v1/admin/documents/{id}/coverage`; every action lands in `audit_log`
- Admin configuration (role `admin`, versioned, audited): `/v1/admin/assumptions` (financial
  assumptions per zone with optional absolute bounds; the current version is what the panel
  reads and names), `/v1/admin/zone-parameters` (typical planning values per zone, shown on
  the zone panel), `/v1/admin/users` (staff users, no passwords)
- Expert review (roles admin / reviewer / expert): `GET /v1/admin/review` (staged extracted
  items with value, target, source page + bbox + snippet and a signed page link),
  `POST /v1/admin/review/{id}/approve|amend|reject`, `POST /v1/admin/review/bulk-approve`,
  `GET /v1/admin/review/summary` (per-document counters, `can_publish`); `GET /v1/admin/audit`
  reads the append-only audit trail (who changed what and when)
- Expert-analysis orders: `POST /v1/orders` (guest checkout, price from `ORDER_PRICE_TIERS`,
  bank-transfer instructions by e-mail, snapshot of the panel shown), `GET /v1/orders/{reference}/status`
  (no personal data); staff: `GET /v1/admin/orders`, `PATCH .../status`, `POST .../payment`,
  `POST .../assign`, `POST .../report` (PDF upload delivers the order and e-mails a signed link)

## Publishing and rollback

`POST /v1/admin/publish` runs the `publish_approved` job on the `publish` queue: approved and
amended review items and staged geometry become a new serving version, parcel links and heatmap
cells are recomputed, every map layer is exported and built into one PMTiles archive (tippecanoe
+ tile-join, built into `backend/Dockerfile.worker`), the archive is uploaded to the private
bucket, and the version becomes current in the same transaction. The public map asks
`GET /v1/tiles/current` for the signed archive URL and the data version.

Rollback is a manual pointer flip, nothing is recomputed:

```bash
curl -X POST http://localhost:8000/v1/admin/publish/rollback -H "Authorization: Bearer $TOKEN" -d '{}'
```

(`{"version_id": N}` picks a specific version; `GET /v1/admin/publish` lists them.) The last
`PUBLISH_KEEP_VERSIONS` (3) versions keep their archives and derived rows; older archives are
pruned and can no longer be restored. Entity geometry (parcels, blocks, zones, coverage) is
upserted in place with stable ids, so a geometry change is rolled back by re-ingesting.

## Transactional e-mail

Three templates (`backend/core/mail/templates`, Montenegrin + English, text + HTML):
payment instructions on order creation, report delivery on upload, staff magic links. Every
e-mail is a `send_email` job on the `email` queue with retries; every send is an `email_log` row
(recipient, template, order / user id, provider message id, status), bodies are never stored.

- Dev: `docker compose up` includes Mailpit; the worker sends to it and the inbox is
  http://localhost:8025. Without `SMTP_HOST` nothing is sent (rows say `suppressed`).
- Staging: set `MAIL_ALLOWLIST` (addresses or `@domain`); anything else is suppressed, so a
  real customer is never mailed from staging.
- Production: the SMTP provider account (Postmark / SES / Resend: host, port, credentials,
  verified sender with SPF, DKIM and DMARC) goes into `backend/.env`. Deliverability check:

```bash
cd backend && .venv/Scripts/python -m core.mail.testsend --template payment_instructions --to you@example.com
```

