"""Validation of the admin configuration payloads (no database) and the engine adapter's use of
absolute bounds: low ≤ expected ≤ high with both bounds or neither, percentages, dates,
e-mails, "nothing to change" updates."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from api.schemas.admin_config import (
    AssumptionsIn,
    AssumptionsUpdate,
    RateIn,
    StaffUserIn,
    StaffUserUpdate,
    ZoneParametersIn,
    ZoneParametersUpdate,
)
from core.engine.feasibility import Assumptions, MarketInputs, compute_feasibility


def rate(**overrides):
    return {"expected": 1000, **overrides}


BASE = {
    "land_rate": rate(),
    "build_rate": rate(),
    "design_rate": rate(),
    "sale_rate": rate(),
    "source": "Realitica, Estitor, Monstat",
}


@pytest.mark.parametrize(
    "bad",
    [
        {"expected": 1000, "low": 1100, "high": 1200},
        {"expected": 1000, "low": 900, "high": 950},
        {"expected": 1000, "low": 1100, "high": 900},
        {"expected": 1000, "low": 900},
        {"expected": 1000, "high": 1100},
        {"expected": 0},
        {"expected": 1000, "low": 0, "high": 1200},
        {"expected": 1000, "low": -10, "high": 1200},
        {"expected": 1000, "low": 900, "high": 1100, "kind": "absolute"},
    ],
    ids=[
        "low above expected",
        "high below expected",
        "reversed",
        "only low",
        "only high",
        "zero",
        "zero low",
        "negative low",
        "unknown key",
    ],
)
def test_a_rate_range_must_be_low_expected_high(bad):
    with pytest.raises(ValidationError):
        RateIn(**bad)


def test_rate_ranges_accept_ordered_and_equal_bounds():
    assert RateIn(expected=1000, low=900, high=1100).low == 900
    assert RateIn(expected=1000, low=1000, high=1000).high == 1000
    assert RateIn(expected=1000).low is None


def test_assumptions_payload():
    ok = AssumptionsIn(**BASE)
    assert (ok.range_low_factor, ok.range_high_factor, ok.zone_id) == (0.86, 1.15, None)
    with pytest.raises(ValidationError):
        AssumptionsIn(**BASE, range_low_factor=1.2)
    with pytest.raises(ValidationError):
        AssumptionsIn(**BASE, range_high_factor=0.9)
    with pytest.raises(ValidationError):
        AssumptionsIn(**{**BASE, "source": ""})
    with pytest.raises(ValidationError):
        # beyond the validator's one day of time-zone slack whatever the local clock says
        AssumptionsIn(**BASE, source_date=date.today() + timedelta(days=3))
    with pytest.raises(ValidationError):
        AssumptionsIn(**BASE, zone_id=0)
    with pytest.raises(ValidationError):
        AssumptionsIn(**BASE, unexpected=1)


def test_assumption_updates_need_a_change():
    with pytest.raises(ValidationError):
        AssumptionsUpdate()
    assert AssumptionsUpdate(sale_rate=rate()).sale_rate.expected == 1000
    assert "notes" in AssumptionsUpdate(notes=None).model_fields_set  # explicit null counts


@pytest.mark.parametrize(
    "bad",
    [
        {"zone_id": 1},
        {"zone_id": 1, "notes": "only a note"},
        {"zone_id": 1, "max_site_coverage_pct": 101},
        {"zone_id": 1, "max_far": -1},
        {"zone_id": 1, "max_floors": 101},
        {"zone_id": 1, "max_height_m": -2},
        {"zone_id": 1, "max_far": 2, "source_page": 3},
        {"zone_id": 1, "max_far": 2, "verified_on": str(date.today() + timedelta(days=3))},
        {"zone_id": 0, "max_far": 2},
        {"zone_id": 1, "max_far": 2, "extra": True},
    ],
    ids=[
        "no values",
        "note only",
        "coverage over 100",
        "negative FAR",
        "too many floors",
        "negative height",
        "page without document",
        "verified in the future",
        "bad zone id",
        "unknown key",
    ],
)
def test_zone_parameters_validation(bad):
    with pytest.raises(ValidationError):
        ZoneParametersIn(**bad)


def test_zone_parameters_accept_a_partial_set_with_a_source():
    ok = ZoneParametersIn(
        zone_id=2,
        max_far=1.5,
        max_site_coverage_pct=40,
        source_document_id=4,
        source_page=5,
        verified_on=date.today(),
    )
    assert ok.land_use is None and ok.max_far == 1.5
    with pytest.raises(ValidationError):
        ZoneParametersUpdate()
    assert ZoneParametersUpdate(max_floors=4).max_floors == 4


def test_staff_user_validation():
    user = StaffUserIn(email="  Ana.Novak@Example.com ", role="reviewer")
    assert user.email == "ana.novak@example.com"
    assert user.role.value == "reviewer"
    for bad in ("not-an-email", "ana@", "@example.com", "ana@example", "ana example@x.com"):
        with pytest.raises(ValidationError):
            StaffUserIn(email=bad, role="admin")
    with pytest.raises(ValidationError):
        StaffUserIn(email="ana@example.com", role="god")
    with pytest.raises(ValidationError):
        StaffUserIn(email="ana@example.com", role="admin", password="x")
    with pytest.raises(ValidationError):
        StaffUserUpdate()
    assert StaffUserUpdate(is_active=False).is_active is False


def test_adapter_uses_absolute_bounds_where_the_row_has_them():
    market = MarketInputs(
        land_rate_eur_m2=1350,
        build_rate_eur_m2=860,
        design_rate_eur_m2=90,
        sale_rate_eur_m2=2450,
        range_low_factor=0.86,
        range_high_factor=1.15,
        build_bounds=(800, 900),
        sale_bounds=(2300, 2600),
    )
    result = compute_feasibility(959.6, 3.2, 55, market, Assumptions())
    fields = {f.key: f for f in result.fields}
    rows = {r.key: r for r in result.cost_rows}
    gfa = 959.6 * 3.2
    saleable = gfa * 0.7
    assert fields["revenue_eur"].low == pytest.approx(saleable * 2300, abs=0.01)
    assert fields["revenue_eur"].expected == pytest.approx(saleable * 2450, abs=0.01)
    assert fields["revenue_eur"].high == pytest.approx(saleable * 2600, abs=0.01)
    assert rows["construction_cost_eur"].low == pytest.approx(gfa * 800, abs=0.01)
    assert rows["construction_cost_eur"].high == pytest.approx(gfa * 900, abs=0.01)
    # rates without absolute bounds keep the factors
    assert rows["land_value_eur"].low == pytest.approx(959.6 * 1350 * 0.86, abs=0.01)
    assert rows["land_value_eur"].high == pytest.approx(959.6 * 1350 * 1.15, abs=0.01)
