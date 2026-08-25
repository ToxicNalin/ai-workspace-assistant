import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.ai.provider import get_chat_model, get_embedder
from app.auth.permissions import require_member
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
