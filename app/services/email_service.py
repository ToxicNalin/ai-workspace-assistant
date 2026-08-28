"""Sending mail, behind an interface with three implementations.

SPEC-v2 D16 is the reason this is not the Gmail API. `gmail.send` is a
*restricted* scope: shipping it publicly needs a CASA security assessment by a
Google-empanelled assessor, takes several weeks, and must be recertified every
year. That is not something you finish before a deadline, so the default is a
transactional provider with a permanent free tier, and Gmail stays a documented
stub rather than a half-built integration.

`From:` is a no-reply address the project controls; `Reply-To:` is the person
who asked for the action. That split matters on a shared sender: a reply should
reach the colleague who requested the email, not a mailbox nobody reads -- and
the recipient can see who is actually behind the message.
"""

import base64
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.constants import (
    BREVO_API_URL,
    BREVO_TIMEOUT_SECONDS,
    RESEND_API_URL,
    RESEND_TIMEOUT_SECONDS,
)
from app.exceptions import UpstreamFailure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class OutboundEmail:
    """A message with its recipients already resolved to real addresses.

    There is no path that builds one of these from a string the model
    produced. Every address here came out of `workspace_members` via
    app/ai/tools/resolve.py (SPEC-v2 D21).
    """

    to: list[str]
    subject: str
    body: str
    reply_to: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class SentEmail:
    provider: str
    message_id: str
    recipients: list[str]


class EmailProvider(Protocol):
    name: str

    async def send(self, message: OutboundEmail) -> SentEmail: ...


class ConsoleEmailProvider:
    """Records the message and logs it instead of sending it.

    The offline half of the same fake/real split used for embeddings and the
    chat model, and it exists for the same reason: a fresh clone should be able
    to drive an approved action all the way through without an API key, a
    network call, or something landing in a stranger's inbox. Tests read
    `outbox` to assert what would have gone out.
    """

    name = "console"

    def __init__(self) -> None:
        self.outbox: list[OutboundEmail] = []

    async def send(self, message: OutboundEmail) -> SentEmail:
        self.outbox.append(message)
        logger.info(
            "email (not actually sent: console provider)",
            extra={
                "to": message.to,
                "subject": message.subject,
                "attachments": [item.filename for item in message.attachments],
            },
        )
        return SentEmail(
            provider=self.name,
            message_id=f"console-{len(self.outbox)}",
            recipients=list(message.to),
        )

    def clear(self) -> None:
        self.outbox.clear()


