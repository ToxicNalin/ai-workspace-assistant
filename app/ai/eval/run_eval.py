"""Measure retrieval quality against app/ai/eval/dataset.yaml.

Run it directly:

    python -m app.ai.eval.run_eval

It builds a throwaway workspace, indexes the corpus from the dataset, runs
every question through all three retrievers, and prints hit-rate per retriever
so the three can be compared rather than asserted about in the abstract.
Everything it created is deleted before it exits.

CI runs this as a non-blocking job: a drop in the number is information, not a
reason to fail a build, since retrieval quality moves for legitimate reasons.
"""

import argparse
import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import get_embedder
from app.ai.retriever import hybrid, keyword, vector
from app.ai.retriever.base import RetrievedChunk
from app.constants import RETRIEVAL_TOP_K, DocumentStatus
from app.database.models.chunk import DocumentChunk
from app.database.models.document import Document
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.session import async_session_factory

DATASET_PATH = Path(__file__).with_name("dataset.yaml")


@dataclass(frozen=True)
class Question:
    question: str
    expect_document: str
    expect_text: str


@dataclass
class Score:
    """Two numbers, because they answer different questions.

    `document_hits` is whether the right file surfaced at all. `text_hits` is
    the stricter one: whether the passage that actually answers the question
    surfaced. A retriever can score well on the first and badly on the second
    by returning the right document's least relevant paragraph.
    """

    name: str
    total: int = 0
    document_hits: int = 0
    text_hits: int = 0
    reciprocal_rank_total: float = 0.0

    def record(self, question: Question, hits: Sequence[RetrievedChunk]) -> None:
        self.total += 1

        if any(hit.document_name == question.expect_document for hit in hits):
            self.document_hits += 1

        needle = question.expect_text.lower()
        for rank, hit in enumerate(hits, start=1):
            if needle in hit.text.lower():
                self.text_hits += 1
                self.reciprocal_rank_total += 1.0 / rank
                break

    def _rate(self, value: int) -> float:
        return value / self.total if self.total else 0.0

    def render(self) -> str:
        mrr = self.reciprocal_rank_total / self.total if self.total else 0.0
        return (
            f"  {self.name:<10} "
            f"document hit-rate {self._rate(self.document_hits):>6.1%}   "
            f"passage hit-rate {self._rate(self.text_hits):>6.1%}   "
            f"MRR {mrr:>5.3f}"
        )


def load_dataset(path: Path = DATASET_PATH) -> tuple[dict[str, list[str]], list[Question]]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    corpus = {document["name"]: list(document["chunks"]) for document in raw["corpus"]}
    questions = [
        Question(
            question=item["question"],
            expect_document=item["expect_document"],
            expect_text=item["expect_text"],
        )
        for item in raw["questions"]
    ]

    unknown = {q.expect_document for q in questions} - corpus.keys()
    if unknown:
        raise ValueError(f"questions reference documents not in the corpus: {sorted(unknown)}")

    return corpus, questions


async def _index_corpus(
    db: AsyncSession, *, workspace: Workspace, user: User, corpus: dict[str, list[str]]
) -> None:
    embedder = get_embedder()

    for name, texts in corpus.items():
        document = Document(
            workspace_id=workspace.id,
            name=name,
            storage_key=f"{workspace.id}/{uuid.uuid4()}-{name}",
            content_hash=uuid.uuid4().hex,
            mime_type="text/markdown",
            size_bytes=sum(len(text) for text in texts),
            uploaded_by=user.id,
            status=DocumentStatus.READY,
            chunk_count=len(texts),
            embedding_model=embedder.model_name,
        )
        db.add(document)
        await db.flush()

        vectors = await embedder.embed_documents(texts)
        db.add_all(
            [
                DocumentChunk(
                    workspace_id=workspace.id,
                    document_id=document.id,
                    text=text,
                    page_no=index + 1,
                    chunk_index=index,
                    embedding=embedding,
                )
                for index, (text, embedding) in enumerate(zip(texts, vectors, strict=True))
            ]
        )

    await db.commit()


async def evaluate(top_k: int = RETRIEVAL_TOP_K) -> dict[str, Score]:
    corpus, questions = load_dataset()
    embedder = get_embedder()

    workspace_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    scores = {
        name: Score(name) for name in ("vector", "keyword", "hybrid")
    }

    try:
        async with async_session_factory() as db:
            user = User(
                email=f"eval-{uuid.uuid4().hex}@example.invalid",
                password_hash="not-a-real-account",
                name="Evaluation Harness",
            )
            db.add(user)
            await db.flush()
            workspace = Workspace(name="Evaluation", owner_id=user.id)
            db.add(workspace)
            await db.flush()
            workspace_id, user_id = workspace.id, user.id

            await _index_corpus(db, workspace=workspace, user=user, corpus=corpus)

            for question in questions:
                query_embedding = await embedder.embed_query(question.question)

                scores["vector"].record(
                    question,
                    await vector.search(
                        db,
                        workspace_id=workspace.id,
                        query_embedding=query_embedding,
                        limit=top_k,
                    ),
                )
                scores["keyword"].record(
                    question,
                    await keyword.search(
                        db, workspace_id=workspace.id, query=question.question, limit=top_k
                    ),
                )
                scores["hybrid"].record(
                    question,
                    await hybrid.search(
                        db,
                        workspace_id=workspace.id,
                        query=question.question,
                        query_embedding=query_embedding,
                        limit=top_k,
                    ),
                )
    finally:
        if workspace_id is not None:
            async with async_session_factory() as db:
                # Deleting the workspace cascades documents and chunks.
                await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()

    return scores


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=RETRIEVAL_TOP_K)
    args = parser.parse_args()

    _, questions = load_dataset()
    scores = await evaluate(top_k=args.top_k)

    print(f"\nRetrieval evaluation - {len(questions)} questions, top-{args.top_k}\n")
    for score in scores.values():
        print(score.render())

    embedder = get_embedder()
    print(f"\n  embedder: {embedder.model_name}")
    if embedder.model_name.startswith("fake"):
        print(
            "  NOTE: the deterministic offline embedder hashes text, so it has no\n"
            "  semantic behaviour at all. The vector numbers above are a floor, not\n"
            "  a measurement. Set EMBEDDING_PROVIDER=gemini for a real one."
        )
    print()


if __name__ == "__main__":
    asyncio.run(main())
