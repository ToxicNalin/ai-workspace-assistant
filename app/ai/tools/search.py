"""The workspace document search tool -- the only one the agent may run
unattended (SPEC-v2 §5: read-only, auto-approve)."""

import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts.rag import build_user_message
from app.ai.provider import get_embedder
from app.ai.retriever import hybrid


def build_search_tool(db: AsyncSession, workspace_id: uuid.UUID) -> BaseTool:
    """Bind the retriever to one workspace, at construction time.

    `workspace_id` is closed over rather than being a tool argument, so it is
    not a thing the model can pass, get wrong, or be talked into changing by a
    document. There is no argument the agent could supply that would widen what
    this tool can see.
    """

    @tool("search_documents")
    async def search_documents(query: str) -> str:
        """Search this workspace's uploaded documents for passages relevant to a
        query. Returns excerpts to quote from. Use this before answering any
        question about the team's documents."""
        embedding = await get_embedder().embed_query(query)
        chunks = await hybrid.search(
            db, workspace_id=workspace_id, query=query, query_embedding=embedding
        )
        # Rendered through the same delimiting used by plain RAG chat, so
        # retrieved text arrives at the model wrapped and labelled untrusted
        # here too -- a tool result is no more trustworthy than a chunk.
        return build_user_message(question=query, chunks=chunks)

    return search_documents


ToolFactory = Callable[[AsyncSession, uuid.UUID], BaseTool]


def tool_names(tools: list[Any]) -> list[str]:
    return [getattr(item, "name", "") for item in tools]
