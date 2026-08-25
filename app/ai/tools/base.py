"""Shared vocabulary for the agent's tools: how an action is refused, and the
guard that proves no side-effecting tool body ever runs.

The second half is worth reading before changing anything in this package.

Every tool the agent can call that touches the world outside this process is
listed in `INTERRUPT_POLICY` (app/ai/agent/graph.py) and pauses for a human.
When the human approves, the side effect is carried out by
app/services/action_executor.py, working from the payload the approval hash
covers -- *not* by resuming the graph and letting the tool run. So the bodies
of `send_email`, `create_event` and `create_tasks` are unreachable by
construction, and `refuse_direct_execution` turns that from an assumption into
a loud failure if it ever stops being true.

Which it could. `HumanInTheLoopMiddleware` interrupts on the tools named in
`interrupt_on` and silently lets everything else through -- a side-effecting
tool added later without an entry there would execute unattended, with no
approval and no audit trail. `assert_every_tool_has_a_policy` refuses to build
such an agent at all.
"""

from typing import NoReturn

from langchain_core.tools import BaseTool


class ActionRefused(Exception):
    """The server will not offer this action to a human at all.

    Deliberately not an AppError: this never becomes an HTTP status. It aborts
    one proposed action inside the agent flow, which is then recorded as a
    refusal and reported back to the model.

    Refusing outright rather than showing the human a warning is the point. An
    approval dialogue is a place where plausible-looking things get clicked
    through; anything the server already knows is wrong should not reach it.
    """

    def __init__(self, reference: str, message: str) -> None:
        self.reference = reference
        super().__init__(message)


class InvalidActionArguments(ActionRefused):
    """The model produced arguments the server cannot make sense of.

    Caught at proposal time rather than at execution time on purpose: an
    unparseable timestamp discovered after approval means a human has already
    authorised something that was never going to work.
    """


class ApprovalGateBypassed(RuntimeError):
    """A side-effecting tool body was executed. This should be impossible.

    Raised rather than logged. If the approval gate has been routed around,
    the safe outcome is a failed request -- not an email that quietly goes out
    with nobody's decision behind it.
    """


def refuse_direct_execution(tool_name: str) -> NoReturn:
    raise ApprovalGateBypassed(
        f"The '{tool_name}' tool was executed directly. Side-effecting tools in this "
        f"application are never run by the agent: they pause for human approval, and "
        f"the approved payload is then carried out by app/services/action_executor.py. "
        f"Reaching this line means the approval gate was bypassed."
    )


def assert_every_tool_has_a_policy(
    tools: list[BaseTool], policy: dict[str, bool | dict[str, list[str]]]
) -> None:
    """Every tool must be explicitly listed as interrupting or as auto-approved.

    The middleware's default for an unlisted tool is to run it, so silence here
    is the dangerous answer rather than the safe one. Making omission a startup
    failure means the decision has to be written down.
    """
    missing = sorted({tool.name for tool in tools} - set(policy))
    if missing:
        raise ApprovalGateBypassed(
            f"These tools have no entry in INTERRUPT_POLICY: {', '.join(missing)}. "
            f"An unlisted tool is not interrupted -- it runs unattended, with no "
            f"approval and no audit trail. Add each one explicitly, as interrupting "
            f"or as auto-approved."
        )
