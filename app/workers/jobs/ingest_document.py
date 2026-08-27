import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chat_model import estimate_tokens
from app.ai.chunking.splitter import extract_pages, split_pages
from app.ai.embeddings.embedder import Embedder
from app.constants import DocumentStatus, UsageKind
from app.database.models.chunk import DocumentChunk
from app.database.models.document import Document
from app.services import usage_service
from app.storage.base import ObjectStore

logger = logging.getLogger(__name__)


class NoExtractableText(Exception):
    """The file parsed, but yielded nothing worth embedding — a scanned PDF
    with no text layer, or an empty document. Retrying will not help, but the
    attempt counter will still retire it, and the user sees error_message."""


async def ingest_document(
    db: AsyncSession,
    store: ObjectStore,
    embedder: Embedder,
    *,
    document_id: uuid.UUID,
) -> int:
    """Fetch, extract, chunk, embed, store. Returns the chunk count.

    Idempotent by construction: the existing chunks for this document are
    deleted and rewritten inside the same transaction that flips the document
    to `ready`. A process killed anywhere in here leaves the document exactly
    as it was, and the reclaimed job simply does the whole thing again.
    """
    document = await db.get(Document, document_id)
    if document is None:
        # Deleted between enqueue and claim. Not an error — nothing to do.
        logger.info("ingestion skipped, document is gone", extra={"document_id": str(document_id)})
        return 0

    document.status = DocumentStatus.PROCESSING
    await db.commit()

    data = await store.get(document.storage_key)
    pages = extract_pages(data, mime_type=document.mime_type)
    chunks = split_pages(pages)

    if not chunks:
        raise NoExtractableText(
            "No readable text could be extracted from this file. If it is a scanned "
            "document, it needs to be run through OCR first."
        )

    vectors = await embedder.embed_documents([chunk.text for chunk in chunks])

    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    db.add_all(
        [
            DocumentChunk(
                workspace_id=document.workspace_id,
                document_id=document.id,
                text=chunk.text,
                page_no=chunk.page_no,
                chunk_index=chunk.chunk_index,
                embedding=vector,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
    )

    # Embedding a corpus is the largest single piece of token spend in this
    # application, and leaving it out of the ledger would make /admin/usage
    # report a number that bears no relation to the bill. Attributed to the
    # uploader, and counted again on a re-ingest -- because a re-ingest really
    # does embed everything a second time.
    usage_service.record(
        db,
        workspace_id=document.workspace_id,
        user_id=document.uploaded_by,
        kind=UsageKind.EMBEDDING,
        model=embedder.model_name,
        tokens_in=sum(estimate_tokens(chunk.text) for chunk in chunks),
        tokens_out=0,
        estimated=True,
    )

    document.status = DocumentStatus.READY
    document.chunk_count = len(chunks)
    document.error_message = None
    document.embedding_model = embedder.model_name
    await db.commit()

    logger.info(
        "document ingested",
        extra={
            "document_id": str(document.id),
            "workspace_id": str(document.workspace_id),
            "chunk_count": len(chunks),
        },
    )
    return len(chunks)
