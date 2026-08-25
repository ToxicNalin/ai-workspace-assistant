"""Step 5: hybrid retrieval.

Two properties matter here. The first is that hybrid actually earns its
keep -- if it did not beat vector-only on the queries vectors are bad at,
running two retrievers would be pure cost. The second is that retrieval, which
is the first thing in this project to read across many documents at once,
never reaches out of the workspace it was asked about.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings.embedder import FakeEmbedder
from app.ai.retriever import hybrid, keyword, vector
from app.ai.retriever.base import RetrievedChunk
from app.ai.retriever.hybrid import reciprocal_rank_fusion
from app.tests.factories import (
    make_indexed_document,
    make_user,
    make_workspace,
    random_email,
)

# --------------------------------------------------------------------------
# Fusion, as pure logic.
# --------------------------------------------------------------------------


def _chunk(name: str, score: float = 0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_OID, name),
        document_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        document_name=f"{name}.txt",
        text=name,
        page_no=1,
        score=score,
    )


def test_fusion_prefers_a_result_both_retrievers_rank_well() -> None:
    """The whole reason for RRF: consistently good in both beats first in one."""
    both = _chunk("both")
    vector_only = _chunk("vector-only")
    keyword_only = _chunk("keyword-only")

    fused = reciprocal_rank_fusion(
        [[vector_only, both], [keyword_only, both]], limit=3
    )

    assert fused[0].chunk_id == both.chunk_id


def test_fusion_never_returns_the_same_chunk_twice() -> None:
    shared = _chunk("shared")

    fused = reciprocal_rank_fusion([[shared], [shared]], limit=10)

    assert len(fused) == 1


def test_fusion_ignores_the_incoming_score_scales() -> None:
    """A cosine similarity and a ts_rank cannot be compared, so fusion must
    read position only. A huge raw score in one list must not win on its own."""
    inflated = _chunk("inflated", score=999.0)
    modest = _chunk("modest", score=0.01)

    fused = reciprocal_rank_fusion([[inflated], [modest, inflated]], limit=2)

    # `modest` is ranked first in the second list; `inflated` is first in the
    # first list and second in the other, so it still wins -- on positions.
    assert fused[0].chunk_id == inflated.chunk_id
    assert fused[0].score < 1.0


def test_fusion_of_nothing_is_nothing() -> None:
    assert reciprocal_rank_fusion([[], []], limit=5) == []


# --------------------------------------------------------------------------
# Against the real indexes.
# --------------------------------------------------------------------------


async def test_keyword_search_finds_an_exact_term(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session,
        workspace=workspace,
        uploaded_by=user,
        name="runbook.txt",
        texts=[
            "The deployment pipeline builds the container and pushes it.",
            "If the service returns error code E4021 the certificate has expired.",
            "Rotating credentials requires an administrator approval.",
        ],
    )

    hits = await keyword.search(db_session, workspace_id=workspace.id, query="E4021")

    assert len(hits) == 1
    assert "E4021" in hits[0].text


async def test_hybrid_beats_vector_only_on_an_exact_term(db_session: AsyncSession) -> None:
    """SPEC-v2 §5's justification for making hybrid the default, asserted.

    The query is a bare identifier. Its embedding is unrelated to the chunk
    that contains it -- the fake embedder hashes text, so an unseen string
    lands nowhere near anything, exactly like a real model faced with an
    opaque error code. Lexical search is what finds it, and fusion is what
    carries it to the top.
    """
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session,
        workspace=workspace,
        uploaded_by=user,
        name="runbook.txt",
        texts=[
            "The deployment pipeline builds the container image and pushes it.",
            "If the service returns error code E4021 the certificate has expired.",
            "Rotating credentials requires an administrator approval first.",
            "Logs are retained for thirty days in the observability bucket.",
        ],
    )
    query = "E4021"
    query_embedding = await FakeEmbedder().embed_query(query)

    vector_hits = await vector.search(
        db_session, workspace_id=workspace.id, query_embedding=query_embedding
    )
    hybrid_hits = await hybrid.search(
        db_session,
        workspace_id=workspace.id,
        query=query,
        query_embedding=query_embedding,
    )

    def top_text(hits: list[RetrievedChunk]) -> str:
        return hits[0].text if hits else ""

    assert "E4021" in top_text(hybrid_hits), "hybrid failed to surface the exact term"
    assert "E4021" not in top_text(vector_hits), (
        "vector-only happened to rank it first, so this test proves nothing"
    )


async def test_vector_search_finds_semantically_identical_text(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    target = "Rotating credentials requires an administrator approval."
    await make_indexed_document(
        db_session,
        workspace=workspace,
        uploaded_by=user,
        name="policy.txt",
        texts=["Something entirely unrelated about catering.", target],
    )

    query_embedding = await FakeEmbedder().embed_query(target)
    hits = await vector.search(
        db_session, workspace_id=workspace.id, query_embedding=query_embedding
    )

    assert hits
    assert hits[0].text == target
    # Cosine distance flipped to similarity: an exact match is ~1.0.
    assert hits[0].score > 0.99


async def test_retrieval_carries_what_a_citation_needs(db_session: AsyncSession) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    document = await make_indexed_document(
        db_session,
        workspace=workspace,
        uploaded_by=user,
        name="handbook.txt",
        texts=["Expenses are reimbursed within thirty days of submission."],
    )

    hits = await keyword.search(db_session, workspace_id=workspace.id, query="expenses")

    assert len(hits) == 1
    assert hits[0].document_id == document.id
    assert hits[0].document_name == "handbook.txt"
    assert hits[0].page_no == 1


# --------------------------------------------------------------------------
# The workspace boundary.
# --------------------------------------------------------------------------


async def test_no_retriever_crosses_a_workspace_boundary(db_session: AsyncSession) -> None:
    """The first feature in the project that reads across many documents at
    once, and so the first that could leak a whole corpus rather than one row.
    Every retriever is checked, not just the default."""
    owner_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=owner_a)
    owner_b = await make_user(db_session, email=random_email())
    workspace_b = await make_workspace(db_session, owner=owner_b)

    secret = "The acquisition of Northwind closes on the fourteenth of March."
    await make_indexed_document(
        db_session,
        workspace=workspace_b,
        uploaded_by=owner_b,
        name="confidential.txt",
        texts=[secret],
    )
    await make_indexed_document(
        db_session,
        workspace=workspace_a,
        uploaded_by=owner_a,
        name="ours.txt",
        texts=["Our own unremarkable meeting notes."],
    )

    # Searched from A, using B's exact text as the query -- the strongest
    # possible pull towards the other tenant's data.
    query_embedding = await FakeEmbedder().embed_query(secret)

    vector_hits = await vector.search(
        db_session, workspace_id=workspace_a.id, query_embedding=query_embedding
    )
    keyword_hits = await keyword.search(
        db_session, workspace_id=workspace_a.id, query="Northwind acquisition"
    )
    hybrid_hits = await hybrid.search(
        db_session,
        workspace_id=workspace_a.id,
        query=secret,
        query_embedding=query_embedding,
    )

    for hits in (vector_hits, keyword_hits, hybrid_hits):
        assert all("Northwind" not in hit.text for hit in hits)
        assert all(hit.document_name != "confidential.txt" for hit in hits)

    # And the same query from B does find it, so the assertions above are
    # about scoping rather than about the text being unfindable.
    from_b = await keyword.search(
        db_session, workspace_id=workspace_b.id, query="Northwind acquisition"
    )
    assert len(from_b) == 1


# --------------------------------------------------------------------------
# The evaluation set itself.
# --------------------------------------------------------------------------


def test_the_eval_dataset_parses_and_is_internally_consistent() -> None:
    """Cheap guard on app/ai/eval/dataset.yaml.

    The eval runs as a non-blocking CI job, so a malformed dataset or a
    question pointing at a document that no longer exists would otherwise fail
    quietly in a log nobody reads.
    """
    from app.ai.eval.run_eval import load_dataset

    corpus, questions = load_dataset()

    assert len(questions) >= 20, "SPEC-v2 §8 asks for 20-30 questions"
    assert len(questions) <= 30
    assert corpus

    for question in questions:
        assert question.expect_document in corpus
        # The expected passage must actually be somewhere in the document it
        # is attributed to, or the question can never be answered correctly.
        assert any(
            question.expect_text.lower() in chunk.lower()
            for chunk in corpus[question.expect_document]
        ), f"{question.question!r} expects text not present in {question.expect_document}"
