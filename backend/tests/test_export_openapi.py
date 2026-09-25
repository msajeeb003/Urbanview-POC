"""`python -m api.export_openapi` builds the schema the frontend generates its types from."""

import json

from api.export_openapi import build_schema, main


def test_schema_has_every_public_route_the_frontend_calls():
    paths = build_schema()["paths"]
    for route in (
        "/v1/municipality",
        "/v1/locate",
        "/v1/locate/parcel",
        "/v1/geocode",
        "/v1/panel",
        "/v1/feasibility",
        "/v1/source/value/{value_id}",
        "/v1/source/{document_id}/page/{page}",
        "/v1/events",
        "/v1/tiles/current",
        "/v1/orders",
        "/v1/orders/{reference}/status",
    ):
        assert route in paths, route


def test_main_writes_the_file(tmp_path):
    target = tmp_path / "openapi.json"
    assert main([str(target)]) == 0
    schemas = json.loads(target.read_text(encoding="utf-8"))["components"]["schemas"]
    assert "LocationResolution" in schemas
