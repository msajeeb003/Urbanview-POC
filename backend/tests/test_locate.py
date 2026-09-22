"""Locate endpoints without a database (no-data resolver): the never-an-error contract."""

from __future__ import annotations

from api.routers.v1.locate import normalise_parcel_ref


async def test_point_without_data_is_200_uncovered(client):
    r = await client.get("/v1/locate", params={"lat": 42.4411, "lng": 19.2636})
    assert r.status_code == 200
    body = r.json()
    assert body["covered"] is False
    assert body["coverage"] == {
        "status": "uncovered",
        "reason": "no_adopted_plan",
        "message": "No adopted planning document covers this location.",
    }
    assert body["zone"] is None and body["planning_document"] is None
    assert body["cadastral_parcel"] is None and body["urban_parcel"] is None
    assert body["urban_parcels"] == []
    assert body["query"]["lat"] == 42.4411 and body["query"]["lng"] == 19.2636


async def test_point_outside_municipality_is_200_not_404(client):
    r = await client.get("/v1/locate", params={"lat": 0, "lng": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["covered"] is False
    assert body["coverage"]["reason"] == "outside_municipality"
    assert "Podgorica" in body["coverage"]["message"]


async def test_malformed_coordinates_are_a_validation_error(client):
    r = await client.get("/v1/locate", params={"lat": 95, "lng": 19.26})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_parcel_lookup_requires_ko(client):
    r = await client.get("/v1/locate/parcel", params={"number": "1042"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    r = await client.get("/v1/locate/parcel", params={"ko": "   ", "number": "1042"})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["loc"] == ["query", "ko"]


async def test_parcel_lookup_without_data_is_200_not_found(client):
    r = await client.get("/v1/locate/parcel", params={"ko": "Podgorica I", "number": "1042"})
    assert r.status_code == 200
    body = r.json()
    assert body["covered"] is False
    assert body["coverage"]["reason"] == "parcel_not_found"
    assert body["query"] == {
        "mode": "parcel",
        "municipality_id": "podgorica",
        "lat": None,
        "lng": None,
        "ko": "Podgorica I",
        "parcel_number": "1042",
        "sub_number": None,
    }


def test_normalise_parcel_ref_splits_number_and_sub():
    assert normalise_parcel_ref(" Podgorica   I ", " 1042 ", None) == ("Podgorica I", "1042", None)
    assert normalise_parcel_ref("Podgorica I", "1042/3", None) == ("Podgorica I", "1042", "3")
    assert normalise_parcel_ref("Podgorica I", "1042", "") == ("Podgorica I", "1042", None)
    assert normalise_parcel_ref("Podgorica I", "1042/3", "9") == ("Podgorica I", "1042/3", "9")