def _refusal_reason(response: httpx.Response) -> str:
    """The provider's own explanation for a 4xx/5xx, if it gave one.

    Defensive on every step: a refusal is exactly the moment a provider is
    least likely to return the JSON its documentation promises, and a
    diagnostic that raises while explaining a failure is worse than no
    diagnostic. Truncated because it ends up in an HTTP response body.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    return str(message)[:300] if isinstance(message, str) else ""


class ResendEmailProvider:
    """Resend's REST API. One POST, no SDK.

    A dependency for a single JSON endpoint would be a dependency to keep
    up to date, audit and explain, in exchange for saving a dozen lines.
    """

    name = "resend"

    def __init__(self, *, api_key: str, from_address: str, from_name: str) -> None:
        self._api_key = api_key
        self._from = f"{from_name} <{from_address}>" if from_name else from_address

    def _payload(self, message: OutboundEmail) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "from": self._from,
            "to": message.to,
            "subject": message.subject,
            "text": message.body,
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if message.attachments:
            payload["attachments"] = [
                {
                    "filename": item.filename,
                    "content": base64.b64encode(item.content).decode("ascii"),
                    "content_type": item.content_type,
                }
                for item in message.attachments
            ]
        return payload

    async def send(self, message: OutboundEmail) -> SentEmail:
        if not self._api_key:
            raise UpstreamFailure("The email provider is not configured")

        try:
            async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as http:
                response = await http.post(
                    RESEND_API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=self._payload(message),
                )
        except httpx.HTTPError as exc:
            # The key is in a header rather than the body, but log nothing from
            # the request regardless -- an exception repr is the classic way a
            # credential ends up in a log aggregator.
            logger.warning("email provider unreachable", extra={"error": type(exc).__name__})
            raise UpstreamFailure("The email provider could not be reached") from exc

        if response.status_code >= 400:
            # Resend's refusals are self-explaining and carry no credential --
            # "you can only send testing emails to your own email address",
            # "domain is not verified". Swallowing that and reporting a bare
            # status code is what turns a five-minute dashboard fix into an
            # afternoon, so the message is passed through to whoever approved
            # the action. Only the provider's own `message` field, never the
            # whole body and never the request.
            reason = _refusal_reason(response)
            logger.warning(
                "email provider refused the message",
                extra={"status": response.status_code, "reason": reason},
            )
            raise UpstreamFailure(
                f"The email provider refused the message ({response.status_code})"
                + (f": {reason}" if reason else "")
            )

        body = response.json()
        return SentEmail(
            provider=self.name,
            message_id=str(body.get("id", "")),
            recipients=list(message.to),
        )


class BrevoEmailProvider:
    """Brevo's transactional API. Same shape as Resend, different JSON.

    Here for one reason: it is the only free route to emailing people who are
    not you. Since Gmail, Yahoo and Microsoft tightened sender requirements in
    2024, sending to arbitrary recipients needs an authenticated sending
    domain, and a free webmail domain cannot be authenticated by anybody --
    so `onboarding@resend.dev` and `something@gmail.com` are both dead ends for
    a project with no domain of its own.

    Brevo takes a single address verified by a code instead. The cost is that
    it rewrites the visible `From:` to a compliant address of its own when the
    sender is free webmail. That is a real downgrade in polish and worth being
    honest about -- but `Reply-To:` survives it, which is the header actually
    carrying the person who asked for the action (SPEC-v2 D16).

    Verifying a domain and switching back to Resend is two values in a
    dashboard, and nothing above this class changes. That interchangeability
    was the point of the interface long before there was a second provider to
    prove it.
    """

    name = "brevo"

    def __init__(self, *, api_key: str, from_address: str, from_name: str) -> None:
        self._api_key = api_key
        self._from_address = from_address
        self._from_name = from_name

    def _payload(self, message: OutboundEmail) -> dict[str, Any]:
        sender: dict[str, str] = {"email": self._from_address}
        if self._from_name:
            # Brevo caps the display name at 70 characters and rejects the
            # whole request over it, which would be an odd way to lose an
            # email.
            sender["name"] = self._from_name[:70]

        payload: dict[str, Any] = {
            "sender": sender,
            "to": [{"email": address} for address in message.to],
            "subject": message.subject,
            "textContent": message.body,
        }
        if message.reply_to:
            payload["replyTo"] = {"email": message.reply_to}
        if message.attachments:
            # Singular "attachment", and "name" rather than "filename" -- the
            # two providers disagree on both, which is exactly the sort of
            # detail that belongs in one adapter rather than in the caller.
            payload["attachment"] = [
                {
                    "name": item.filename,
                    "content": base64.b64encode(item.content).decode("ascii"),
                }
                for item in message.attachments
            ]
        return payload

    async def send(self, message: OutboundEmail) -> SentEmail:
        if not self._api_key:
            raise UpstreamFailure("The email provider is not configured")

        try:
            async with httpx.AsyncClient(timeout=BREVO_TIMEOUT_SECONDS) as http:
                response = await http.post(
                    BREVO_API_URL,
                    headers={"api-key": self._api_key, "accept": "application/json"},
                    json=self._payload(message),
                )
        except httpx.HTTPError as exc:
            logger.warning("email provider unreachable", extra={"error": type(exc).__name__})
            raise UpstreamFailure("The email provider could not be reached") from exc

        if response.status_code >= 400:
            reason = _refusal_reason(response)
            logger.warning(
                "email provider refused the message",
                extra={"status": response.status_code, "reason": reason},
            )
            raise UpstreamFailure(
                f"The email provider refused the message ({response.status_code})"
                + (f": {reason}" if reason else "")
            )

        body = response.json()
        return SentEmail(
            provider=self.name,
            message_id=str(body.get("messageId", "")),
            recipients=list(message.to),
        )


class GmailEmailProvider:
    """Not shipped. Kept as a class so the blocker is documented in code.

    Sending as the user through Gmail needs the `gmail.send` scope, which
    Google classifies as *restricted*. Publishing an app that requests it
    requires a CASA (Cloud Application Security Assessment) tier-2 review by an
    assessor from Google's panel, takes several weeks, and has to be redone
    every year. SPEC-v2 D16 identified that as a dead end for a project with a
    deadline and took the transactional-provider route instead.

    The interface is the point: if that assessment were ever completed, this
    class is where it would land, and nothing above it would change.
    """

    name = "gmail"

    async def send(self, message: OutboundEmail) -> SentEmail:
        raise UpstreamFailure(
            "Gmail sending is not enabled: gmail.send is a restricted scope requiring a "
            "CASA security assessment (SPEC-v2 D16). Use the Resend provider."
        )


@lru_cache
def get_email_provider() -> EmailProvider:
    """The only place a mail provider is chosen."""
    settings = get_settings()

    if settings.email_provider == "resend":
        return ResendEmailProvider(
            api_key=settings.resend_api_key,
            from_address=settings.email_from_address,
            from_name=settings.email_from_name,
        )

    if settings.email_provider == "brevo":
        return BrevoEmailProvider(
            api_key=settings.brevo_api_key,
            from_address=settings.email_from_address,
            from_name=settings.email_from_name,
        )

    return ConsoleEmailProvider()


def provider_is_configured() -> bool:
    """Whether the selected provider could actually deliver a message.

    The console provider always can -- delivering to a list in memory is what
    it does. The real ones cannot without a key, and saying so on
    `/email/status` is better than finding out at the moment somebody approves
    an action.
    """
    return _missing_api_key() is None


def _missing_api_key() -> str | None:
    """The name of the key the selected provider needs and has not been given.

    One function so that adding a provider cannot leave `/email/status`
    answering for the old one -- the failure that would produce is a
    deployment reporting itself configured while every send raises.
    """
    settings = get_settings()
    required = {"resend": "RESEND_API_KEY", "brevo": "BREVO_API_KEY"}.get(
        settings.email_provider
    )
    if required is None:
        return None

    key = {"resend": settings.resend_api_key, "brevo": settings.brevo_api_key}[
        settings.email_provider
    ]
    if key:
        return None
    return f"{required} is not set, so the email provider cannot authenticate."


def undeliverable_reason() -> str | None:
    """Why mail from this deployment cannot reach a real inbox, or None.

    A stricter question than `provider_is_configured`, and the distinction is
    the whole point. Console is a perfectly *configured* provider; it just
    delivers to a list in memory. Locally and in tests that is the intent. In
    production it means every approved email is recorded as sent and none of
    them exist, which is the worst failure mode this application has: silent,
    and indistinguishable from success to the person who approved it.

    Callers use this to make that state loud -- a warning at boot, a refusal
    at execution time -- rather than letting it be discovered by a colleague
    who never received the message.
    """
    settings = get_settings()

    if settings.email_provider == "console":
        return (
            "EMAIL_PROVIDER is 'console', which records messages instead of sending them. "
            "Set EMAIL_PROVIDER to a real provider, with its API key, to deliver mail."
        )
    return _missing_api_key()


def delivery_blocked() -> str | None:
    """`undeliverable_reason()`, but only where it is a mistake rather than
    the point.

    The environment check lives here rather than at each call site so that
    "console is intended locally, and a misconfiguration in production" is one
    fact in one place -- every caller that has to distinguish the two asks the
    same question and gets the same answer.
    """
    if get_settings().environment != "production":
        return None
    return undeliverable_reason()
