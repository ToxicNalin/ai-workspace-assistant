"""The agent's state and the shapes the interrupt speaks in.

`create_agent` supplies its own message-carrying state, so there is no custom
graph state to declare. What does need naming is the structure LangGraph hands
back when the human-in-the-loop middleware interrupts, because
app/services/agent_service.py reads it and turning an untyped nest of dicts
into typed records at the boundary is what keeps that service legible.
"""

from dataclasses import dataclass
from typing import Any

from app.constants import PendingActionType


@dataclass(frozen=True)
class ProposedAction:
    """One tool call the agent wants to make, paused awaiting a decision."""

    tool_name: str
    args: dict[str, Any]
    description: str

    @property
    def action_type(self) -> PendingActionType:
        return PendingActionType(self.tool_name)


def parse_interrupts(result: dict[str, Any]) -> list[ProposedAction]:
    """Pull the proposed actions out of an interrupted agent result.

    The middleware packs every paused tool call from one model turn into a
    single interrupt whose value carries an `action_requests` list. The order
    matters and is preserved: resuming requires exactly one decision per
    request, in the same order.
    """
    proposed: list[ProposedAction] = []

    for interrupt in result.get("__interrupt__") or []:
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue

        for request in value.get("action_requests") or []:
            proposed.append(
                ProposedAction(
                    tool_name=request.get("name", ""),
                    args=dict(request.get("args") or {}),
                    description=request.get("description", ""),
                )
            )

    return proposed


def reply_text(result: dict[str, Any]) -> str:
    """The assistant's last message, as text a person can read.

    `message.content` is not reliably a string. Gemini 3.x returns a list of
    content blocks, so `str(content)` yields a Python repr --
    `[{'type': 'text', 'text': 'Sent.', 'extras': {...}}]` -- which would then
    be persisted as a chat message and shown to the user verbatim.

    `.text` collapses blocks to their text for every provider that emits them,
    so it is the right accessor. It currently returns a `TextAccessor` -- a str
    subclass that is *also* callable, for back-compat with the older
    `.text()` method -- so the string check has to come first: calling it
    still works but is deprecated, and `str()` narrows it back to a plain
    string before it is persisted.

    The fallback covers a message object with neither. All of it lives here
    rather than duplicated across two services because getting it wrong is
    invisible until you point the app at a real provider.
    """
    for message in reversed(result.get("messages") or []):
        if getattr(message, "type", None) != "ai" or not message.content:
            continue

        text = getattr(message, "text", None)
        if isinstance(text, str):
            return str(text)
        if callable(text):
            return str(text())

        return str(message.content)

    return ""
