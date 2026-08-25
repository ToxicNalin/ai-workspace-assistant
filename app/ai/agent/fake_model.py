"""Offline chat models for the agent.

`create_agent` needs a real `BaseChatModel` that can emit tool calls, which the
narrow `ChatModel` protocol in app/ai/chat_model.py deliberately cannot. These
two fill that gap without an API key.

`ScriptedAgentModel` is the one that matters. Tests use it to say exactly what
the model decides, including deciding something malicious -- which is the only
honest way to test a defence that exists because the model is downstream of
untrusted text. A test that hopes a real model resists injection is measuring
the model; a test that assumes the model is already compromised and asserts the
server still refuses is measuring the thing we actually built.
"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class _ToolBindingModel(BaseChatModel):
    """Accepts bind_tools and ignores it: these models decide by rule, not by
    reading a tool schema."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self


class ScriptedAgentModel(_ToolBindingModel):
    """Returns a fixed sequence of messages, one per turn, repeating the last."""

    responses: list[AIMessage] = []
    turn: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-agent"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.responses:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="No script."))])

        index = min(self.turn, len(self.responses) - 1)
        self.turn += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


_EMAIL_INTENT = re.compile(r"\b(email|e-mail|message|write to|send)\b", re.IGNORECASE)
_TASK_INTENT = re.compile(r"\b(task|todo|to-do|action item)\b", re.IGNORECASE)
_EVENT_INTENT = re.compile(r"\b(event|meeting|calendar|schedule)\b", re.IGNORECASE)
# "email Alice about X", "invite Bob Smith" -- capitalised runs after the verb.
_NAME = re.compile(
    r"\b(?:to|email|invite|assign(?:ed)? to|for)\s+([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)"
)


class KeywordAgentModel(_ToolBindingModel):
    """Deterministic rule-based agent behaviour, so a fresh clone can drive the
    whole approval flow with no API key.

    It is not pretending to be a language model. It routes on keywords, and
    once a tool has answered it stops. Set LLM_PROVIDER=gemini for real
    behaviour.
    """

    @property
    def _llm_type(self) -> str:
        return "keyword-agent"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # A tool has already answered, so this turn is the final reply. Without
        # this the agent would propose the same action for ever.
        if messages and isinstance(messages[-1], ToolMessage):
            content = str(messages[-1].content)
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=content[:2000]))]
            )

        request = ""
        for message in reversed(messages):
            if message.type == "human":
                request = str(message.content)
                break

        names = _NAME.findall(request) or ["the team"]

        if _EMAIL_INTENT.search(request):
            call = {
                "name": "send_email",
                "args": {
                    "recipients": names,
                    "subject": "Update",
                    "body": request,
                },
                "id": "call_email",
            }
        elif _TASK_INTENT.search(request):
            call = {
                "name": "create_tasks",
                "args": {"tasks": [{"title": request[:200], "assignee": names[0]}]},
                "id": "call_tasks",
            }
        elif _EVENT_INTENT.search(request):
            call = {
                "name": "create_event",
                "args": {
                    "title": request[:200],
                    "start_time": "2026-09-01T10:00:00+00:00",
                    "end_time": "2026-09-01T11:00:00+00:00",
                    "guests": names,
                },
                "id": "call_event",
            }
        else:
            call = {
                "name": "search_documents",
                "args": {"query": request},
                "id": "call_search",
            }

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))]
        )
