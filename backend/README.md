# backend/

FastAPI + Python 3.11, SQLAlchemy 2 (async, asyncpg, GeoAlchemy2), Celery + Redis, boto3 (S3).
Migrations and seed data live in `../database/`. See the repo-root `README.md` and `CLAUDE.md`.

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"     # Windows; use .venv/bin on Unix
poe run              # API on :8000
poe migrate          # alembic -c ../database/alembic.ini upgrade head
poe seed             # load database/seeds/podgorica_sample
poe test             # unit tests
poe test-integration # PostGIS tests (TEST_DATABASE_URL)
```
