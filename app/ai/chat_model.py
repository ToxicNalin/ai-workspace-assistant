import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from app.ai.upstream import THE_ASSISTANT, provider_errors
from app.constants import CHARS_PER_TOKEN_ESTIMATE

_EXCERPT = re.compile(
    r'<excerpt id="(?P<index>\d+)" document="(?P<document>[^"]*)"[^>]*>\n(?P<body>.*?)\n</excerpt>',
    re.DOTALL,
)


def estimate_tokens(text: str) -> int:
    """A rough token count, for when the provider will not give a real one.

    Roughly four characters to a token across English prose and the tokenisers
    in common use. Only ever a fallback -- see Usage.estimated. An estimate
    that is off by a fifth still bounds a bill; no number at all does not, and
    silently recording zero would make the daily budget unreachable and the
    whole limit decorative.
    """
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    # Whether these came from the provider or from estimate_tokens above.
    # Carried rather than discarded because "we think this cost 900 tokens"
    # and "this cost 900 tokens" are different claims. It is stored on the
    # usage row and reported by /admin/usage as `tokens_estimated`, so the
    # page can show a total without implying a precision it does not have.
    estimated: bool = False

    @classmethod
    def estimate(cls, *, prompt: str, completion: str) -> "Usage":
        return cls(
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(completion),
            estimated=True,
        )


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage


@dataclass(frozen=True)
class StreamChunk:
    """One event from a streaming call.

    Text arrives in deltas with no usage; the final chunk carries the usage
    and no text. Modelling it this way rather than returning bare strings is
    what lets the budget be charged for a streamed answer at all -- the token
    count is only knowable once the stream has finished.
    """

    text: str = ""
    usage: Usage | None = None


class ChatModel(Protocol):
    """The interface the LLM is mocked at (CLAUDE.md, Testing).

    Deliberately narrow: a system message and a user message in, text out.
    Everything about how retrieved content is framed lives in
    app/ai/prompts/, not here.
    """

    @property
    def model_name(self) -> str: ...

    async def complete(self, *, system: str, user: str) -> Completion: ...

    def stream(self, *, system: str, user: str) -> AsyncIterator[StreamChunk]: ...


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

    def _answer(self, user: str) -> str:
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

    async def complete(self, *, system: str, user: str) -> Completion:
        answer = self._answer(user)
        return Completion(
            text=answer, usage=Usage.estimate(prompt=system + user, completion=answer)
        )

    async def stream(self, *, system: str, user: str) -> AsyncIterator[StreamChunk]:
        answer = self._answer(user)

        # Word by word, so a test can prove the endpoint really emits several
        # events rather than one buffered blob wearing a stream's clothing.
        for index, word in enumerate(answer.split(" ")):
            yield StreamChunk(text=word if index == 0 else f" {word}")

        yield StreamChunk(usage=Usage.estimate(prompt=system + user, completion=answer))


def _text_of(message: Any) -> str:
    """Pull plain text out of a LangChain message or chunk.

    `content` is a string for most providers and a list of content blocks for
    some, and which one arrives is not worth making every caller care about.

    The string check has to come before the callable one, for the reason
    app/ai/agent/state.py sets out at length: `.text` is currently a
    `TextAccessor`, a str subclass that is *also* callable for back-compat with
    the older `.text()` method. Testing `callable` first therefore takes the
    deprecated path on every real provider response -- it works today, emits a
    LangChainDeprecationWarning on every call, and breaks outright when that
    back-compat is removed.
    """
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return str(text)
    if callable(text):
        return str(text())

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)

    return str(content)


def _usage_of(message: Any) -> Usage | None:
    metadata = getattr(message, "usage_metadata", None)
    if not metadata:
        return None
    return Usage(
        tokens_in=int(metadata.get("input_tokens", 0) or 0),
        tokens_out=int(metadata.get("output_tokens", 0) or 0),
    )


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

    def _messages(self, system: str, user: str) -> list[Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        # The retrieved content is inside `user` and only inside `user`. It is
        # never concatenated into the system message -- see app/ai/prompts/rag.py.
        return [SystemMessage(content=system), HumanMessage(content=user)]

    async def complete(self, *, system: str, user: str) -> Completion:
        # SPEC-v2 §7: the free tier's own rate limit is the error a public demo
        # is most likely to hit, and it must not arrive as a 500.
        with provider_errors(THE_ASSISTANT):
            response = await self._model.ainvoke(self._messages(system, user))
        text = _text_of(response)
        return Completion(
            text=text,
            usage=_usage_of(response) or Usage.estimate(prompt=system + user, completion=text),
        )

    async def stream(self, *, system: str, user: str) -> AsyncIterator[StreamChunk]:
        collected: list[str] = []
        usage: Usage | None = None

        # Wrapping the whole loop, not just its first step: a provider can fail
        # part-way through a stream. Nothing here catches CancelledError or
        # GeneratorExit, so a client that walks away is not logged as an outage.
        with provider_errors(THE_ASSISTANT):
            async for chunk in self._model.astream(self._messages(system, user)):
                # Usage can arrive on any chunk depending on the provider, and
                # for several it arrives only on the last one. Keep whatever
                # turns up and settle the total after the stream closes.
                usage = _usage_of(chunk) or usage
                text = _text_of(chunk)
                if text:
                    collected.append(text)
                    yield StreamChunk(text=text)

        answer = "".join(collected)
        yield StreamChunk(
            usage=usage or Usage.estimate(prompt=system + user, completion=answer)
        )
