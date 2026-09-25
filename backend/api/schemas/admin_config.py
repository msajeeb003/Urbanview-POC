"""Schemas for the admin configuration API: financial assumptions (versioned per zone), zone
parameter sets (versioned per zone) and staff users. The validation rules live here: a numeric
range is ``low ≤ expected ≤ high`` with both bounds or neither, percentages are 0–100, dates
are not in the future (one day of time-zone slack), an e-mail looks like one."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.panel import RateRange
from core.auth import Role

BoundsKind = Literal["absolute", "multiplier"]
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# --- financial assumptions ------------------------------------------------------------------------


class RateIn(BaseModel):
    """A rate in €/m² with optional absolute bounds; without them the range factors apply."""

    model_config = ConfigDict(extra="forbid")

    expected: float = Field(gt=0, le=1_000_000)
    low: float | None = Field(default=None, gt=0, le=1_000_000)
    high: float | None = Field(default=None, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def _low_expected_high(self) -> RateIn:
        if (self.low is None) != (self.high is None):
            raise ValueError("give both low and high bounds, or neither")
        if self.low is not None and self.high is not None:
            if not self.low <= self.expected <= self.high:
                raise ValueError("bounds must satisfy low ≤ expected ≤ high")
        return self


class AssumptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: int | None = Field(
        default=None, gt=0, description="null = the municipality-wide default row"
    )
    land_rate: RateIn = Field(description="Land value per m² of parcel area")
    build_rate: RateIn = Field(description="Construction cost per m² GFA")
    design_rate: RateIn = Field(description="Design & documentation per m² GFA")
    sale_rate: RateIn = Field(description="Selling price per m² saleable area")
    range_low_factor: float = Field(default=0.86, gt=0, le=1)
    range_high_factor: float = Field(default=1.15, ge=1, le=5)
    source: str = Field(
        min_length=1, max_length=200, description="e.g. Realitica, Estitor, Monstat"
    )
    source_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("source_date")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError("source_date is in the future")
        return value


class AssumptionsUpdate(BaseModel):
    """Fields of a new version based on the current row; omitted fields are carried over."""

    model_config = ConfigDict(extra="forbid")

    land_rate: RateIn | None = None
    build_rate: RateIn | None = None
    design_rate: RateIn | None = None
    sale_rate: RateIn | None = None
    range_low_factor: float | None = Field(default=None, gt=0, le=1)
    range_high_factor: float | None = Field(default=None, ge=1, le=5)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    source_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _something_changes(self) -> AssumptionsUpdate:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class AssumptionsOut(BaseModel):
    id: int
    zone_id: int | None = Field(description="null = municipality-wide default")
    zone_name: str | None = None
    version: int
    is_current: bool = Field(description="The row the panel and feasibility read")
    supersedes_id: int | None = None
    land_rate: RateRange
    build_rate: RateRange
    design_rate: RateRange
    sale_rate: RateRange
    range_low_factor: float
    range_high_factor: float
    source: str | None = None
    source_date: date | None = None
    notes: str | None = None
    created_by: str | None = None
    created_at: datetime
    retired_at: datetime | None = None
    retired_by: str | None = None


class AssumptionsList(BaseModel):
    items: list[AssumptionsOut]


# --- zone parameter sets --------------------------------------------------------------------------

VALUE_FIELDS = ("land_use", "max_far", "max_site_coverage_pct", "max_height_m", "max_floors")


class ZoneParametersBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    land_use: str | None = Field(default=None, max_length=300)
    max_far: float | None = Field(default=None, ge=0, le=20, description="II, typical")
    max_site_coverage_pct: float | None = Field(default=None, ge=0, le=100, description="IZ %")
    max_height_m: float | None = Field(default=None, ge=0, le=500)
    max_floors: int | None = Field(default=None, ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)
    source_document_id: int | None = Field(default=None, gt=0)
    source_page: int | None = Field(default=None, ge=1)
    source_note: str | None = Field(default=None, max_length=500)
    verified_on: date | None = None
    verified_by: str | None = Field(default=None, max_length=200)

    @field_validator("verified_on")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError("verified_on is in the future")
        return value

    @model_validator(mode="after")
    def _page_needs_a_document(self) -> ZoneParametersBase:
        if self.source_page is not None and self.source_document_id is None:
            raise ValueError("source_page needs a source_document_id")
        return self


class ZoneParametersIn(ZoneParametersBase):
    zone_id: int = Field(gt=0)

    @model_validator(mode="after")
    def _at_least_one_value(self) -> ZoneParametersIn:
        if all(getattr(self, name) is None for name in VALUE_FIELDS):
            raise ValueError("give at least one of " + ", ".join(VALUE_FIELDS))
        return self


class ZoneParametersUpdate(ZoneParametersBase):
    """Fields of a new version based on the current row; omitted fields are carried over."""

    @model_validator(mode="after")
    def _something_changes(self) -> ZoneParametersUpdate:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class ZoneParameterSource(BaseModel):
    document_id: int
    document_name: str | None = None
    page: int | None = None
    note: str | None = None
    registry_url: str | None = None


class ZoneParametersOut(BaseModel):
    id: int
    zone_id: int
    zone_name: str | None = None
    version: int
    is_current: bool
    supersedes_id: int | None = None
    land_use: str | None = None
    max_far: float | None = None
    max_site_coverage_pct: float | None = None
    max_height_m: float | None = None
    max_floors: int | None = None
    notes: str | None = None
    source: ZoneParameterSource | None = None
    verified_on: date | None = None
    verified_by: str | None = None
    created_by: str
    created_at: datetime
    retired_at: datetime | None = None
    retired_by: str | None = None


class ZoneParametersList(BaseModel):
    items: list[ZoneParametersOut]


# --- staff users ----------------------------------------------------------------------------------


class StaffUserIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    role: Role = Field(description="admin | reviewer (expert reviewer) | expert")
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def _looks_like_an_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("not an e-mail address")
        return value


class StaffUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    display_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _something_changes(self) -> StaffUserUpdate:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class StaffUserOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None
    open_sessions: int = Field(description="Sessions neither revoked nor expired")


class StaffUserList(BaseModel):
    items: list[StaffUserOut]
