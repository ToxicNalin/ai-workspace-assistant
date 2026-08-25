from pydantic import BaseModel, Field


class EmailProviderStatus(BaseModel):
    """What `/email/status` reports.

    `configured` is the useful field: the Resend provider selects fine without
    an API key and then fails at the moment somebody approves an action, which
    is the worst possible time to find out.
    """

    provider: str
    configured: bool
    from_address: str


class ManualEmailRequest(BaseModel):
    """A person composing an email, which still goes through the approval gate.

    `recipients` are member references -- names or addresses -- and are
    resolved server-side against `workspace_members` exactly as the agent's are
    (SPEC-v2 D21). An address that is not a member of this workspace is refused
    here too, so this endpoint cannot be used as a way around the resolver.
    """

    recipients: list[str] = Field(min_length=1, max_length=50)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=20_000)
