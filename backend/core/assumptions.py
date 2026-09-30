"""Effective-dated financial assumptions: which version of a zone's market inputs applies on a day.

Every zone's ``financial_assumptions`` versions form a timeline. A version applies from its
``effective_from`` (a date in the municipality's time zone, profile ``timezone``), but never
before the day it was saved: its place on the timeline is ``applies_from = GREATEST(
effective_from, local date of created_at)``. So a set dated after today is **scheduled** (nothing
reads it before its date, and no job has to switch it on: every reader applies the rule below with
the database's own clock), while a version stating a past date (an approved market input dated to
its figures' reference period) applies from the day it was approved and keeps its stated date. A
version applies until a version with a later ``applies_from`` takes effect; on the same day the
newer version wins. A retired version keeps its place and applies nothing, so the zone has no
market figures until a later version takes effect. ``is_current`` marks the newest version of each
zone (the head of its history, what an update builds on), not what applies today.

The rule is SQL shared by every reader: the panels (``api.services.panel_sql``), the panel cache
stamp, the publish job's heatmap cells and the admin listing. Statements bind the
municipality id and ``:tz`` (the profile's time zone). A zone holds a handful of versions: the
lookup is index-backed by ``ix_financial_assumptions_history`` (municipality, zone, version).
"""

from __future__ import annotations

from typing import Literal

AssumptionStatus = Literal["live", "scheduled", "superseded", "retired"]

# The municipality's local date, read from the statement's clock (now()).
LOCAL_TODAY = "(now() AT TIME ZONE CAST(:tz AS text))::date"


def applies_from_sql(alias: str) -> str:
    """The day a version takes its place on the timeline: its effective date, never before the
    local day it was saved."""
    return (
        f"GREATEST({alias}.effective_from, "
        f"({alias}.created_at AT TIME ZONE CAST(:tz AS text))::date)"
    )


def timeline_order_sql(alias: str) -> str:
    """How one zone's versions rank on a day: the latest ``applies_from``, then the newest."""
    return f"{applies_from_sql(alias)} DESC, {alias}.version DESC, {alias}.id DESC"


def top_versions_sql(*, municipality: str = ":m", alias: str = "t") -> str:
    """The version on top of each zone's timeline today (``zone_id`` null = the
    municipality-wide row), retired ones included: a retired top version means the zone has no
    figures. Select from it and filter ``retired_at IS NULL`` for the live versions."""
    return f"""
    SELECT DISTINCT ON ({alias}.zone_id) {alias}.*
    FROM financial_assumptions {alias}
    WHERE {alias}.municipality_id = {municipality}
      AND {applies_from_sql(alias)} <= {LOCAL_TODAY}
    ORDER BY {alias}.zone_id, {timeline_order_sql(alias)}"""


def live_versions_sql(*, municipality: str = ":m") -> str:
    """One row per zone with market figures today: the live version (all columns)."""
    return f"SELECT live.* FROM ({top_versions_sql(municipality=municipality)}) live " + (
        "WHERE live.retired_at IS NULL"
    )


def status_sql(alias: str, top_alias: str) -> str:
    """A version's status today, given a join of the zone's top version as ``top_alias``."""
    return f"""CASE
        WHEN {alias}.retired_at IS NOT NULL THEN 'retired'
        WHEN {applies_from_sql(alias)} > {LOCAL_TODAY} THEN 'scheduled'
        WHEN {top_alias}.id = {alias}.id THEN 'live'
        ELSE 'superseded'
    END"""
