from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends

from app.constants import WorkspaceRole
from app.dependencies import WorkspaceContext, get_workspace_context
from app.exceptions import Forbidden

_ROLE_RANK: dict[WorkspaceRole, int] = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
}


def require_role(minimum: WorkspaceRole) -> Callable[..., Coroutine[Any, Any, WorkspaceContext]]:
    async def dependency(
        context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
    ) -> WorkspaceContext:
        if _ROLE_RANK[context.role] < _ROLE_RANK[minimum]:
            raise Forbidden("This action requires a higher role in this workspace")
        return context

    return dependency


require_viewer = require_role(WorkspaceRole.VIEWER)
require_member = require_role(WorkspaceRole.MEMBER)
require_admin = require_role(WorkspaceRole.ADMIN)
