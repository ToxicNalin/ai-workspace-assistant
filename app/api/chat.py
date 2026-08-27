import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.ai.provider import get_chat_model, get_embedder
from app.auth.permissions import require_member
from app.database.session import async_session_factory
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.chat import ChatRequest, ChatResponse, CitationOut, MessageOut, ThreadOut
from app.services import chat_service

router = APIRouter(prefix="/workspaces/{workspace_id}/chat", tags=["chat"])


@router.post("/query", response_model=ChatResponse)
async def query(
    body: ChatRequest,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> ChatResponse:
    thread, message, citations = await chat_service.answer_question(
        db,
        get_embedder(),
        get_chat_model(),
        workspace_id=context.workspace_id,
        user_id=context.user.id,
        question=body.question,
        thread_id=body.thread_id,
    )

    return ChatResponse(
        thread_id=thread.id,
        message=MessageOut(
            id=message.id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            citations=[CitationOut.model_validate(citation) for citation in citations],
        ),
    )


@router.get("/stream")
async def stream(
    request: Request,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
    question: Annotated[str, Query(min_length=1, max_length=4000)],
    thread_id: uuid.UUID | None = None,
) -> EventSourceResponse:
    """The same answer as /query, delivered token by token.

    GET with the question in the query string, because SSE is a GET-only
    protocol. The access token still travels in the Authorization header
    rather than the URL: SPEC-v2 D19 keeps it in a JS variable, so the client
    reads this with fetch rather than the browser's EventSource, which cannot
    set headers. A token in a query string ends up in server logs and browser
    history, which is the reason not to do the obvious thing here.

    The budget is checked before the response is opened, so exhausting it is
    an ordinary 429 with Retry-After. Once a 200 and the first byte of an
    event stream have gone out, there is no status code left to report with.
    """
    await chat_service.ensure_within_budget(db, context.workspace_id)

    workspace_id = context.workspace_id
    user_id = context.user.id
    # Resolved here, while the request scope is still alive, rather than
    # inside the generator below.
    session_factory: Any = getattr(
        request.app.state, "db_session_factory", async_session_factory
    )

    async def events() -> AsyncIterator[dict[str, str]]:
        # A session of its own, deliberately. The request-scoped session is
        # torn down when the handler returns, and the handler returns as soon
        # as this response object is constructed -- the generator runs
        # afterwards, while the body is being sent.
        async with session_factory() as session:
            async for event in chat_service.stream_answer(
                session,
                get_embedder(),
                get_chat_model(),
                workspace_id=workspace_id,
                user_id=user_id,
                question=question,
                thread_id=thread_id,
            ):
                yield {"event": event.event, "data": json.dumps(event.data)}

    return EventSourceResponse(events())


@router.get("/threads", response_model=list[ThreadOut])
async def list_threads(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> list[ThreadOut]:
    threads = await chat_service.list_threads(db, context.workspace_id)
    return [ThreadOut.model_validate(thread) for thread in threads]


@router.get("/threads/{thread_id}/history", response_model=list[MessageOut])
async def history(
    thread_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> list[MessageOut]:
    messages, citations = await chat_service.list_messages(
        db, context.workspace_id, thread_id
    )

    return [
        MessageOut(
            id=message.id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            citations=[
                CitationOut.model_validate(citation)
                for citation in citations.get(message.id, [])
            ],
        )
        for message in messages
    ]
