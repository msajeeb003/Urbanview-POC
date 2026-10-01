"""The API holds exactly what the 220 h POC plan funds (API check 2026-09-30): every route the app
serves is in SURFACE with the plan item that needs it, and nothing else. A new route needs a funded
row here first; a removed one (rollback, bulk approval, the Overview, the e-mail log, file
listings, pasted market listings, market coverage, a payment webhook ...) fails this test if it
comes back. The pilot scope's paths are ``/api/...``; this API's are ``/v1/...`` (kept, see
CLAUDE.md "Source viewer")."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.helpers import make_app

PUBLIC = {
    # B1 setup: liveness / readiness for the compose health checks (not rate-limited)
    ("get", "/health"): "B1 scaffolding",
    ("get", "/health/ready"): "B1 scaffolding",
    # 1. GET /api/municipalities: the covered city (profile) + the current tiles key / version_no
    ("get", "/v1/municipality"): "1 municipalities",
    ("get", "/v1/tiles/current"): "1 municipalities (tiles key, version_no)",
    ("get", "/v1/geocode"): "2 geocode",
    ("get", "/v1/locate"): "3 locate by point",
    ("get", "/v1/locate/parcel"): "3 locate by KO + number",
    # S2 "enter a parcel number": the planned parcel's own number (tester's report 2026-10-02:
    # with no cadastral base loaded no parcel number could be found at all)
    ("get", "/v1/locate/urban-parcel"): "3 locate by planned parcel number",
    # 4. / 5. the zone and parcel panels (the map renders /v1/panel; /parcels/{id} = the deep link)
    ("get", "/v1/panel"): "4 zones/{id} + 5 parcels/{id}",
    ("get", "/v1/parcels/{parcel_id}/panel"): "5 parcels/{id}",
    ("get", "/v1/zones"): "S2 search: zone names and a hit's zone",
    ("post", "/v1/feasibility"): "6 feasibility",
    ("get", "/v1/source/{document_id}/page/{page}"): "7 documents/{id}/source?page=n",
    ("get", "/v1/source/value/{value_id}"): "7 the cited page of a value",
    ("post", "/v1/events"): "8 events",
    ("post", "/v1/orders"): "9 orders",
    ("get", "/v1/orders/pricing"): "S4 price stated before commitment",
    ("get", "/v1/orders/{reference}"): "10 orders/{reference}",
}
STAFF = {
    ("post", "/v1/auth/magic-link"): "12 auth",
    ("post", "/v1/auth/magic-link/exchange"): "12 auth",
    ("post", "/v1/auth/sign-out"): "12 auth",
    ("get", "/v1/admin/documents"): "13 documents",
    ("post", "/v1/admin/documents"): "13 documents",
    ("get", "/v1/admin/documents/{document_id}"): "13 documents",
    ("patch", "/v1/admin/documents/{document_id}"): "13 documents",
    ("patch", "/v1/admin/documents/{document_id}/coverage"): "13 documents (coverage live)",
    ("post", "/v1/admin/documents/{document_id}/files"): "13 A1 multi-file upload",
    ("patch", "/v1/admin/documents/{document_id}/files/{file_id}"): "13 A1 multi-file upload",
    ("delete", "/v1/admin/documents/{document_id}/files/{file_id}"): "13 A1 multi-file upload",
    ("post", "/v1/admin/files"): "14 files",
    ("post", "/v1/admin/documents/{document_id}/jobs/extract"): "14 files/{id}/extract",
    ("post", "/v1/admin/files/{file_id}/jobs/geo"): "14 files/{id}/geo",
    ("post", "/v1/admin/zones/import"): "14 zones/import",
    ("get", "/v1/admin/jobs"): "Jobs: status",
    ("get", "/v1/admin/jobs/{job_id}"): "Jobs: status (status_url)",
    ("post", "/v1/admin/jobs/{job_id}/retry"): "Jobs: retries",
    ("get", "/v1/admin/review"): "15 review",
    ("get", "/v1/admin/review/{item_id}"): "15 review",
    ("post", "/v1/admin/review/{item_id}/approve"): "15 review decision",
    ("post", "/v1/admin/review/{item_id}/amend"): "15 review decision",
    ("post", "/v1/admin/review/{item_id}/reject"): "15 review decision",
    ("get", "/v1/admin/review/summary"): "A2 100 % reviewed before publish",
    ("get", "/v1/admin/review/options"): "A2 amend: the document's own wordings",
    ("get", "/v1/admin/geometry"): "A2 geometry_draft review",
    ("get", "/v1/admin/geometry/{batch_id}"): "A2 geometry_draft review",
    ("get", "/v1/admin/geometry/{batch_id}/features"): "A2 geometry_draft review",
    ("post", "/v1/admin/geometry/{batch_id}/approve"): "A2 geometry_draft review",
    ("post", "/v1/admin/geometry/{batch_id}/reject"): "A2 geometry_draft review",
    ("post", "/v1/admin/publish"): "16 publish",
    ("get", "/v1/admin/publish"): "A4 publish status",
    ("get", "/v1/admin/assumptions"): "17 assumptions",
    ("post", "/v1/admin/assumptions"): "17 assumptions CRUD",
    ("post", "/v1/admin/assumptions/batch"): "A5 save",
    ("get", "/v1/admin/assumptions/{assumptions_id}"): "17 assumptions CRUD",
    ("put", "/v1/admin/assumptions/{assumptions_id}"): "17 assumptions",
    ("delete", "/v1/admin/assumptions/{assumptions_id}"): "17 assumptions CRUD (retire)",
    ("get", "/v1/admin/formulas"): "17 formulas",
    ("get", "/v1/admin/orders"): "18 orders",
    ("get", "/v1/admin/orders/{order_id}"): "18 orders",
    ("patch", "/v1/admin/orders/{order_id}/status"): "18 orders",
    ("post", "/v1/admin/orders/{order_id}/payment"): "A6 payment received / not / refunded",
    ("post", "/v1/admin/orders/{order_id}/assign"): "A6 assign expert",
    ("post", "/v1/admin/orders/{order_id}/report"): "A6 upload report, delivered",
    ("get", "/v1/admin/orders/experts"): "A6 assign expert",
    ("get", "/v1/admin/analytics"): "19 analytics",
    ("get", "/v1/admin/audit"): "20 audit",
    ("get", "/v1/admin/users"): "21 users",
    ("post", "/v1/admin/users"): "21 users",
    ("get", "/v1/admin/users/me"): "21 users (the console's role)",
    ("get", "/v1/admin/users/{user_id}"): "21 users",
    ("patch", "/v1/admin/users/{user_id}"): "21 users",
    ("post", "/v1/admin/market/imports"): "22 market-imports",
    ("get", "/v1/admin/market/imports"): "22 market-imports",
    ("get", "/v1/admin/market/imports/{import_id}"): "22 market-imports",
    ("get", "/v1/admin/review/market-inputs"): "22 market-import review",
    ("post", "/v1/admin/review/market-inputs/{item_id}/approve"): "22 market-import review",
    ("post", "/v1/admin/review/market-inputs/{item_id}/amend"): "22 market-import review",
    ("post", "/v1/admin/review/market-inputs/{item_id}/reject"): "22 market-import review",
}
SURFACE = {**PUBLIC, **STAFF}
BACKEND = Path(__file__).resolve().parents[1]


def test_the_api_serves_exactly_the_funded_routes():
    paths = make_app().openapi()["paths"]
    served = {(method, path) for path, operations in paths.items() for method in operations}
    assert served - SURFACE.keys() == set(), "routes no funded row needs"
    assert SURFACE.keys() - served == set(), "funded routes missing"


def test_no_payment_webhook_and_no_checkout_url():
    spec = make_app().openapi()
    assert not [path for path in spec["paths"] if "webhook" in path]
    reply = spec["paths"]["/v1/orders"]["post"]["responses"]["201"]["content"]["application/json"]
    created = spec["components"]["schemas"][reply["schema"]["$ref"].rsplit("/", 1)[-1]]
    assert "reference" in created["properties"]
    assert not [name for name in created["properties"] if "checkout" in name]


def test_no_route_can_reach_an_llm():
    """The extraction's model calls run in the worker's jobs (the ``ai`` extra is installed in the
    worker image only): no API module builds a model and the API process never loads the SDK."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (BACKEND / "api").rglob("*.py"))
    for builder in ("ClaudeModel(", "model_from_settings(", "import anthropic"):
        assert builder not in sources, builder
    probe = (
        "import sys; from api.app import create_app; create_app(); "
        "print('SDK loaded:', 'anthropic' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND,
        env={**os.environ, "LOCATION_RESOLVER": "nodata"},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert "SDK loaded: False" in result.stdout.splitlines(), result.stdout
