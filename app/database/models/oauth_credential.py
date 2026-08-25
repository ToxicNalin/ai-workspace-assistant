import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import OAuthService
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey
from app.database.types import str_enum


class OAuthCredential(Base, UUIDPrimaryKey):
    """A user's refresh token for a third-party service, encrypted at rest.

    Not workspace-scoped: the grant is between a person and Google, and it
    follows that person across every workspace they belong to.

    Nothing in this project writes a row here yet, and that is the point rather
    than an omission. SPEC-v2 §9 lists Google Calendar and Gmail as explicit
    non-goals -- both need restricted or sensitive scopes with multi-week
    verification, so they are abstracted behind an interface instead of
    shipped. The table exists so the interface has a real place to store a
    credential the day someone completes that verification, and so
    app/services/calendar_service.py's Google path can say "not connected"
    from a query rather than from a hardcoded False.

    The token is Fernet-encrypted through app/utils/crypto.py before it gets
    here. A refresh token is a bearer credential for someone else's account:
    the same reasoning that hashes invite tokens (D6) applies, except this one
    has to be readable again, so it is encrypted rather than hashed.
    """

    __tablename__ = "oauth_credentials"
    __table_args__ = (UniqueConstraint("user_id", "service"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    service: Mapped[OAuthService] = mapped_column(str_enum(OAuthService, name="oauth_service"))
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(Text, default="", server_default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
