import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped


class MessageCitation(Base, UUIDPrimaryKey, WorkspaceScoped):
    """Evidence for one assistant message, snapshotted at the time it answered.

    SPEC-v2 D5: the denormalised columns are the point. A plain FK to the chunk
    would dangle the moment the source document is deleted, and the chat
    history would silently lose the evidence it was built on. Everything the
    UI needs to render a citation is copied here, so a citation stays readable
    long after its chunk is gone.
    """

    __tablename__ = "message_citations"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    # Nullable and SET NULL, not CASCADE: losing the chunk must not delete the
    # citation that quoted it.
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL"), default=None
    )
    document_name: Mapped[str] = mapped_column(String(255))
    quoted_text: Mapped[str] = mapped_column(Text)
    # Part of the same snapshot: once the chunk is gone, chunk_id is NULL and
    # this is the only record of where in the document the quote came from.
    page_no: Mapped[int | None] = mapped_column(Integer, default=None)
    score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
