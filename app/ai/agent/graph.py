"""Agent construction. SPEC-v2 §5, with the interrupt policy that is the point
of the whole step.

The middleware config below is the security boundary in one object: every tool
that touches the world outside this process interrupts for a human, and the
only tool that does not is the read-only one.

The library does *not* fail safe here, which is worth stating plainly because
the shape of the config invites the opposite assumption.
`HumanInTheLoopMiddleware.after_model` interrupts on the tools it finds in
`interrupt_on` and lets every other tool call straight through -- so a
side-effecting tool added later and forgotten here would run unattended, with
no approval and no audit trail. `assert_every_tool_has_a_policy` is what
supplies the missing default: an agent whose tools are not all accounted for
below refuses to be built at all.
"""

import uuid

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.prompt import AGENT_SYSTEM_PROMPT
from app.ai.tools.base import assert_every_tool_has_a_policy
from app.ai.tools.calendar import build_calendar_tool
from app.ai.tools.email import build_email_tool
from app.ai.tools.search import build_search_tool
from app.ai.tools.tasks import build_tasks_tool
from app.constants import AUTO_APPROVED_TOOL

# "respond" appears in every side-effecting entry and is never offered to a
# human. It is the server's own channel: once an approved action has actually
# been carried out by app/services/action_executor.py, the graph is resumed
# with the real outcome as the tool's result, so the model reports what
# happened rather than what it hoped would happen -- and the tool body itself
# is never executed. The API only accepts approve, edit and reject from a
# client (see app/schemas/approval.py), so this widens nothing a caller can
# reach.
INTERRUPT_POLICY: dict[str, bool | dict[str, list[str]]] = {
    "send_email": {"allowed_decisions": ["approve", "edit", "reject", "respond"]},
    "create_event": {"allowed_decisions": ["approve", "edit", "reject", "respond"]},
    # No edit: a task list is created wholesale, and letting a reviewer rewrite
    # an arbitrary batch in the approval dialogue is more surface than value.
    "create_tasks": {"allowed_decisions": ["approve", "reject", "respond"]},
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
    tools = [
        build_search_tool(db, workspace_id),
        build_email_tool(),
        build_calendar_tool(),
        build_tasks_tool(),
    ]
    assert_every_tool_has_a_policy(tools, INTERRUPT_POLICY)

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=AGENT_SYSTEM_PROMPT,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on=INTERRUPT_POLICY,  # type: ignore[arg-type]
                description_prefix="This action needs your approval",
            )
        ],
        checkpointer=checkpointer,
    )
