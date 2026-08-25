"""The `create_event` tool declaration. Guests are member references, not
addresses -- same reasoning as app/ai/tools/email.py."""

from langchain_core.tools import BaseTool, tool


def build_calendar_tool() -> BaseTool:
    @tool("create_event")
    async def create_event(
        title: str, start_time: str, end_time: str, guests: list[str]
    ) -> str:
        """Create a calendar event and invite workspace members to it.

        Args:
            title: The event title.
            start_time: ISO 8601 start timestamp.
            end_time: ISO 8601 end timestamp.
            guests: The NAMES of workspace members to invite. Not addresses --
                the server resolves each name itself, and refuses anyone who is
                not a member of this workspace.
        """
        return f"Event '{title}' created with {len(guests)} guest(s)."

    return create_event
