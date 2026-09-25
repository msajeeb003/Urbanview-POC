"""`GET /v1/zones`: the zone index the search box matches zone names against."""

from __future__ import annotations

import json

from api.services.zone_index import ZONE_INDEX_SQL, ZoneIndexService, build_index
from tests.helpers import make_app, make_client, make_settings

RING = [[19.25, 42.43], [19.27, 42.43], [19.26, 42.45], [19.25, 42.43]]


def _row(**overrides):
    row = {
        "id": 3,
        "name": "Centar",
        "zone_type": "mix",
        "covered": True,
        "min_lng": 19.2551234567,
        "min_lat": 42.4381,
        "max_lng": 19.2702,
        "max_lat": 42.4459,
        "lng": 19.2634567891,
        "lat": 42.4412,
        "geometry": json.dumps({"type": "MultiPolygon", "coordinates": [[RING]]}),
    }
    row.update(overrides)
    return row


def test_build_index_rounds_and_keeps_order():
    index = build_index([_row(), _row(id=7, name="Zabjelo", zone_type=None, covered=None)])
    first, second = index.zones
    assert first.bbox == (19.255123, 42.4381, 19.2702, 42.4459)
    assert first.centroid.lng == 19.263457 and first.covered is True
    assert (second.id, second.zone_type, second.covered) == (7, None, False)
    assert first.geometry["type"] == "MultiPolygon"
    assert first.geometry["coordinates"][0][0][0] == [19.25, 42.43]


def test_coverage_rule_is_the_tiles_rule():
    from jobs.publish_layers import ZONE_COVERED

    assert ZONE_COVERED in str(ZONE_INDEX_SQL)
    assert "municipality_id = :m" in str(ZONE_INDEX_SQL)


async def test_route_serves_the_index_with_a_short_cache():
    app = make_app(make_settings())

    class Fake(ZoneIndexService):
        def __init__(self):
            pass

        async def index(self):
            return build_index([_row()])

    async with app.router.lifespan_context(app), make_client(app) as client:
        app.state.zone_index_service = Fake()
        res = await client.get("/v1/zones")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"
    body = res.json()
    assert body["zones"][0]["name"] == "Centar" and body["zones"][0]["bbox"][0] == 19.255123


async def test_route_without_postgis_answers_503():
    app = make_app(make_settings())
    async with app.router.lifespan_context(app), make_client(app) as client:
        res = await client.get("/v1/zones")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "service_unavailable"
