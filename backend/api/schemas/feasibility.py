"""Schemas for ``POST /v1/feasibility``: server-side recalculation from edited assumptions.

The response reuses the panel's feasibility and assumptions blocks (``api/schemas/panel.py``) so
the numbers a visitor gets here are, field for field, the ones the panel shows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.panel import AssumptionsBlock, CalculationBasis, FeasibilityBlock


class EditedAssumptions(BaseModel):
    """The visitor's edits. Omitted / null = keep the default (0.70 share, market rates)."""

    model_config = ConfigDict(extra="forbid")

    construction_cost_per_m2: float | None = Field(
        default=None, gt=0, le=100_000, description="Construction cost per m² of GFA, EUR"
    )
    selling_price_per_m2: float | None = Field(
        default=None, gt=0, le=100_000, description="Selling price per m² of saleable area, EUR"
    )
    saleable_share: float | None = Field(
        default=None, gt=0, le=1, description="Share of the GFA that is saleable (0 < share ≤ 1)"
    )


class FeasibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parcel_id: int = Field(
        gt=0,
        description="urban_parcels.id for type 'urban'; cadastral_parcels.id (the Parcel ID) for "
        "type 'cadastral'",
    )
    type: Literal["urban", "cadastral"]
    assumptions: EditedAssumptions = Field(default_factory=EditedAssumptions)


class PlanningInputsUsed(BaseModel):
    """The planning parameters the formulas ran on (from the serving tables)."""

    calculation_basis: CalculationBasis
    plot_area_m2: float = Field(description="Planned urban parcel area, or the cadastral fallback")
    far: float | None = Field(default=None, description="Max FAR (II); null = not stated in plan")
    site_coverage_pct: float | None = Field(
        default=None, description="Max site coverage (IZ) in %; null = not stated in plan"
    )
    max_gfa_m2: float | None = Field(default=None, description="FAR × plot area, if computable")
    max_coverage_area_m2: float | None = Field(
        default=None, description="Coverage % × plot area, if computable"
    )


class EngineInfo(BaseModel):
    name: Literal["@urbanview/feasibility-engine"] = "@urbanview/feasibility-engine"
    engine_version: str = Field(description="Version of the shared engine implementation")
    formula_version: str = Field(description="Version of the client's formula set (poc-1)")
    range_derivation: str
    deterministic: Literal[True] = Field(
        default=True, description="Pure formula engine: no AI, no randomness, no I/O"
    )


class Disclaimer(BaseModel):
    en: str
    me: str
    status: Literal["placeholder", "client_approved"]
    version: str


class FeasibilityResponse(BaseModel):
    type: Literal["urban", "cadastral"]
    parcel_id: int
    municipality_id: str
    covered: bool = Field(
        description="False when no adopted document governs the parcel: nothing can be calculated"
    )
    coverage_note_en: str | None = None
    coverage_note_me: str | None = None
    calculation_basis: CalculationBasis
    basis_area_m2: float
    planning_inputs: PlanningInputsUsed | None = None
    feasibility: FeasibilityBlock | None = Field(
        default=None,
        description="The 7 Group 2 figures (saleable area explicit) and the 4 cost rows, each as "
        "low / expected / high; null when the parcel is not covered",
    )
    assumptions: AssumptionsBlock | None = Field(
        default=None, description="The assumptions actually used, with overrides and sources"
    )
    engine: EngineInfo
    data_version: str
    data_version_date: str | None = None
    client_validated: Literal[False] = False
    disclaimer: Disclaimer
