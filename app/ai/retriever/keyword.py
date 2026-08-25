import uuid

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.retriever.base import RetrievedChunk
from app.constants import RETRIEVAL_CANDIDATES
from app.database.models.chunk import DocumentChunk
from app.database.models.document import Document


async def search(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    limit: int = RETRIEVAL_CANDIDATES,
) -> list[RetrievedChunk]:
    """Postgres full-text search over the generated tsvector column.

    This is the half of hybrid retrieval that vectors are bad at: exact terms.
    An embedding of "error code E4021" sits near every other error-handling
    passage in the corpus, while a lexical match finds the one line that
    actually contains it.
    """
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank(DocumentChunk.tsv, tsquery).label("rank")

    result = await db.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            Document.name,
            DocumentChunk.text,
            DocumentChunk.page_no,
            rank,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.tsv.op("@@")(tsquery),
        )
        .order_by(desc(rank))
        .limit(limit)
    )

    return [
        RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            document_name=row.name,
            text=row.text,
            page_no=row.page_no,
            score=float(row.rank),
        )
        for row in result
    ]
