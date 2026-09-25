"""Write the API's OpenAPI document to a file, without a database, Redis or network.

The frontend generates its TypeScript API types from this file (``npm run api:types`` in
``frontend/``), so the typed client always matches the routes and schemas served here.

    python -m api.export_openapi ../frontend/openapi.json

The app is built with ``LOCATION_RESOLVER=nodata`` and development defaults; building the schema
never touches PostGIS, Redis, storage or the broker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from api.app import create_app
from core.config import Settings

DEFAULT_TARGET = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def build_schema() -> dict:
    settings = Settings(_env_file=None, app_env="dev", location_resolver="nodata")
    app = create_app(settings)
    return app.openapi()


def main(argv: list[str]) -> int:
    target = Path(argv[0]) if argv else DEFAULT_TARGET
    schema = build_schema()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    version = schema.get("info", {}).get("version", "?")
    print(f"OpenAPI {version}: {len(schema['paths'])} paths -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
