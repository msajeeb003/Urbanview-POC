"""Schemas of the calculation engine's proposals (``/v1/admin/engine/proposals``): formulas and
data inputs staff propose. A proposal is recorded and audited for the client's review; it never
changes the deterministic engine (a new ``FORMULA_VERSION`` with validated fixtures does)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProposalKind = Literal["formula", "data_input"]
ProposalStatus = Literal["new", "pending", "accepted", "declined"]


class EngineProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProposalKind
    name: str = Field(
        min_length=1, max_length=200, description="Output name (formula) or dataset name"
    )
    expression: str | None = Field(
        default=None, max_length=500, description="Formula: e.g. GFA ÷ 60 (required)"
    )
    source: str | None = Field(
        default=None, max_length=200, description="Formula: where its inputs come from"
    )
    provides: str | None = Field(
        default=None, max_length=500, description="Data input: what it provides (required)"
    )
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "expression", "source", "provides", "note")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _complete(self) -> EngineProposalIn:
        if not self.name:
            raise ValueError("give a name")
        if self.kind == "formula" and not self.expression:
            raise ValueError("a formula needs an expression")
        if self.kind == "data_input" and not self.provides:
            raise ValueError("a data input needs what it provides")
        return self


class EngineProposalOut(BaseModel):
    id: int
    kind: ProposalKind
    name: str
    expression: str | None = None
    source: str | None = None
    provides: str | None = None
    status: ProposalStatus = Field(
        description="new (a formula) / pending (a data input) until the client decides"
    )
    note: str | None = None
    created_by: str
    created_at: datetime


class EngineProposalList(BaseModel):
    items: list[EngineProposalOut]
