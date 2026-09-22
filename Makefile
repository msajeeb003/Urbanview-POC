# UrbanView monorepo task runner (run from the repo root).
# Without `make` (e.g. Windows): `cd backend` and use `poe <task>` (tasks in backend/pyproject.toml).
.DEFAULT_GOAL := help
SHELL := /bin/sh

VENV ?= .venv                       # created inside backend/
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
  PY  := python
else
  BIN := $(VENV)/bin
  PY  := python3
endif
COMPOSE ?= docker compose
ALEMBIC := $(BIN)/alembic -c ../database/alembic.ini
TEST_DATABASE_URL ?= postgresql+asyncpg://urbanview:urbanview@localhost:5432/urbanview_test

.PHONY: help venv install run worker migrate migration downgrade seed test test-integration test-all \
        lint fmt up down logs ps db-dev-install db-dev-start db-dev-stop db-dev-status clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

venv: ## create backend/.venv
	cd backend && $(PY) -m venv $(VENV)

install: venv ## install the backend with dev extras
	cd backend && $(BIN)/pip install --upgrade pip && $(BIN)/pip install -e ".[dev]"

run: ## run the API with auto-reload on :8000
	cd backend && $(BIN)/uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

worker: ## run the Celery worker (all queues)
	cd backend && $(BIN)/celery -A jobs.celery_app worker --loglevel=info -Q default,gis,extraction,publish

migrate: ## apply migrations to head (database/migrations)
	cd backend && $(ALEMBIC) upgrade head

migration: ## autogenerate a migration: make migration m="add orders"
	cd backend && $(ALEMBIC) revision --autogenerate -m "$(m)"

downgrade: ## roll back one migration
	cd backend && $(ALEMBIC) downgrade -1

seed: ## load the Podgorica sample dataset (database/seeds/podgorica_sample)
	cd backend && $(BIN)/python -m core.seeds podgorica_sample

test: ## unit tests (no services needed)
	cd backend && $(BIN)/pytest -q --ignore=tests/integration

test-integration: ## PostGIS integration tests (needs TEST_DATABASE_URL)
	cd backend && TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest -q tests/integration

test-all: ## unit + integration
	cd backend && TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest -q

lint: ## ruff check backend + database
	cd backend && $(BIN)/ruff check . ../database

fmt: ## ruff format backend + database
	cd backend && $(BIN)/ruff format . ../database

up: ## start postgres/postgis, redis, minio, migrate, api and worker
	$(COMPOSE) up -d --build

down: ## stop the stack
	$(COMPOSE) down

logs: ## tail api + worker logs
	$(COMPOSE) logs -f api worker

ps: ## show stack status
	$(COMPOSE) ps

db-dev-install: ## portable PostgreSQL 16 + PostGIS for machines without Docker (one-off download)
	$(PY) database/scripts/dev_postgis.py install

db-dev-start: ## start the portable PostGIS on 127.0.0.1:55432 and ensure urbanview + urbanview_test exist
	$(PY) database/scripts/dev_postgis.py start
	$(PY) database/scripts/dev_postgis.py createdb urbanview
	$(PY) database/scripts/dev_postgis.py createdb urbanview_test

db-dev-stop: ## stop the portable PostGIS
	$(PY) database/scripts/dev_postgis.py stop

db-dev-status: ## status of the portable PostGIS
	$(PY) database/scripts/dev_postgis.py status

clean: ## remove caches
	rm -rf backend/.pytest_cache backend/.ruff_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
