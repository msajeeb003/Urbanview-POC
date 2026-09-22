"""POST /v1/feasibility without a database (no-data mode): the route exists and reports 503.

The calculation itself is covered by the PostGIS integration tests
(``tests/integration/test_feasibility_route.py``) and the shared-fixture parity tests.
"""

from __future__ import annotations


async def test_route_needs_the_planning_database(client):
    r = await client.post(
        "/v1/feasibility", json={"parcel_id": 1, "type": "urban", "assumptions": {}}
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"


async def test_route_is_documented(client):
    spec = (await client.get("/openapi.json")).json()
    assert "post" in spec["paths"]["/v1/feasibility"]
    schema = spec["components"]["schemas"]["EditedAssumptions"]
    assert set(schema["properties"]) == {
        "construction_cost_per_m2",
        "selling_price_per_m2",
        "saleable_share",
    }
