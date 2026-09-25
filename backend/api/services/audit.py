"""One writer for ``audit_log`` (migration 0006): every staff action, whichever service did it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import Principal
from core.context import get_request_id

AUDIT_SQL = text(
    """
    INSERT INTO audit_log (municipality_id, actor, actor_user_id, action, entity_type, entity_id,
                           details, before, after, note, request_id)
    VALUES (:m, :actor, :actor_user_id, :action, :entity_type, :entity_id,
            CAST(:details AS jsonb), CAST(:before AS jsonb), CAST(:after AS jsonb), :note,
            :request_id)
    """
)


async def write_audit(
    session: AsyncSession,
    *,
    municipality_id: str,
    principal: Principal | None = None,
    action: str,
    actor: str | None = None,
    entity_type: str | None,
    entity_id: int | None,
    details: Mapping[str, Any] | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """Append one row inside the caller's transaction (the caller commits). ``before`` / ``after``
    are entity-specific summaries of the state around the action; the table is append-only.
    Actions without a staff principal (a guest placing an order) name their ``actor``."""
    if principal is None and not actor:
        raise ValueError("write_audit needs a principal or an actor")
    await session.execute(
        AUDIT_SQL,
        {
            "m": municipality_id,
            "actor": principal.subject if principal is not None else actor,
            "actor_user_id": principal.user_id if principal is not None else None,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": json.dumps(dict(details or {}), default=str),
            "before": json.dumps(dict(before), default=str) if before is not None else None,
            "after": json.dumps(dict(after), default=str) if after is not None else None,
            "note": note,
            "request_id": get_request_id(),
        },
    )
