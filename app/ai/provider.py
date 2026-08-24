from functools import lru_cache

from app.ai.embeddings.embedder import Embedder
from app.config import get_settings


@lru_cache
def get_embedder() -> Embedder:
    """The only place a model provider is named, so swapping one is this file
    (SPEC-v2 D18). `get_chat_model()` joins it in Step 5, with its first
    caller — there is nothing to invoke a chat model from yet.
    """
    settings = get_settings()

    if settings.embedding_provider == "gemini":
        from app.ai.embeddings.embedder import GeminiEmbedder
        from app.constants import EMBEDDING_DIMENSIONS

        return GeminiEmbedder(
            model=settings.embedding_model,
            api_key=settings.google_api_key,
            dimensions=EMBEDDING_DIMENSIONS,
        )

    from app.ai.embeddings.embedder import FakeEmbedder

    return FakeEmbedder()
