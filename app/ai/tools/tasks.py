"""The `create_tasks` tool declaration.

Assignees are member references, not addresses or arbitrary user ids -- an
assignment is a side effect that names a person, so it goes through the same
resolver as recipients and guests (SPEC-v2 D21). The body is unreachable; see
app/ai/tools/base.py.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.ai.tools.base import refuse_direct_execution


def build_tasks_tool() -> BaseTool:
    @tool("create_tasks")
    async def create_tasks(tasks: list[dict[str, Any]]) -> str:
        """Create one or more tasks in this workspace.

        Args:
            tasks: A list of objects, each with a "title", an optional
                "description", and an optional "assignee" holding the NAME of a
                workspace member. Not an address or an id -- the server
                resolves the name and refuses anyone who is not a member here.
        """
        refuse_direct_execution("create_tasks")

    return create_tasks
