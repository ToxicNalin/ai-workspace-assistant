import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chat_model import ChatModel, Usage, estimate_tokens
from app.ai.embeddings.embedder import Embedder
from app.ai.prompts.rag import build_user_message
from app.ai.prompts.system import SYSTEM_PROMPT
from app.ai.retriever import hybrid
from app.ai.retriever.base import RetrievedChunk
from app.constants import (
    CHAT_TITLE_MAX_CHARS,
    CITATION_QUOTE_CHARS,
    ChatRole,
    UsageKind,
)
from app.database.models.chat import ChatMessage, ChatThread
from app.database.models.citation import MessageCitation
from app.exceptions import NotFound
from app.services import usage_service


def _derive_title(question: str) -> str:
    """First line of the opening question, trimmed. Enough for a usable
    sidebar without making the user name anything."""
    title = " ".join(question.split())
    if len(title) <= CHAT_TITLE_MAX_CHARS:
        return title
    return title[: CHAT_TITLE_MAX_CHARS - 1].rstrip() + "…"


async def list_threads(
    db: AsyncSession, workspace_id: uuid.UUID
) -> Sequence[ChatThread]:
    result = await db.scalars(
        select(ChatThread)
        .where(ChatThread.workspace_id == workspace_id)
        .order_by(ChatThread.created_at.desc())
    )
    return result.all()


async def get_thread(
    db: AsyncSession, workspace_id: uuid.UUID, thread_id: uuid.UUID
) -> ChatThread:
    thread = await db.scalar(
        select(ChatThread).where(
            ChatThread.id == thread_id, ChatThread.workspace_id == workspace_id
        )
    )
    if thread is None:
        raise NotFound
    return thread


async def list_messages(
    db: AsyncSession, workspace_id: uuid.UUID, thread_id: uuid.UUID
) -> tuple[Sequence[ChatMessage], dict[uuid.UUID, list[MessageCitation]]]:
    """Messages oldest first, with their citations grouped by message id."""
    await get_thread(db, workspace_id, thread_id)

    messages = (
        await db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread_id,
                ChatMessage.workspace_id == workspace_id,
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).all()

    citations = (
        await db.scalars(
            select(MessageCitation)
            .where(MessageCitation.message_id.in_([message.id for message in messages]))
            .order_by(MessageCitation.score.desc())
        )
    ).all()

    grouped: dict[uuid.UUID, list[MessageCitation]] = {}
    for citation in citations:
        grouped.setdefault(citation.message_id, []).append(citation)

    return messages, grouped


def _to_citation(
    *, workspace_id: uuid.UUID, message_id: uuid.UUID, chunk: RetrievedChunk
) -> MessageCitation:
    return MessageCitation(
        workspace_id=workspace_id,
        message_id=message_id,
        chunk_id=chunk.chunk_id,
        # Denormalised on purpose. If the document is deleted later, chunk_id
        # goes NULL and these three columns are all that is left of the
        # evidence this answer was built on (SPEC-v2 D5).
        document_name=chunk.document_name,
        quoted_text=chunk.text[:CITATION_QUOTE_CHARS],
        page_no=chunk.page_no,
        score=chunk.score,
    )


