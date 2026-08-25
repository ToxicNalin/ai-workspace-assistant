import uuid
from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.retriever.base import RetrievedChunk
from app.constants import HNSW_EF_SEARCH, RETRIEVAL_CANDIDATES
from app.database.models.chunk import DocumentChunk
from app.database.models.document import Document

_SET_EF_SEARCH = text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")


async def search(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query_embedding: Sequence[float],
    limit: int = RETRIEVAL_CANDIDATES,
) -> list[RetrievedChunk]:
    """Cosine similarity over the HNSW index, scoped to one workspace.

    The workspace filter is not an optimisation. document_chunks carries
    workspace_id precisely so this query can never reach across a tenant
    boundary, and every caller goes through the scoping dependency to get it.
    """
    # SET LOCAL, so it lasts exactly as long as the surrounding transaction and
    # cannot leak into whatever the pooled connection serves next. The useful
    # range is 40-200; much higher and the planner may abandon the index for a
    # sequential scan (SPEC-v2 §5).
    #
    # Interpolated rather than bound because SET LOCAL takes no parameters --
    # HNSW_EF_SEARCH is a module constant int, never anything a caller supplies.
    await db.execute(_SET_EF_SEARCH)

    distance = DocumentChunk.embedding.cosine_distance(query_embedding).label("distance")
    result = await db.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            Document.name,
            DocumentChunk.text,
            DocumentChunk.page_no,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.workspace_id == workspace_id)
        .order_by(distance)
        .limit(limit)
    )

    return [
        RetrievedChunk(
            chunk_id=row.id,
            document_id=row.document_id,
            document_name=row.name,
            text=row.text,
            page_no=row.page_no,
            # Cosine distance runs 0 (identical) to 2 (opposite); flipped here
            # so every retriever in this package reports "higher is better".
            score=1.0 - float(row.distance),
        )
        for row in result
    ]
