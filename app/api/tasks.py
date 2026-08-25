import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.permissions import require_member
from app.constants import TaskStatus
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.task import TaskCreate, TaskOut, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    assigned_to: uuid.UUID | None = None,
) -> list[TaskOut]:
    tasks = await task_service.list_tasks(
        db, context.workspace_id, status=task_status, assigned_to=assigned_to
    )
    return [TaskOut.model_validate(task) for task in tasks]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> TaskOut:
    task = await task_service.create_task(
        db,
        workspace_id=context.workspace_id,
        created_by=context.user.id,
        title=body.title,
        description=body.description,
        assigned_to=body.assigned_to,
        due_date=body.due_date,
        status=body.status,
    )
    return TaskOut.model_validate(task)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> TaskOut:
    task = await task_service.get_task(db, context.workspace_id, task_id)
    return TaskOut.model_validate(task)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdate,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> TaskOut:
    # exclude_unset, not exclude_none: "assigned_to": null means unassign,
    # while omitting the key means leave the assignee alone.
    task = await task_service.update_task(
        db, context.workspace_id, task_id, body.model_dump(exclude_unset=True)
    )
    return TaskOut.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> None:
    await task_service.delete_task(db, context.workspace_id, task_id)
