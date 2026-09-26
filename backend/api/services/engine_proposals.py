"""Formulas and data inputs proposed for the calculation engine (the admin console's
"+ Add formula" / "+ Add data input").

The engine's formulas are client-owned and deterministic (``packages/feasibility-engine``, its
Python copy ``core.engine.shared``, the shared fixtures): a proposal records the request for the
client's review and writes an ``audit_log`` row, and nothing reads it when figures are
calculated. A formula starts ``new``, a data input ``pending`` (the wireframe's labels).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.schemas.engine import EngineProposalIn, EngineProposalList, EngineProposalOut
from api.services.audit import write_audit
from core.auth import Principal

LIST_SQL = text(
    """
    SELECT id, kind, name, expression, source, provides, status, note, created_by, created_at
    FROM engine_proposals
    WHERE municipality_id = :m
    ORDER BY created_at ASC, id ASC
    """
)
INSERT_SQL = text(
    """
    INSERT INTO engine_proposals (municipality_id, kind, name, expression, source, provides,
                                  status, note, created_by, created_by_user_id)
    VALUES (:m, :kind, :name, :expression, :source, :provides, :status, :note, :created_by,
            :created_by_user_id)
    RETURNING id, kind, name, expression, source, provides, status, note, created_by, created_at
    """
)
INITIAL_STATUS = {"formula": "new", "data_input": "pending"}


def _out(row: Mapping[str, Any]) -> EngineProposalOut:
    return EngineProposalOut(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        expression=row["expression"],
        source=row["source"],
        provides=row["provides"],
        status=row["status"],
        note=row["note"],
        created_by=row["created_by"],
        created_at=row["created_at"].astimezone(UTC),
    )


class EngineProposalService:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, municipality_id: str
    ) -> None:
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def list_proposals(self) -> EngineProposalList:
        async with self.session_factory() as session:
            rows = (await session.execute(LIST_SQL, {"m": self.municipality_id})).mappings().all()
        return EngineProposalList(items=[_out(r) for r in rows])

    async def create_proposal(
        self, principal: Principal, payload: EngineProposalIn
    ) -> EngineProposalOut:
        values = {
            "kind": payload.kind,
            "name": payload.name,
            "expression": payload.expression if payload.kind == "formula" else None,
            "source": payload.source if payload.kind == "formula" else None,
            "provides": payload.provides if payload.kind == "data_input" else None,
            "status": INITIAL_STATUS[payload.kind],
            "note": payload.note,
        }
        async with self.session_factory() as session:
            row = (
                (
                    await session.execute(
                        INSERT_SQL,
                        {
                            "m": self.municipality_id,
                            "created_by": principal.subject,
                            "created_by_user_id": principal.user_id,
                            **values,
                        },
                    )
                )
                .mappings()
                .one()
            )
            await write_audit(
                session,
                municipality_id=self.municipality_id,
                principal=principal,
                action="engine.proposal",
                entity_type="engine_proposal",
                entity_id=int(row["id"]),
                details={"kind": payload.kind, "name": payload.name, "engine_changed": False},
                after=values,
                note=payload.note,
            )
            await session.commit()
        return _out(row)
