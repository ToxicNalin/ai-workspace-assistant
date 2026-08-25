"""The `create_event` tool declaration. Guests are member references, not
addresses -- same reasoning as app/ai/tools/email.py, and the body is
unreachable for the same reason."""

from langchain_core.tools import BaseTool, tool

from app.ai.tools.base import refuse_direct_execution


def build_calendar_tool() -> BaseTool:
    @tool("create_event")
    async def create_event(
        title: str, start_time: str, end_time: str, guests: list[str]
    ) -> str:
        """Create a calendar event and invite workspace members to it.

        Args:
            title: The event title.
            start_time: ISO 8601 start timestamp, for example
                2026-09-01T10:00:00+00:00.
            end_time: ISO 8601 end timestamp. Must be after start_time.
            guests: The NAMES of workspace members to invite. Not addresses --
                the server resolves each name itself, and refuses anyone who is
                not a member of this workspace.
        """
        refuse_direct_execution("create_event")

    return create_event
