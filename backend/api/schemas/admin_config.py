"""Schemas for the admin configuration API: financial assumptions (versioned and effective-dated
per zone), zone parameter sets (versioned per zone) and staff users. The validation rules live
here: a numeric range is ``low ≤ expected ≤ high`` with both bounds or neither, percentages are
0–100, a saleable share is (0, 1], dates of record (source, verification) are not in the future
(one day of time-zone slack), an e-mail looks like one. An assumptions set's ``effective_from``
is today or later in the municipality's time zone; the service checks it with its own clock."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas.panel import RateRange
from core.assumptions import AssumptionStatus
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
        default=None,
        gt=0,
        description=(
            "null = the municipality-wide row: its range factors widen single-figure market "
            "imports; it never supplies a zone's figures"
        ),
    )
    land_rate: RateIn = Field(description="Land value per m² of parcel area")
    build_rate: RateIn = Field(description="Construction cost per m² GFA")
    design_rate: RateIn = Field(description="Design & documentation per m² GFA")
    sale_rate: RateIn = Field(description="Selling price per m² saleable area")
    range_low_factor: float = Field(default=0.86, gt=0, le=1)
    range_high_factor: float = Field(default=1.15, ge=1, le=5)
    saleable_share: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="The zone's default saleable share of GFA (0–1); null = the product's 0.70",
    )
    source: str = Field(
        min_length=1, max_length=200, description="e.g. Realitica, Estitor, Monstat"
    )
    source_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    effective_from: date | None = Field(
        default=None,
        description=(
            "The municipality's local date the set applies from: today (the default) or later; "
            "a later date schedules it (the panel keeps the live set until then)"
        ),
    )

    @field_validator("source_date")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError("the date is in the future")
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
    saleable_share: float | None = Field(default=None, gt=0, le=1)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    source_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    effective_from: date | None = Field(
        default=None, description="Today (the default) or later; never carried over"
    )

    @field_validator("source_date")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError("the date is in the future")
        return value

    @model_validator(mode="after")
    def _something_changes(self) -> AssumptionsUpdate:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class AssumptionsOut(BaseModel):
    id: int
    zone_id: int | None = Field(
        description="null = the municipality-wide row (range factors only, never a zone's figures)"
    )
    zone_name: str | None = None
    version: int
    status: AssumptionStatus = Field(
        description=(
            "live: what the panel and feasibility read today (the latest effective date on or "
            "before today, the newest version on a tie); scheduled: dated after today; "
            "superseded: replaced by a later date or a newer version; retired"
        )
    )
    is_current: bool = Field(
        description="The newest version of the zone (the head of its history, what PUT builds on)"
    )
    supersedes_id: int | None = None
    land_rate: RateRange
    build_rate: RateRange
    design_rate: RateRange
    sale_rate: RateRange
    range_low_factor: float
    range_high_factor: float
    saleable_share: float | None = Field(
        default=None, description="The zone's default saleable share; null = the product's 0.70"
    )
    source: str | None = None
    source_date: date | None = None
    notes: str | None = None
    effective_from: date = Field(description="The date the set states it applies from")
    applies_from: date = Field(
        description=(
            "Its place on the timeline: the effective date, never before the local day it was "
            "saved (a backdated market input applies from its approval)"
        )
    )
    rate_sources: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per rate, once reviewed market inputs set it: source, source_date, market_data_id, "
            "import_id, range_basis, effective_from (or set_by admin after a manual edit)"
        ),
    )
    created_by: str | None = None
    created_at: datetime
    retired_at: datetime | None = None
    retired_by: str | None = None


class AssumptionsList(BaseModel):
    items: list[AssumptionsOut]
    today: date = Field(description="The municipality's local date the statuses are computed for")
    timezone: str


class AssumptionsBatchIn(BaseModel):
    """Several zones' new sets saved together (the console's "Save changes"): one transaction,
    all or nothing, one version and one audit row per set."""

    model_config = ConfigDict(extra="forbid")

    effective_from: date | None = Field(
        default=None,
        description="Applies to every set that states none: today (the default) or later",
    )
    sets: list[AssumptionsIn] = Field(min_length=1, max_length=100)


class AssumptionsBatchOut(BaseModel):
    items: list[AssumptionsOut]


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


class StaffMeOut(BaseModel):
    """The signed-in principal: a staff user, or a configured service token."""

    id: int | None = Field(default=None, description="staff_users.id; null for a service token")
    email: str | None = None
    display_name: str | None = None
    role: Role
    subject: str
    via: Literal["session", "token"]


class StaffUserList(BaseModel):
    items: list[StaffUserOut]