async def ensure_within_budget(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Refuse before anything is spent.

    Exposed here so a route can check the budget without importing the usage
    service to do it, and so the check happens before an SSE response has been
    opened -- once the stream is running the only way to report this would be
    an error event inside a 200, which no HTTP client retries correctly.
    """
    await usage_service.enforce_budget(db, workspace_id)


async def _open_thread(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    thread_id: uuid.UUID | None,
) -> ChatThread:
    if thread_id is None:
        thread = ChatThread(
            workspace_id=workspace_id, user_id=user_id, title=_derive_title(question)
        )
        db.add(thread)
        await db.flush()
        return thread

    return await get_thread(db, workspace_id, thread_id)


async def _retrieve(
    db: AsyncSession,
    embedder: Embedder,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
) -> list[RetrievedChunk]:
    """Embed the question, search this workspace, and bill the embedding.

    The embedding call is small but it is not free, and leaving it out of the
    ledger would mean the daily budget quietly under-counts every question
    ever asked. The provider returns vectors rather than token counts, so this
    one is necessarily an estimate.
    """
    query_embedding = await embedder.embed_query(question)
    usage_service.record(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        kind=UsageKind.EMBEDDING,
        model=embedder.model_name,
        tokens_in=estimate_tokens(question),
        tokens_out=0,
        estimated=True,
    )

    return list(
        await hybrid.search(
            db,
            workspace_id=workspace_id,
            query=question,
            query_embedding=query_embedding,
        )
    )


async def _persist_answer(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    thread: ChatThread,
    answer: str,
    chunks: Sequence[RetrievedChunk],
) -> tuple[ChatMessage, list[MessageCitation]]:
    message = ChatMessage(
        workspace_id=workspace_id,
        thread_id=thread.id,
        user_id=None,
        role=ChatRole.ASSISTANT,
        content=answer,
    )
    db.add(message)
    await db.flush()

    citations = [
        _to_citation(workspace_id=workspace_id, message_id=message.id, chunk=chunk)
        for chunk in chunks
    ]
    db.add_all(citations)
    return message, citations


async def answer_question(
    db: AsyncSession,
    embedder: Embedder,
    chat_model: ChatModel,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    thread_id: uuid.UUID | None = None,
) -> tuple[ChatThread, ChatMessage, list[MessageCitation]]:
    """Resolve thread, retrieve, prompt, answer, persist.

    The ordering matters twice over. Retrieval is scoped to `workspace_id`
    before the model is ever involved, so there is no path by which the
    model's output can widen what it is allowed to see -- and the budget is
    checked before any of it, so a workspace that has spent its allowance
    costs this deployment one aggregate query rather than an embedding call
    and a completion.
    """
    await ensure_within_budget(db, workspace_id)

    thread = await _open_thread(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        question=question,
        thread_id=thread_id,
    )

    db.add(
        ChatMessage(
            workspace_id=workspace_id,
            thread_id=thread.id,
            user_id=user_id,
            role=ChatRole.USER,
            content=question,
        )
    )

    chunks = await _retrieve(
        db, embedder, workspace_id=workspace_id, user_id=user_id, question=question
    )

    completion = await chat_model.complete(
        system=SYSTEM_PROMPT,
        # Retrieved text goes here, in the user turn, and nowhere else.
        user=build_user_message(question=question, chunks=chunks),
    )
    answer = completion.text

    usage_service.record(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        kind=UsageKind.CHAT,
        model=chat_model.model_name,
        tokens_in=completion.usage.tokens_in,
        tokens_out=completion.usage.tokens_out,
        estimated=completion.usage.estimated,
    )

    message, citations = await _persist_answer(
        db, workspace_id=workspace_id, thread=thread, answer=answer, chunks=chunks
    )

    await db.commit()
    await db.refresh(thread)
    await db.refresh(message)
    for citation in citations:
        await db.refresh(citation)

    return thread, message, citations


@dataclass
class StreamEvent:
    """One server-sent event, before it is encoded.

    The service decides what happens and in what order; app/api/chat.py
    decides how that becomes SSE on the wire. Keeping the split means the
    ordering guarantee below is testable without an HTTP client.
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)


async def stream_answer(
    db: AsyncSession,
    embedder: Embedder,
    chat_model: ChatModel,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    thread_id: uuid.UUID | None = None,
) -> AsyncIterator[StreamEvent]:
    """The same conversation as answer_question, delivered as it is produced.

    The event order is a contract the frontend depends on:

      meta       once, first -- carries the thread id, so a client that just
                 started a new conversation can attach to it before the answer
                 has finished arriving
      token      zero or more, in order
      citations  once, after the last token -- they cannot be sent earlier
                 without claiming an answer cites something it had not said yet
      done       once, last, carrying the persisted message id

    Nothing is written until the model has finished. A half-streamed answer
    that the client abandoned is not an answer, and persisting one would put
    truncated text in the thread history and bill the workspace for it.
    """
    await ensure_within_budget(db, workspace_id)

    thread = await _open_thread(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        question=question,
        thread_id=thread_id,
    )
    db.add(
        ChatMessage(
            workspace_id=workspace_id,
            thread_id=thread.id,
            user_id=user_id,
            role=ChatRole.USER,
            content=question,
        )
    )

    yield StreamEvent(event="meta", data={"thread_id": str(thread.id)})

    chunks = await _retrieve(
        db, embedder, workspace_id=workspace_id, user_id=user_id, question=question
    )

    collected: list[str] = []
    usage = Usage(tokens_in=0, tokens_out=0, estimated=True)

    async for piece in chat_model.stream(
        system=SYSTEM_PROMPT,
        user=build_user_message(question=question, chunks=chunks),
    ):
        if piece.usage is not None:
            usage = piece.usage
        if piece.text:
            collected.append(piece.text)
            yield StreamEvent(event="token", data={"text": piece.text})

    answer = "".join(collected)

    usage_service.record(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        kind=UsageKind.CHAT,
        model=chat_model.model_name,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        estimated=usage.estimated,
    )

    message, citations = await _persist_answer(
        db, workspace_id=workspace_id, thread=thread, answer=answer, chunks=chunks
    )
    await db.commit()

    yield StreamEvent(
        event="citations",
        data={
            "citations": [
                {
                    "document_name": citation.document_name,
                    "quoted_text": citation.quoted_text,
                    "page_no": citation.page_no,
                    "score": citation.score,
                }
                for citation in citations
            ]
        },
    )
    yield StreamEvent(
        event="done",
        data={"thread_id": str(thread.id), "message_id": str(message.id)},
    )
