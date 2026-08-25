import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieval hit, carrying everything a citation needs.

    `document_name` and `page_no` are read across the join here rather than
    looked up later, because the citation written from this snapshot has to
    survive the document being deleted (SPEC-v2 D5).
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    text: str
    page_no: int | None
    score: float
