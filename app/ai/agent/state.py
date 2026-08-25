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
