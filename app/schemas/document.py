import uuid
from datetime import datetime

from app.constants import DocumentStatus
from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    error_message: str | None
    uploaded_by: uuid.UUID
    uploaded_at: datetime


class DocumentUploadResponse(ORMModel):
    document: DocumentOut
    # True when this upload matched an existing document's content hash --
    # nothing new was stored, the existing row was returned instead.
    deduplicated: bool
