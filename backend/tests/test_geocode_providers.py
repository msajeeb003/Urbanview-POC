"""Provider adapters against a mocked HTTP transport: request shape (scope, policy headers) and
the mapping from each provider's taxonomy onto the compact result vocabulary."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from core.geocode import PROVIDERS, build_provider, user_agent_for
from core.geocode.base import (
    BoundingBox,
    GeocodeHit,
    GeocodeProviderError,
    SearchScope,
    classify,
    compose_hit,
)
from core.geocode.nominatim import NominatimProvider
from core.geocode.photon import PhotonProvider
from tests.helpers import make_settings

SCOPE = SearchScope(
    bbox=BoundingBox(19.10, 42.33, 19.45, 42.55),
    center=(19.2636, 42.4411),
    country_code="ME",
    language="sr-Latn-ME",
)
UA = "urbanview-api/0.1.0 (+ops@example.com)"


def mock_transport(payload: Any, status: int = 200):
    """A transport answering every request with ``payload`` (or raising it); records requests."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(handler), requests


def hit(label, address, lat, lng, kind, country="ME"):
    return GeocodeHit(label, address, lat, lng, kind, country)


EXPECTED = [
    hit("Ulica Slobode 12", "Stara Varoš, Podgorica", 42.4402, 19.2612, "address"),
    hit("Bulevar Svetog Petra Cetinjskog", "Centar, Podgorica", 42.4425, 19.2580, "street"),
    hit("Blok 5", "Podgorica", 42.4380, 19.2450, "place"),
    hit("Delta City", "Cetinjski put 5, Podgorica", 42.4310, 19.2380, "poi"),
]

NOMINATIM_ITEMS: list[Any] = [
    {
        "lat": "42.4402",
        "lon": "19.2612",
        "category": "place",
        "type": "house",
        "name": "",
        "address": {
            "house_number": "12",
            "road": "Ulica Slobode",
            "suburb": "Stara Varoš",
            "city": "Podgorica",
            "country_code": "me",
        },
    },
    {
        "lat": "42.4425",
        "lon": "19.2580",
        "category": "highway",
        "type": "primary",
        "name": "Bulevar Svetog Petra Cetinjskog",
        "address": {
            "road": "Bulevar Svetog Petra Cetinjskog",
            "suburb": "Centar",
            "city": "Podgorica",
            "country_code": "me",
        },
    },
    {
        "lat": "42.4380",
        "lon": "19.2450",
        "category": "place",
        "type": "suburb",
        "name": "Blok 5",
        "address": {"suburb": "Blok 5", "city": "Podgorica", "country_code": "me"},
    },
    {
        "lat": "42.4310",
        "lon": "19.2380",
        "category": "shop",
        "type": "mall",
        "name": "Delta City",
        "address": {
            "house_number": "5",
            "road": "Cetinjski put",
            "city": "Podgorica",
            "country_code": "me",
        },
    },
    {"lat": "n/a", "lon": "19.2", "category": "place", "type": "house", "address": {}},
    {  # nameless building without a street: useless as a suggestion
        "lat": "42.44",
        "lon": "19.26",
        "category": "building",
        "type": "yes",
        "name": "",
        "address": {"city": "Podgorica", "country_code": "me"},
    },
    "not an object",
]


def feature(lng, lat, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": props,
    }


PHOTON_FEATURES: list[Any] = [
    feature(
        19.2612,
        42.4402,
        {
            "osm_key": "building",
            "osm_value": "yes",
            "type": "house",
            "housenumber": "12",
            "street": "Ulica Slobode",
            "district": "Stara Varoš",
            "city": "Podgorica",
            "countrycode": "ME",
        },
    ),
    feature(
        19.2580,
        42.4425,
        {
            "osm_key": "highway",
            "osm_value": "primary",
            "type": "street",
            "name": "Bulevar Svetog Petra Cetinjskog",
            "district": "Centar",
            "city": "Podgorica",
            "countrycode": "ME",
        },
    ),
    feature(
        19.2450,
        42.4380,
        {
            "osm_key": "place",
            "osm_value": "suburb",
            "type": "district",
            "name": "Blok 5",
            "city": "Podgorica",
            "countrycode": "ME",
        },
    ),
    feature(
        19.2380,
        42.4310,
        {
            "osm_key": "shop",
            "osm_value": "mall",
            "type": "house",
            "name": "Delta City",
            "housenumber": "5",
            "street": "Cetinjski put",
            "city": "Podgorica",
            "countrycode": "ME",
        },
    ),
    feature(
        19.2636,
        42.4411,
        {
            "osm_key": "place",
            "osm_value": "city",
            "type": "city",
            "name": "Podgorica",
            "city": "Podgorica",
            "countrycode": "ME",
        },
    ),
    {"type": "Feature", "geometry": None, "properties": {"name": "broken"}},
]


