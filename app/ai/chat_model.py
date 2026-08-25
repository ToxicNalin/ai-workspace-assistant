import re
from typing import Protocol

_EXCERPT = re.compile(
    r'<excerpt id="(?P<index>\d+)" document="(?P<document>[^"]*)"[^>]*>\n(?P<body>.*?)\n</excerpt>',
    re.DOTALL,
)


class ChatModel(Protocol):
    """The interface the LLM is mocked at (CLAUDE.md, Testing).

    Deliberately narrow: a system message and a user message in, a string out.
    Everything about how retrieved content is framed lives in
    app/ai/prompts/, not here.
    """

    @property
    def model_name(self) -> str: ...

    async def complete(self, *, system: str, user: str) -> str: ...


class FakeChatModel:
    """Deterministic, offline, and grounded in what it was actually given.

    It does not imitate a language model -- it answers by quoting the excerpts
    in the user message. That makes it a useful test double rather than a
    stub: an answer can only mention a document if that document was really
    retrieved, so a test asserting the answer is grounded is asserting
    something real about the retrieval that fed it.
    """

    @property
    def model_name(self) -> str:
        return "fake-grounded"

    async def complete(self, *, system: str, user: str) -> str:
        excerpts = _EXCERPT.findall(user)
        if not excerpts:
            return "I could not find anything relevant in this workspace's documents."

        documents: list[str] = []
        for _, document, _body in excerpts:
            if document not in documents:
                documents.append(document)

        first_body = excerpts[0][2].strip().replace("\n", " ")
        sources = ", ".join(documents)
        return f"According to {sources}: {first_body}"


class GeminiChatModel:
    """Production chat model, constructed through langchain's provider-agnostic
    factory so swapping to OpenAI is one setting (SPEC-v2 D18)."""

    def __init__(self, *, model: str, api_key: str) -> None:
        # Imported lazily so a local or CI run, which uses FakeChatModel, does
        # not pay to import the provider stack at all.
        from langchain.chat_models import init_chat_model

        self._model_name = model
        self._model = init_chat_model(model, api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    async def complete(self, *, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        # The retrieved content is inside `user` and only inside `user`. It is
        # never concatenated into the system message -- see app/ai/prompts/rag.py.
        response = await self._model.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        return str(response.text()) if callable(response.text) else str(response.content)
