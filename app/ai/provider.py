from functools import lru_cache
from typing import TYPE_CHECKING

from app.ai.chat_model import ChatModel
from app.ai.embeddings.embedder import Embedder
from app.config import get_settings

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


@lru_cache
def get_embedder() -> Embedder:
    """The only place a model provider is named, so swapping one is this file
    (SPEC-v2 D18)."""
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


@lru_cache
def get_chat_model() -> ChatModel:
    settings = get_settings()

    if settings.llm_provider == "gemini":
        from app.ai.chat_model import GeminiChatModel

        return GeminiChatModel(model=settings.llm_model, api_key=settings.google_api_key)

    from app.ai.chat_model import FakeChatModel

    return FakeChatModel()


@lru_cache
def get_agent_model() -> "BaseChatModel":
    """The tool-calling model the LangGraph agent runs on.

    Separate from get_chat_model() because create_agent needs a real
    BaseChatModel that can emit tool calls, which the narrow ChatModel protocol
    used by plain RAG chat deliberately cannot.
    """
    settings = get_settings()

    if settings.llm_provider == "gemini":
        from langchain.chat_models import init_chat_model

        return init_chat_model(settings.llm_model, api_key=settings.google_api_key)

    from app.ai.agent.fake_model import KeywordAgentModel

    return KeywordAgentModel()
