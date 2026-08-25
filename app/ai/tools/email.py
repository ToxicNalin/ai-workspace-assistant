"""The `send_email` tool declaration.

Nothing here sends anything, and after Step 7 that is truer than it was
before: this body is never executed at all. What the declaration is *for* is
the signature -- `recipients` are member references, never addresses. See
app/ai/tools/resolve.py for why that distinction is the whole security model
rather than a naming preference, and app/ai/tools/base.py for why the body
raises instead of doing the work.
"""

from langchain_core.tools import BaseTool, tool

from app.ai.tools.base import refuse_direct_execution


def build_email_tool() -> BaseTool:
    @tool("send_email")
    async def send_email(recipients: list[str], subject: str, body: str) -> str:
        """Send an email to one or more members of this workspace.

        Args:
            recipients: The NAMES of workspace members to send to, for example
                ["Alice Smith"]. You must not supply email addresses; the
                server looks up each member's address itself. A name that is
                not a member of this workspace will be refused.
            subject: The subject line.
            body: The message body.
        """
        refuse_direct_execution("send_email")

    return send_email
