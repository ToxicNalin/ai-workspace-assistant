import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.auth.permissions import require_member
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.calendar import CalendarEventCreate, CalendarEventOut
from app.services import calendar_service

router = APIRouter(prefix="/workspaces/{workspace_id}/events", tags=["calendar"])


@router.get("", response_model=list[CalendarEventOut])
async def list_events(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> list[CalendarEventOut]:
    events = await calendar_service.list_events(db, context.workspace_id)
    return [CalendarEventOut.model_validate(event) for event in events]


@router.post("", response_model=CalendarEventOut, status_code=status.HTTP_201_CREATED)
async def create_event(
    body: CalendarEventCreate,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> CalendarEventOut:
    event = await calendar_service.create_event_for_references(
        db,
        workspace_id=context.workspace_id,
        created_by=context.user.id,
        title=body.title,
        description=body.description,
        start_time=body.start_time,
        end_time=body.end_time,
        guest_references=body.guests,
    )
    return CalendarEventOut.model_validate(event)


@router.get("/{event_id}", response_model=CalendarEventOut)
async def get_event(
    event_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> CalendarEventOut:
    event = await calendar_service.get_event(db, context.workspace_id, event_id)
    return CalendarEventOut.model_validate(event)


@router.get(
    "/{event_id}/ics",
    response_class=Response,
    responses={200: {"content": {"text/calendar": {}}}},
)
async def download_ics(
    event_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> Response:
    """The invitation, as a file any calendar client can open.

    Rebuilt from the stored event rather than cached, but from the *stored*
    guest snapshot -- so this is the same invitation that was sent, not one
    reflecting whoever happens to be a member today.
    """
    event, invitation = await calendar_service.ics_for(db, context.workspace_id, event_id)

    return Response(
        content=invitation,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="event-{event.id}.ics"',
        },
    )
