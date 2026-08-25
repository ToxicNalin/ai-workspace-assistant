"""Agent construction. SPEC-v2 §5, with the interrupt policy that is the point
of the whole step.

The middleware config below is the security boundary in one object: every tool
that touches the world outside this process interrupts for a human, and the
only tool that does not is the read-only one. That is deliberately a whitelist
of the safe thing rather than a blacklist of the dangerous ones -- a tool added
later without a matching entry here interrupts by default rather than
executing silently.
"""

import uuid

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.prompt import AGENT_SYSTEM_PROMPT
from app.ai.tools.calendar import build_calendar_tool
from app.ai.tools.email import build_email_tool
from app.ai.tools.search import build_search_tool
from app.ai.tools.tasks import build_tasks_tool
from app.constants import AUTO_APPROVED_TOOL

INTERRUPT_POLICY: dict[str, bool | dict[str, list[str]]] = {
    "send_email": {"allowed_decisions": ["approve", "edit", "reject"]},
    "create_event": {"allowed_decisions": ["approve", "edit", "reject"]},
    # No edit: a task list is created wholesale, and letting a reviewer rewrite
    # an arbitrary batch in the approval dialogue is more surface than value.
    "create_tasks": {"allowed_decisions": ["approve", "reject"]},
    AUTO_APPROVED_TOOL: False,
}


def build_agent(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
) -> CompiledStateGraph:  # type: ignore[type-arg]
    """Compile an agent bound to one workspace.

    The graph is built per request rather than once at startup because the
    search tool closes over this session and this workspace id. That costs a
    graph compile per turn and buys a property worth more than the cycles:
    the workspace the agent can read is fixed by the caller, not carried in
    state where a tool argument or a crafted document could reach it.
    """
    return create_agent(
        model=model,
        tools=[
            build_search_tool(db, workspace_id),
            build_email_tool(),
            build_calendar_tool(),
            build_tasks_tool(),
        ],
        system_prompt=AGENT_SYSTEM_PROMPT,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on=INTERRUPT_POLICY,  # type: ignore[arg-type]
                description_prefix="This action needs your approval",
            )
        ],
        checkpointer=checkpointer,
    )
