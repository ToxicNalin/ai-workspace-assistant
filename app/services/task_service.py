"""Tasks: ordinary CRUD, plus the bulk create the agent's approved action calls.

The one rule worth stating is SPEC-v2 D3. `tasks.assigned_to` points at
`users`, not at `workspace_members`, because membership rows are deleted and
recreated when somebody leaves and rejoins -- a task keyed to a membership
would either break or silently reattach to whoever inherited that row. The
membership check that a foreign key would have given us therefore has to
happen here, on every write, and it is the same check the agent's recipient
resolver makes: is this person a member of *this* workspace, right now.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MAX_TASKS_PER_ACTION, TaskStatus
from app.database.models.membership import WorkspaceMember
from app.database.models.task import Task
from app.exceptions import Conflict, NotFound


async def _assert_is_member(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """NotFound rather than Forbidden, deliberately.

    The id being checked came from the client. Answering "that user exists but
    is not in this workspace" would confirm the existence of a user outside the
    caller's tenant, which is the same leak the cross-tenant 404 rule exists to
    prevent -- just through a body field instead of a path parameter.
    """
    membership = await db.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if membership is None:
        raise NotFound("That user is not a member of this workspace")


async def list_tasks(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: TaskStatus | None = None,
    assigned_to: uuid.UUID | None = None,
) -> Sequence[Task]:
    statement = select(Task).where(Task.workspace_id == workspace_id)
    if status is not None:
        statement = statement.where(Task.status == status)
    if assigned_to is not None:
        statement = statement.where(Task.assigned_to == assigned_to)

    result = await db.scalars(statement.order_by(Task.created_at.desc()))
    return result.all()


async def get_task(db: AsyncSession, workspace_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = await db.scalar(
        select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
    )
    if task is None:
        raise NotFound
    return task


async def create_task(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID | None,
    title: str,
    description: str = "",
    assigned_to: uuid.UUID | None = None,
    due_date: datetime | None = None,
    status: TaskStatus = TaskStatus.TODO,
    source_message_id: uuid.UUID | None = None,
) -> Task:
    if assigned_to is not None:
        await _assert_is_member(db, workspace_id=workspace_id, user_id=assigned_to)

    task = Task(
        workspace_id=workspace_id,
        title=title,
        description=description,
        assigned_to=assigned_to,
        due_date=due_date,
        status=status,
        source_message_id=source_message_id,
        created_by=created_by,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def update_task(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    changes: dict[str, object],
) -> Task:
    """Apply only the fields the caller actually sent.

    `changes` comes from a Pydantic model dumped with exclude_unset, so
    "assigned_to": None means unassign, and an absent key means leave it
    alone. Those are different intentions and a plain optional field cannot
    tell them apart.
    """
    task = await get_task(db, workspace_id, task_id)

    assignee = changes.get("assigned_to")
    if isinstance(assignee, uuid.UUID):
        await _assert_is_member(db, workspace_id=workspace_id, user_id=assignee)

    for field, value in changes.items():
        setattr(task, field, value)

    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, workspace_id: uuid.UUID, task_id: uuid.UUID) -> None:
    task = await get_task(db, workspace_id, task_id)
    await db.delete(task)
    await db.commit()


async def create_many(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID | None,
    specs: Sequence[dict[str, object]],
    source_message_id: uuid.UUID | None = None,
) -> list[Task]:
    """The bulk create behind an approved `create_tasks` action.

    Does not commit: the caller is app/services/action_executor.py, which owns
    the transaction so that the tasks and the audit entry recording them land
    together. Each spec's assignee has already been resolved to a real member
    by app/services/agent_service.py before a human ever saw it -- the check
    below is the second one, not the first.
    """
    if not specs:
        raise Conflict("There are no tasks to create")
    if len(specs) > MAX_TASKS_PER_ACTION:
        raise Conflict(f"No more than {MAX_TASKS_PER_ACTION} tasks can be created at once")

    tasks: list[Task] = []
    for spec in specs:
        assigned_to = spec.get("assigned_to")
        if isinstance(assigned_to, uuid.UUID):
            await _assert_is_member(db, workspace_id=workspace_id, user_id=assigned_to)
        else:
            assigned_to = None

        task = Task(
            workspace_id=workspace_id,
            title=str(spec.get("title", "")),
            description=str(spec.get("description", "")),
            assigned_to=assigned_to,
            source_message_id=source_message_id,
            created_by=created_by,
        )
        db.add(task)
        tasks.append(task)

    await db.flush()
    return tasks
