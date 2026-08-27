"""Usage and membership reporting, for whoever runs a workspace.

BUILD-ORDER names these `/admin/usage` and `/admin/users`, unqualified. They
are mounted under `/workspaces/{workspace_id}/` instead, and the reason is
architectural rather than cosmetic: there is no site-wide administrator in this
data model. `users` has no superuser flag and RBAC is defined per membership,
so a global `/admin/usage` would either be readable by anybody logged in, or
would need a privilege level invented here that nothing else in the system
knows about. Both are worse than the alternative, and an unscoped admin route
is precisely the shape of thing CLAUDE.md's first architecture rule exists to
forbid.

Scoped this way they obey every rule that already holds: admin-only within the
workspace, 404 rather than 403 across a workspace boundary, and a case each in
test_tenant_isolation.py.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.permissions import require_admin
from app.constants import ADMIN_USAGE_DEFAULT_DAYS
from app.dependencies import DbSession, WorkspaceContext
from app.schemas.admin import MemberUsageOut, UsageOut
from app.services import usage_service

router = APIRouter(prefix="/workspaces/{workspace_id}/admin", tags=["admin"])

# A window, not a full history. Reporting over all time on a 0.5 GB database
# would get slower every week to answer a question nobody asks.
DaysWindow = Annotated[int, Query(ge=1, le=90)]


@router.get("/usage", response_model=UsageOut)
async def usage(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_admin)],
    days: DaysWindow = ADMIN_USAGE_DEFAULT_DAYS,
) -> UsageOut:
    summary = await usage_service.summary(db, context.workspace_id, days=days)
    return UsageOut(
        days=days,
        calls=summary.calls,
        tokens_in=summary.tokens_in,
        tokens_out=summary.tokens_out,
        tokens_estimated=summary.tokens_estimated,
        daily_budget=summary.daily_budget,
        tokens_used_today=summary.tokens_used_today,
        tokens_remaining_today=summary.tokens_remaining_today,
        by_kind=summary.by_kind,
        by_day=summary.by_day,
    )


@router.get("/users", response_model=list[MemberUsageOut])
async def users(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_admin)],
    days: DaysWindow = ADMIN_USAGE_DEFAULT_DAYS,
) -> list[MemberUsageOut]:
    members = await usage_service.member_usage(db, context.workspace_id, days=days)
    return [
        MemberUsageOut(
            user_id=member.user_id,
            name=member.name,
            email=member.email,
            role=member.role,
            joined_at=member.joined_at,
            calls=member.calls,
            tokens_in=member.tokens_in,
            tokens_out=member.tokens_out,
        )
        for member in members
    ]