# --- Nominatim ---------------------------------------------------------------------------------


async def test_nominatim_request_carries_scope_and_policy_headers():
    transport, requests = mock_transport(NOMINATIM_ITEMS)
    provider = NominatimProvider(
        user_agent=UA, timeout_ms=1500, transport=transport, email="ops@example.com"
    )
    hits = await provider.search("Delta", limit=16, scope=SCOPE)
    await provider.aclose()
    (request,) = requests
    assert request.url.host == "nominatim.openstreetmap.org"
    assert request.url.path == "/search"
    assert dict(request.url.params) == {
        "q": "Delta",
        "format": "jsonv2",
        "addressdetails": "1",
        "limit": "16",
        "countrycodes": "me",
        "viewbox": "19.1,42.33,19.45,42.55",
        "bounded": "1",
        "dedupe": "1",
        "accept-language": "sr-Latn-ME",
        "email": "ops@example.com",
    }
    assert request.headers["User-Agent"] == UA
    assert request.headers["Accept"] == "application/json"
    assert hits == EXPECTED


async def test_nominatim_omits_language_and_email_when_unset_and_honours_the_base_url():
    transport, requests = mock_transport([])
    provider = NominatimProvider(
        user_agent=UA, timeout_ms=1500, transport=transport, base_url="https://geo.example.org/nom/"
    )
    scope = SearchScope(bbox=SCOPE.bbox, center=SCOPE.center, country_code="ME", language=None)
    assert await provider.search("x", limit=5, scope=scope) == []
    await provider.aclose()
    (request,) = requests
    assert str(request.url).startswith("https://geo.example.org/nom/search?")
    assert "accept-language" not in request.url.params
    assert "email" not in request.url.params
    assert provider.min_interval_ms == 1000  # public usage policy: one call per second


# --- Photon ------------------------------------------------------------------------------------


async def test_photon_request_carries_scope_and_bias():
    transport, requests = mock_transport({"type": "FeatureCollection", "features": PHOTON_FEATURES})
    provider = PhotonProvider(user_agent=UA, timeout_ms=1500, transport=transport)
    hits = await provider.search("Delta", limit=16, scope=SCOPE)
    await provider.aclose()
    (request,) = requests
    assert request.url.host == "photon.komoot.io"
    assert request.url.path == "/api/"
    assert dict(request.url.params) == {
        "q": "Delta",
        "limit": "16",
        "bbox": "19.1,42.33,19.45,42.55",
        "lat": "42.4411",
        "lon": "19.2636",
    }
    assert request.headers["User-Agent"] == UA
    assert hits == [*EXPECTED, hit("Podgorica", None, 42.4411, 19.2636, "place")]
    assert provider.min_interval_ms == 0


@pytest.mark.parametrize(
    ("language", "lang_param"),
    [("sr-Latn-ME", None), ("en-GB", "en"), ("de", "de"), (None, None)],
)
async def test_photon_sends_lang_only_for_languages_it_supports(language, lang_param):
    transport, requests = mock_transport({"features": []})
    provider = PhotonProvider(user_agent=UA, timeout_ms=1500, transport=transport)
    scope = SearchScope(bbox=SCOPE.bbox, center=SCOPE.center, country_code="ME", language=language)
    assert await provider.search("x", limit=5, scope=scope) == []
    await provider.aclose()
    assert requests[0].url.params.get("lang") == lang_param


# --- failures and the shared mapping -----------------------------------------------------------


