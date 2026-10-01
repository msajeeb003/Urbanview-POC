"""Land use as the public map shows it.

A plan states a parcel's land use either in words (``stanovanje sa djelatn.``) or as a code of its
legend (``SS``). The published value stays what the plan prints; this module only reads it:

- ``legend_name``: the name the plan's legend gives a code (the profile's
  ``[extraction.land_use_codes]``), shown beside the code in the parcel panel so a code never
  stands alone when its meaning is known;
- ``map_category``: the colour group of the land-use layer, one of the five zone types of the
  map's legend (``res`` | ``com`` | ``mix`` | ``pub`` | ``grn``), from the land-use class of the
  wording (the profile's ``land_use_terms``, the same rules the extraction validator uses). A
  wording no rule matches has no category and is drawn in the neutral fallback colour.

Product-wide; everything place-specific (the wording patterns, the codes) is the profile's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from core.extraction.normalise import LandUseRule, classify_land_use, compile_land_use_terms
from core.extraction.schema import LandUseClass
from core.extraction.textmatch import words_fold

# The land-use layer has the legend of the zones layer: five colour groups.
ZONE_TYPE_OF_CLASS: dict[LandUseClass, str] = {
    LandUseClass.residential: "res",
    LandUseClass.residential_mixed: "mix",
    LandUseClass.mixed_use: "mix",
    LandUseClass.central_activities: "com",
    LandUseClass.tourism: "com",
    LandUseClass.industry: "com",
    LandUseClass.education_social: "pub",
    LandUseClass.health: "pub",
    LandUseClass.culture: "pub",
    LandUseClass.religious: "pub",
    LandUseClass.traffic_infrastructure: "pub",
    LandUseClass.utility_infrastructure: "pub",
    LandUseClass.special_purpose: "pub",
    LandUseClass.sport_recreation: "grn",
    LandUseClass.green_space: "grn",
}


def code_key(text: str) -> str:
    """How a legend code is looked up: case- and accent-folded, no spaces (``S S`` = ``ss``)."""
    return words_fold(text).replace(" ", "")


def legend(codes: Mapping[str, str] | None) -> dict[str, str]:
    """The profile's code table keyed for lookup."""
    return {code_key(code): name for code, name in (codes or {}).items()}


def legend_name(value: object, codes: Mapping[str, str]) -> str | None:
    """The legend's name of a land-use value that is a code (``codes`` from :func:`legend`)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return codes.get(code_key(value))


def map_category(
    wording: str, rules: Sequence[LandUseRule], codes: Mapping[str, str] | None = None
) -> str | None:
    """The land-use layer's colour group of a wording or code, or None when no rule matches."""
    category = classify_land_use(wording, rules, codes)
    return ZONE_TYPE_OF_CLASS.get(category) if category is not None else None


def profile_classifier(municipality_id: str) -> Callable[[str], str | None]:
    """``wording -> colour group`` with the municipality profile's rules and codes; a profile
    without an ``[extraction]`` table classifies nothing."""
    from core.municipality import UnknownMunicipalityError, load_extraction_profile

    try:
        extraction = load_extraction_profile(municipality_id)
    except UnknownMunicipalityError:
        extraction = None
    if extraction is None:
        return lambda _wording: None
    rules = compile_land_use_terms((t.pattern, t.category) for t in extraction.land_use_terms)
    codes = legend(extraction.land_use_codes)
    return lambda wording: map_category(wording, rules, codes)
