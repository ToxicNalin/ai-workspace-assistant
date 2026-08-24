import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MAX_DOCUMENTS_PER_WORKSPACE, MAX_UPLOAD_SIZE_BYTES, DocumentStatus
from app.database.models.document import Document
from app.exceptions import Conflict, NotFound
from app.storage.base import ObjectStore
from app.utils.validators import validate_upload
from app.workers import queue


async def list_documents(db: AsyncSession, workspace_id: uuid.UUID) -> Sequence[Document]:
    result = await db.scalars(
        select(Document)
        .where(Document.workspace_id == workspace_id)
        .order_by(Document.uploaded_at.desc())
    )
    return result.all()


async def get_document(
    db: AsyncSession, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    document = await db.scalar(
        select(Document).where(Document.id == document_id, Document.workspace_id == workspace_id)
    )
    if document is None:
        raise NotFound
    return document


async def upload_document(
    db: AsyncSession,
    store: ObjectStore,
    *,
    workspace_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    filename: str,
    data: bytes,
) -> tuple[Document, bool]:
    mime_type = validate_upload(filename=filename, data=data, max_size_bytes=MAX_UPLOAD_SIZE_BYTES)
    content_hash = hashlib.sha256(data).hexdigest()

    existing = await db.scalar(
        select(Document).where(
            Document.workspace_id == workspace_id, Document.content_hash == content_hash
        )
    )
    if existing is not None:
        # Same bytes, so nothing to store or re-embed. A previous ingestion
        # that gave up is the one exception: re-uploading is the only way a
        # user can ask for another go, so let it queue a fresh job.
        if existing.status == DocumentStatus.FAILED:
            existing.status = DocumentStatus.PENDING
            existing.error_message = None
            await queue.purge_terminal_jobs(db, existing.id)
            await queue.enqueue(db, workspace_id=workspace_id, document_id=existing.id)
            await db.commit()
            await db.refresh(existing)
        return existing, True

    document_count = await db.scalar(
        select(func.count()).select_from(Document).where(Document.workspace_id == workspace_id)
    )
    if (document_count or 0) >= MAX_DOCUMENTS_PER_WORKSPACE:
        raise Conflict("This workspace has reached its document limit")

    storage_key = f"{workspace_id}/{uuid.uuid4()}-{filename}"
    # Store the object before the row exists: if the DB insert then fails,
    # the worst case is an orphaned object in the bucket. The other order
    # risks a row pointing at an object that was never actually written.
    await store.put(storage_key, data, content_type=mime_type)

    document = Document(
        workspace_id=workspace_id,
        name=filename,
        storage_key=storage_key,
        content_hash=content_hash,
        mime_type=mime_type,
        size_bytes=len(data),
        uploaded_by=uploaded_by,
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    # Flushed, not committed: the document row and the job that will process
    # it land in one transaction, so there can never be a pending document
    # with no job queued for it.
    await db.flush()
    await queue.enqueue(db, workspace_id=workspace_id, document_id=document.id)
    await db.commit()
    await db.refresh(document)
    return document, False


async def delete_document(
    db: AsyncSession, store: ObjectStore, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    document = await get_document(db, workspace_id, document_id)
    await store.delete(document.storage_key)
    await db.delete(document)
    await db.commit()
