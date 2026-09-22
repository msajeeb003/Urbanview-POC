# Build context is the repo root (see docker-compose.yml) so the image carries backend/,
# database/ (migrations, seeds) and packages/. The GIS track (GDAL, tippecanoe) will get its own
# worker image later; keep this one slim so the public API stays fast to build and start.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend /app/backend
COPY database /app/database
COPY packages /app/packages

WORKDIR /app/backend
RUN pip install -e ".[dev]"

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
