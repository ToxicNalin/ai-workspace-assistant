import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.retriever import keyword, vector
from app.ai.retriever.base import RetrievedChunk
from app.constants import RETRIEVAL_CANDIDATES, RETRIEVAL_TOP_K, RRF_K


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]], *, limit: int, k: int = RRF_K
) -> list[RetrievedChunk]:
    """Fuse several ranked lists by position rather than by score.

    The two retrievers produce scores on scales that have nothing to do with
    each other -- a cosine similarity and a ts_rank cannot be added, averaged
    or compared, and normalising them means inventing a conversion nobody can
    justify. RRF only reads the *position* of a result in each list, so the
    scales never have to be reconciled. A chunk ranked solidly by both beats
    one ranked first by a single retriever, which is exactly the behaviour
    hybrid search is for.
    """
    fused: dict[uuid.UUID, float] = {}
    seen: dict[uuid.UUID, RetrievedChunk] = {}

    for ranking in rankings:
        for position, chunk in enumerate(ranking, start=1):
            fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + 1.0 / (k + position)
            seen.setdefault(chunk.chunk_id, chunk)

    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)

    return [
        RetrievedChunk(
            chunk_id=seen[chunk_id].chunk_id,
            document_id=seen[chunk_id].document_id,
            document_name=seen[chunk_id].document_name,
            text=seen[chunk_id].text,
            page_no=seen[chunk_id].page_no,
            score=score,
        )
        for chunk_id, score in ordered[:limit]
    ]


async def search(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    query_embedding: Sequence[float],
    limit: int = RETRIEVAL_TOP_K,
    candidates: int = RETRIEVAL_CANDIDATES,
) -> list[RetrievedChunk]:
    """The default retriever (SPEC-v2 §5).

    v1 listed hybrid search as optional. It is the default here because
    Postgres gives full-text search away for free alongside the vector index,
    and vector-only retrieval is visibly bad at exact terms -- names, IDs,
    error codes.
    """
    # Deliberately sequential rather than gathered: both halves run on the same
    # AsyncSession, and a session is not safe to use from two tasks at once.
    vector_hits = await vector.search(
        db, workspace_id=workspace_id, query_embedding=query_embedding, limit=candidates
    )
    keyword_hits = await keyword.search(
        db, workspace_id=workspace_id, query=query, limit=candidates
    )

    return reciprocal_rank_fusion([vector_hits, keyword_hits], limit=limit)
