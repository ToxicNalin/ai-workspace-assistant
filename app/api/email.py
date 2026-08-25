from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.auth.permissions import require_member
from app.config import get_settings
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.approval import PendingActionOut
from app.schemas.email import EmailProviderStatus, ManualEmailRequest
from app.services import approval_service, email_service

router = APIRouter(prefix="/workspaces/{workspace_id}/email", tags=["email"])


@router.get("/status", response_model=EmailProviderStatus)
async def provider_status(
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> EmailProviderStatus:
    """Whether mail can actually be delivered from this deployment.

    Worth an endpoint because the failure it reports is otherwise discovered at
    the worst possible moment: the Resend provider selects perfectly well
    without an API key and only fails when somebody approves an action.
    """
    settings = get_settings()
    return EmailProviderStatus(
        provider=settings.email_provider,
        configured=email_service.provider_is_configured(),
        from_address=settings.email_from_address,
    )


@router.post(
    "/send", response_model=PendingActionOut, status_code=status.HTTP_202_ACCEPTED
)
async def send_email(
    body: ManualEmailRequest,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> PendingActionOut:
    """Propose an email. It is not sent here.

    202 rather than 201: what comes back is a pending action, and nothing
    leaves the process until an admin approves it at
    `/pending-actions/{id}/decide`. A person composing their own message is not
    the threat the gate was built for, but routing every outbound message
    through it means there is exactly one place where mail leaves this
    application, one audit trail, and no second path for a recipient to be
    supplied by anything other than the server-side resolver.
    """
    action = await approval_service.propose_email(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user.id,
        recipients=body.recipients,
        subject=body.subject,
        body=body.body,
    )
    return PendingActionOut.model_validate(action)
