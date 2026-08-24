import asyncio
import hashlib
import logging
import math
import random
from collections.abc import Sequence
from typing import Protocol

from app.constants import EMBEDDING_BATCH_SIZE, EMBEDDING_DIMENSIONS, MAX_EMBEDDING_ATTEMPTS

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """The interface LLM providers are mocked at (CLAUDE.md, Testing).

    Nothing outside app/ai/provider.py names a concrete implementation.
    """

    @property
    def model_name(self) -> str:
        """Recorded on documents.embedding_model so a future re-index knows
        what produced the existing vectors."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


def _batched(texts: Sequence[str], size: int) -> list[Sequence[str]]:
    return [texts[start : start + size] for start in range(0, len(texts), size)]


class FakeEmbedder:
    """Deterministic, offline, no API key, no network.

    Not a random stub: each vector is seeded from a hash of the text, so the
    same text always embeds identically and identical chunks come out exactly
    similar. That is enough to exercise chunk storage, the HNSW index and the
    whole ingestion path in CI and on a fresh clone.
    """

    @property
    def model_name(self) -> str:
        return f"fake-deterministic-{EMBEDDING_DIMENSIONS}"

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]
        # Unit-normalised, like a real embedding model's output -- cosine
        # distance over the HNSW index behaves the same way as a result.
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]


class GeminiEmbedder:
    """Production embedder. See app/ai/provider.py for how it is selected."""

    def __init__(self, *, model: str, api_key: str, dimensions: int) -> None:
        # Imported lazily: a local or CI run uses FakeEmbedder and should not
        # pay to import the Google client stack at all.
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from pydantic import SecretStr

        self._model = model
        self._client = GoogleGenerativeAIEmbeddings(
            model=model,
            # The field is declared google_api_key but aliased to api_key;
            # populate_by_name means both work, the alias is what is typed.
            api_key=SecretStr(api_key),
            output_dimensionality=dimensions,
            # Corpus text and search queries are embedded asymmetrically by
            # this model family; the query side arrives with the retriever in
            # Step 5. Getting this wrong costs recall silently.
            task_type="RETRIEVAL_DOCUMENT",
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batched(texts, EMBEDDING_BATCH_SIZE):
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        delay = 1.0
        for attempt in range(1, MAX_EMBEDDING_ATTEMPTS + 1):
            try:
                return await self._client.aembed_documents(list(batch))
            except Exception as exc:
                if attempt == MAX_EMBEDDING_ATTEMPTS or not _is_retryable(exc):
                    raise
                logger.warning(
                    "embedding batch rate limited, backing off",
                    extra={"attempt": attempt, "delay_seconds": delay},
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")


def _is_retryable(exc: Exception) -> bool:
    """Gemini's free tier rate limits aggressively. Matching on the message is
    crude, but the alternative is importing google.api_core just to catch
    ResourceExhausted -- and the SDK does not surface it consistently anyway.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        marker in text
        for marker in ("429", "resource", "rate limit", "quota", "unavailable", "503", "timeout")
    )
