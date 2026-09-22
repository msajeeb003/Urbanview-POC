from __future__ import annotations

import pytest

from core.municipality import UnknownMunicipalityError, list_municipality_ids, load_profile


def test_podgorica_profile_loads():
    p = load_profile("podgorica")
    assert p.id == "podgorica" and p.country == "ME" and p.currency == "EUR"
    assert p.contains(19.2636, 42.4411)
    assert not p.contains(0.0, 0.0)
    assert p.terminology.site_coverage.abbreviation == "IZ"
    assert p.terminology.far.abbreviation == "II"
    assert p.terminology.cadastral_municipality.abbreviation == "KO"
    assert {s.id for s in p.sources} >= {
        "eregistri",
        "ekatastar",
        "emapa",
        "uzn_geoportal",
        "monstat",
    }
    assert "podgorica" in list_municipality_ids()


def test_unknown_municipality_raises():
    with pytest.raises(UnknownMunicipalityError):
        load_profile("atlantis")


async def test_municipality_endpoint(client):
    r = await client.get("/v1/municipality")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "podgorica"
    assert body["center"] == [19.2636, 42.4411]
    assert body["terminology"]["far"]["local_name"] == "indeks izgrađenosti"
    assert any(s["kind"] == "market" and s["id"] == "monstat" for s in body["sources"])
