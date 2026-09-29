"""A reviewer's correction is checked with the extraction contract's own rules
(``core.extraction.corrections``, the A2 check of 2026-09-29): numbers in the document's
conventions and the field's unit, impossible values refused, unusual ones only when confirmed,
floors in the plan's notation, a land use the document or the profile knows."""

from __future__ import annotations

import pytest

from core.extraction.corrections import (
    CorrectionRefused,
    check_correction,
    conventions_for,
)


@pytest.fixture(scope="module")
def conventions():
    found = conventions_for("podgorica")
    assert found is not None, "the Podgorica profile has an [extraction] section"
    return found


def refused(conventions, field_key, value, unit=None, **kw) -> str:
    with pytest.raises(CorrectionRefused) as info:
        check_correction(field_key, value, unit, conventions=conventions, **kw)
    return info.value.code


def test_numbers_follow_the_documents_conventions(conventions):
    far = check_correction("max_far", "2,5", conventions=conventions)
    assert (far.number, far.unit, far.text, far.rules) == (2.5, None, None, ("decimal_comma",))
    assert check_correction("max_far", 3.4, conventions=conventions).number == 3.4
    grouped = check_correction("planned_parcel_area_m2", "1.906,09", "m²", conventions=conventions)
    assert (grouped.number, grouped.unit) == (1906.09, "m²")
    # the contract's only arithmetic: ha -> m² (a printed unit wins over the sent one)
    hectares = check_correction("planned_parcel_area_m2", "0,19", "ha", conventions=conventions)
    assert (hectares.number, hectares.unit) == (1900.0, "m²")
    assert "ha_to_m2" in hectares.rules
    printed = check_correction("planned_parcel_area_m2", "0,19 ha", "m²", conventions=conventions)
    assert printed.number == 1900.0
    coverage = check_correction("max_site_coverage_pct", "40 %", conventions=conventions)
    assert (coverage.number, coverage.unit) == (40.0, "%")
    assert check_correction("max_height_m", "27,5", "m", conventions=conventions).unit == "m"


def test_a_non_numeric_far_and_impossible_values_are_refused(conventions):
    assert refused(conventions, "max_far", "abc") == "not_a_number"
    assert refused(conventions, "max_far", "2.5 or 3") == "not_a_number"
    assert refused(conventions, "max_far", -5) == "below_minimum"
    assert refused(conventions, "max_height_m", 0, "m") == "below_minimum"  # exclusive minimum
    assert refused(conventions, "planned_parcel_area_m2", 0, "m²") == "below_minimum"
    assert refused(conventions, "max_site_coverage_pct", 250, "%") == "above_maximum"
    assert refused(conventions, "min_green_area_pct", "101 %") == "above_maximum"
    assert refused(conventions, "max_far", 2, "%") == "unit_not_accepted"
    assert refused(conventions, "max_height_m", 20, "m²") == "unit_not_accepted"
    # a stated 0 is a real 0
    assert check_correction("max_far", 0, conventions=conventions).number == 0.0


def test_unusual_values_need_a_confirmation(conventions):
    assert refused(conventions, "max_far", 999) == "out_of_range"
    assert refused(conventions, "max_height_m", 450, "m") == "out_of_range"
    kept = check_correction("max_far", 999, conventions=conventions, confirm_out_of_range=True)
    assert (kept.number, kept.out_of_range) == (999.0, True)
    assert kept.details() == {"out_of_range_confirmed": True}


def test_floors_use_the_profiles_notation(conventions):
    floors = check_correction("max_floors", "Po + P + 6", conventions=conventions)
    assert floors.text == "Po+P+6"
    assert floors.floors is not None and (
        floors.floors.below_ground,
        floors.floors.above_ground,
    ) == (1, 7)
    assert check_correction("max_floors", "S+P+4+Pk", conventions=conventions).floors.attic == 1
    assert refused(conventions, "max_floors", "X+9") == "unknown_floor_notation"
    assert refused(conventions, "max_floors", "6+P") == "unknown_floor_notation"  # order matters
    assert refused(conventions, "max_floors", 5) == "not_a_text"


def test_land_use_must_be_known_to_the_document_or_the_profile(conventions):
    # the profile's land-use terms classify it
    housing = check_correction("land_use", "stanovanje", conventions=conventions)
    assert (housing.text, housing.land_use_class) == ("stanovanje", "residential")
    # a wording the document uses, matched without case or accents, stored as the document has it
    wording = "stanovanje sa djelatnostima"
    known = check_correction(
        "land_use",
        "Stanovanje  sa djelatnostima",
        conventions=conventions,
        known_wordings=[wording],
    )
    assert known.text == wording
    assert refused(conventions, "land_use", "XYZ-unknown-code") == "unknown_land_use"
    assert refused(conventions, "land_use", 7) == "not_a_text"
    # a legend code the document uses is known through its items
    assert (
        check_correction("land_use", "SS", conventions=conventions, known_wordings=["SS"]).text
        == "SS"
    )


def test_texts_are_trimmed_and_bounded(conventions):
    assert check_correction(
        "parking_requirement", "  1 PM /  stan ", conventions=conventions
    ).text == ("1 PM / stan")
    assert refused(conventions, "utilities", "x" * 501) == "too_long"
    assert refused(conventions, "utilities", 12) == "not_a_text"


def test_market_rates_are_positive_numbers(conventions):
    rate = check_correction(None, "850", "€/m²", conventions=conventions)
    assert (rate.number, rate.unit) == (850.0, "€/m²")
    assert refused(conventions, None, 0) == "below_minimum"
    assert refused(conventions, None, "cheap") == "not_a_number"


def test_without_a_profile_numbers_are_still_checked():
    assert check_correction("land_use", "anything", conventions=None).text == "anything"
    assert check_correction("max_floors", "Q+9", conventions=None).text == "Q+9"
    with pytest.raises(CorrectionRefused):
        check_correction("max_far", "abc", conventions=None)


def test_a_refusal_is_one_422_problem(conventions):
    with pytest.raises(CorrectionRefused) as info:
        check_correction("max_far", 999, conventions=conventions)
    problem = info.value.problem()
    assert problem["loc"] == ["body", "value"] and problem["type"] == "out_of_range"
    assert problem["ctx"] == {"minimum": 0, "maximum": 20}
    assert "usual range" in problem["msg"]
