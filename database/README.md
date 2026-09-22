# database/

PostgreSQL 16 + PostGIS. Everything about the schema and its data lives here; the code that uses
it lives in `backend/`.

| Path | Purpose |
|---|---|
| `alembic.ini`, `migrations/` | Alembic (async). `env.py` imports the SQLAlchemy models from `backend/core/models`. Run from `backend/`: `alembic -c ../database/alembic.ini upgrade head` (or `make migrate`). |
| `migrations/versions/` | `0001` enables PostGIS; `0002` the location-resolution schema (zones, planning documents, urban blocks, planned urban parcels, cadastral parcels); `0003` the information-panel schema (publish versions, planning field dictionary, planning parameter values / extractions, financial assumptions, `planning_documents.amends_document_id`). |
| `seeds/podgorica_sample/` | Hand-made Podgorica sample: one GeoJSON FeatureCollection per geometry table and one plain JSON array per panel table, all with explicit ids. Loaded by `make seed` / `python -m core.seeds podgorica_sample` and by the integration tests. Add `--synthetic-bulk` for 300 synthetic documents and 10k cadastral + 10k planned parcels (load/plan testing; tagged `dataset_version = synthetic-bulk`). |
| `docker/initdb/` | Init SQL for the compose `postgres` service (creates `urbanview_test`). |
| `scripts/dev_postgis.py` | Portable PostgreSQL + PostGIS for machines without Docker (Windows). |

## Tables

| Table | Migration | Contents |
|---|---|---|
| `zones` | 0002 | UrbanView's internal city divisions (~quarters) grouping planning documents. |
| `planning_documents` | 0002, 0003 | Planning documents with coverage geometry, `status` (`adopted` / `in_progress` / `superseded`) and, since 0003, `amends_document_id` (explicit amendment link set at ingestion, never derived from coverage). |
| `urban_blocks` | 0002 | Urban blocks. |
| `urban_parcels` | 0002, 0003 | Planned urban parcels from a plan (the calculation basis when one exists). 0003 adds the key `(id, document_id)` (`uq_urban_parcels_id_document`). |
| `cadastral_parcels` | 0002 | Cadastral parcels as recorded by the cadastre; `id` is UrbanView's Parcel ID. |
| `publish_versions` | 0003 | One row per publish act; the row with `is_current` (at most one per municipality) names the panel's `data_version`. |
| `planning_fields` | 0003 | Product-wide field dictionary (the 11 Group 1 fields + 2 computed ones), seeded by the migration itself, never by the seed loader. No `municipality_id`. |
| `planning_parameter_values` | 0003 | SERVING: approved, published planning values. Every row cites a source page of its document and a `publish_version_id`; `urban_parcel_id` null = document-level value. Exactly one of `value_text` / `value_number`. |
| `planning_parameter_extractions` | 0003 | STAGING: the review queue (`review_state` enum `pending_review` / `approved` / `rejected` / `amended`). Never read by the public API; the publish job copies approved rows into the serving table. |
| `financial_assumptions` | 0003 | Admin-published market inputs per zone (`zone_id` null = municipality-wide default); at most one current row per zone and one current default per municipality. |

Contract for the panel tables: `docs/specs/panel-payload.md` section 2.

## Schema rules

- Geometry is stored as `geometry(MultiPolygon, 4326)`; every geometry column has a GiST index
  named `idx_<table>_<column>`.
- Every table carries `municipality_id` (matches `backend/municipalities/<id>.toml`).
- `cadastral_parcels` (the cadastre today; `id` is UrbanView's Parcel ID) and `urban_parcels`
  (planned parcels from an adopted plan) are separate tables, never merged.
- A cadastral parcel is identified by (KO, number, sub-number): unique index
  `uq_cadastral_parcels_ko_number` (case-insensitive KO, NULL sub as `''`). The same number
  recurs across KOs.
- `planning_documents.status` is the enum `planning_document_status`
  (`adopted | in_progress | superseded`); only adopted documents define coverage.

## Local database without Docker (Windows)

```bash
make db-dev-install      # downloads the official EDB PostgreSQL 16 zip + OSGeo PostGIS bundle
make db-dev-start        # runs on 127.0.0.1:55432 (trust auth, local only), creates urbanview + urbanview_test
TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/urbanview_test make test-integration
make db-dev-stop
```

Everything is installed under `%LOCALAPPDATA%\urbanview-dev\pg16` (override with
`URBANVIEW_PG_HOME`); `python database/scripts/dev_postgis.py uninstall` removes it. No
installer, no Windows service, no PATH changes.

## Integration tests

`backend/tests/integration` needs `TEST_DATABASE_URL` (default: the compose database
`postgresql+asyncpg://urbanview:urbanview@localhost:5432/urbanview_test`). The session fixture
drops and recreates the `public` schema, runs the migrations up → base → up, loads the sample plus
the synthetic volume, then checks the location queries (results, `EXPLAIN`-verified GiST index use
and no sequential scans on the large tables, one statement per call, latency) and `alembic check`
(models == migrations).
