"""The `send_email` tool declaration.

Nothing here sends anything -- Step 7 wires a real provider. What matters at
this step is the *signature*: `recipients` are member references, never
addresses. See app/ai/tools/resolve.py for why that distinction is the whole
security model rather than a naming preference.
"""

from langchain_core.tools import BaseTool, tool


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
        # Reached only after a human approved it. By then the arguments have
        # been replaced with the server-resolved payload that human was shown.
        return f"Email sent to {', '.join(recipients)}."

    return send_email
