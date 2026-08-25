"""Append-only audit writer.

There is no update and no delete here, and that is the entire design. A log
that can be rewritten by the code it is watching records nothing worth reading.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import AuditAction
from app.database.models.audit_log import AuditLogEntry


def record(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action: AuditAction,
    user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Stage an entry. Does not commit -- the caller commits it in the same
    transaction as whatever it is recording, so the log and the fact it
    describes cannot disagree."""
    entry = AuditLogEntry(
        workspace_id=workspace_id,
        user_id=user_id,
        action=action.value,
        details=details or {},
    )
    db.add(entry)
    return entry


async def list_entries(
    db: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 100
) -> Sequence[AuditLogEntry]:
    result = await db.scalars(
        select(AuditLogEntry)
        .where(AuditLogEntry.workspace_id == workspace_id)
        .order_by(AuditLogEntry.created_at.desc())
        .limit(limit)
    )
    return result.all()
