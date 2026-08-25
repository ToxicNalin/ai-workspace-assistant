import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chat_model import ChatModel
from app.ai.embeddings.embedder import Embedder
from app.ai.prompts.rag import build_user_message
from app.ai.prompts.system import SYSTEM_PROMPT
from app.ai.retriever import hybrid
from app.ai.retriever.base import RetrievedChunk
from app.constants import CHAT_TITLE_MAX_CHARS, CITATION_QUOTE_CHARS, ChatRole
from app.database.models.chat import ChatMessage, ChatThread
from app.database.models.citation import MessageCitation
from app.exceptions import NotFound


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

    The ordering matters: retrieval is scoped to `workspace_id` before the
    model is ever involved, so there is no path by which the model's output
    can widen what it is allowed to see.
    """
    if thread_id is None:
        thread = ChatThread(
            workspace_id=workspace_id, user_id=user_id, title=_derive_title(question)
        )
        db.add(thread)
        await db.flush()
    else:
        thread = await get_thread(db, workspace_id, thread_id)

    db.add(
        ChatMessage(
            workspace_id=workspace_id,
            thread_id=thread.id,
            user_id=user_id,
            role=ChatRole.USER,
            content=question,
        )
    )

    query_embedding = await embedder.embed_query(question)
    chunks = await hybrid.search(
        db,
        workspace_id=workspace_id,
        query=question,
        query_embedding=query_embedding,
    )

    answer = await chat_model.complete(
        system=SYSTEM_PROMPT,
        # Retrieved text goes here, in the user turn, and nowhere else.
        user=build_user_message(question=question, chunks=chunks),
    )

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

    await db.commit()
    await db.refresh(thread)
    await db.refresh(message)
    for citation in citations:
        await db.refresh(citation)

    return thread, message, citations