@pytest.mark.parametrize("provider_cls", [NominatimProvider, PhotonProvider])
@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"features": []}, 429),
        ("not json", 200),
        ({"unexpected": True}, 200),
        (httpx.ConnectError("refused"), 200),
        (httpx.ReadTimeout("slow"), 200),
    ],
    ids=["http 429", "not json", "wrong shape", "connection error", "timeout"],
)
async def test_every_failure_is_a_provider_error(provider_cls, payload, status):
    transport, _ = mock_transport(payload, status)
    provider = provider_cls(user_agent=UA, timeout_ms=1500, transport=transport)
    with pytest.raises(GeocodeProviderError):
        await provider.search("x", limit=5, scope=SCOPE)
    await provider.aclose()


@pytest.mark.parametrize(
    ("key", "value", "housenumber", "kind"),
    [
        ("highway", "residential", False, "street"),
        ("highway", "bus_stop", False, "poi"),
        ("place", "house", True, "address"),
        ("building", "yes", True, "address"),
        ("building", "yes", False, "other"),
        ("amenity", "cafe", True, "poi"),
        ("shop", "mall", False, "poi"),
        ("place", "suburb", False, "place"),
        ("boundary", "administrative", False, "place"),
        ("natural", "water", False, "other"),
        (None, None, True, "address"),
        (None, None, False, "other"),
    ],
)
def test_classify_maps_osm_tags_to_the_compact_kinds(key, value, housenumber, kind):
    assert classify(key, value, has_housenumber=housenumber) == kind


def test_compose_hit_builds_the_label_and_a_non_repeating_address_line():
    poi = compose_hit(
        name="Delta City",
        street="Cetinjski put",
        housenumber="5",
        locality=None,
        city="Podgorica",
        lat=1.0,
        lng=2.0,
        kind="poi",
        country_code="me",
    )
    assert (poi.label, poi.address, poi.country_code) == (
        "Delta City",
        "Cetinjski put 5, Podgorica",
        "ME",
    )
    street = compose_hit(
        name="Ulica X",
        street="Ulica X",
        housenumber=None,
        locality="Ulica X",
        city="Podgorica",
        lat=1.0,
        lng=2.0,
        kind="street",
        country_code=None,
    )
    assert (street.label, street.address, street.country_code) == ("Ulica X", "Podgorica", None)
    nameless = compose_hit(
        name=None,
        street=None,
        housenumber=None,
        locality="Blok 5",
        city="Podgorica",
        lat=1.0,
        lng=2.0,
        kind="other",
        country_code="ME",
    )
    assert nameless is None


# --- configuration -> provider -----------------------------------------------------------------


async def test_build_provider_defaults_to_photon_with_an_identifying_user_agent():
    provider = build_provider(make_settings())
    assert isinstance(provider, PhotonProvider)
    assert provider.base_url == "https://photon.komoot.io"
    assert provider.user_agent == "urbanview-api/0.1.0"
    await provider.aclose()


async def test_build_provider_nominatim_from_settings():
    settings = make_settings(
        geocoder_provider="nominatim",
        geocoder_base_url="https://geo.example.org/",
        geocoder_contact="ops@example.com",
    )
    provider = build_provider(settings)
    assert isinstance(provider, NominatimProvider)
    assert provider.base_url == "https://geo.example.org"
    assert provider.email == "ops@example.com"
    assert provider.user_agent == UA
    await provider.aclose()


async def test_a_contact_url_identifies_the_operator_but_is_not_an_email():
    settings = make_settings(geocoder_provider="nominatim", geocoder_contact="https://urbanview.me")
    provider = build_provider(settings)
    assert provider.email is None
    assert provider.user_agent == "urbanview-api/0.1.0 (+https://urbanview.me)"
    await provider.aclose()


def test_an_explicit_user_agent_wins():
    settings = make_settings(geocoder_user_agent="MyApp/2.0", geocoder_contact="x@example.com")
    assert user_agent_for(settings) == "MyApp/2.0"


def test_an_unknown_provider_is_a_configuration_error():
    with pytest.raises(ValidationError):
        make_settings(geocoder_provider="google")


def test_blank_env_values_mean_default():
    settings = make_settings(
        geocoder_base_url="", geocoder_contact=" ", geocoder_min_interval_ms=""
    )
    assert settings.geocoder_base_url is None
    assert settings.geocoder_contact is None
    assert settings.geocoder_min_interval_ms is None


def test_registry_lists_both_osm_adapters():
    assert set(PROVIDERS) == {"photon", "nominatim"}
