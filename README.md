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
| `frontend/` | Public map (reserved, build plan P2) | Next.js, Mapbox GL JS + PMTiles |
| `admin/` | Admin / review tool (reserved, build plan P1) | Next.js, Auth.js |
| `packages/` | Shared formula engine + client-validated fixtures (reserved) | TypeScript |
| `docs/` | BRD, BRQ, wireframe, brand SVGs | |

## Quick start

```bash
cp backend/.env.example backend/.env
make install                 # backend/.venv with dev extras
make up                      # postgres/postgis, redis, minio, migrations, api (:8000), worker  (Docker)
make seed                    # load the Podgorica sample dataset
make test                    # unit tests, no services needed
make test-integration        # PostGIS tests (TEST_DATABASE_URL, default = compose's urbanview_test)
```

No Docker? `make db-dev-install && make db-dev-start` sets up a portable PostgreSQL 16 + PostGIS
under your local app-data folder (see `database/README.md`), then:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/urbanview_test make test-integration
```

Without `make`: `cd backend` and use `poe run | test | migrate | seed | worker` (tasks in
`backend/pyproject.toml`).

- API docs: http://localhost:8000/docs
- Health: `GET /health`, `GET /health/ready`
- Location resolution: `GET /v1/locate?lat=&lng=`, `GET /v1/locate/parcel?ko=&number=&sub=`
