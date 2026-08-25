import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.constants import ChatRole
from app.schemas.common import ORMModel


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # Omit to start a new thread. A thread belonging to another workspace is
    # indistinguishable from one that does not exist.
    thread_id: uuid.UUID | None = None


class CitationOut(ORMModel):
    id: uuid.UUID
    # None once the source chunk has been deleted. The fields below are a
    # snapshot taken when the answer was written, so the citation stays
    # readable regardless (SPEC-v2 D5).
    chunk_id: uuid.UUID | None
    document_name: str
    quoted_text: str
    page_no: int | None
    score: float


class MessageOut(ORMModel):
    id: uuid.UUID
    role: ChatRole
    content: str
    created_at: datetime
    citations: list[CitationOut] = []


class ChatResponse(BaseModel):
    thread_id: uuid.UUID
    message: MessageOut


class ThreadOut(ORMModel):
    id: uuid.UUID
    title: str
    created_at: datetime
