"""Land use on the public map (``core.land_use``): the legend name of a code the plan prints, the
colour group of the land-use layer, and the planned-parcel number key of the search."""

from __future__ import annotations

import pytest

from api.services.resolver import NoDataResolver, urban_parcel_key
from core.extraction.normalise import compile_land_use_terms
from core.land_use import legend, legend_name, map_category, profile_classifier
from core.municipality import load_extraction_profile, load_profile


def test_a_code_is_named_by_the_plans_legend():
    codes = legend(load_extraction_profile("podgorica").land_use_codes)
    assert legend_name("SS", codes) == "stanovanje srednje gustine"
    assert legend_name(" ss ", codes) == "stanovanje srednje gustine"
    assert legend_name("MN", codes) == "mješovita namjena – stanovanje sa poslovanjem"
    # a wording is not a code, and a code the legend does not name stays as printed
    assert legend_name("stanovanje", codes) is None
    assert legend_name("U", codes) == "usluge ishrane i pića, ugostiteljstvo"  # the legend's UK
    assert legend_name("SR", codes) == "sport i rekreacija"  # the legend's SKR
    assert legend_name("XX", codes) is None
    assert legend_name(None, codes) is None
    assert legend_name(12.0, codes) is None


@pytest.mark.parametrize(
    ("wording", "group"),
    [
        ("stanovanje", "res"),
        ("SS", "res"),
        ("SV", "res"),
        ("stanovanje sa djelatn.", "mix"),
        ("MN", "mix"),
        ("centralne djelatnosti", "com"),
        ("CD", "com"),
        ("školstvo i soc.zaštita", "pub"),
        ("površine saobraćajne infrastrukture", "pub"),
        ("VO", "pub"),
        ("K", "pub"),
        ("sport i rekreacija", "grn"),
        ("SR", "grn"),
        ("U", "com"),
        ("TS", "pub"),
    ],
)
def test_every_wording_of_the_two_poc_plans_has_a_colour_group(wording, group):
    assert profile_classifier("podgorica")(wording) == group


def test_an_unknown_wording_has_no_group():
    classify = profile_classifier("podgorica")
    assert classify("XX") is None  # a code no legend names
    assert classify("nepoznata namjena") is None
    rules = compile_land_use_terms([("stanovanj", "residential")])
    assert map_category("SS", rules) is None  # a code without a legend has no class
    assert map_category("SS", rules, {"ss": "stanovanje srednje gustine"}) == "res"


def test_urban_parcel_number_key():
    assert urban_parcel_key("UP 40", "UP") == "40"
    assert urban_parcel_key("up40", "UP") == "40"
    assert urban_parcel_key(" up  c2962 ", "UP") == "C2962"
    assert urban_parcel_key("40", "UP") == "40"
    assert urban_parcel_key("UP", "UP") == ""


async def test_without_planning_data_no_urban_parcel_is_found():
    found = await NoDataResolver(load_profile("podgorica")).find_urban_parcels(number="UP 40")
    assert (found.number, found.results) == ("40", [])
