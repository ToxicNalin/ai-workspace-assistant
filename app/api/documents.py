import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from app.auth.permissions import require_member
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.document import DocumentOut, DocumentUploadResponse
from app.services import document_service
from app.storage.base import get_object_store

router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["documents"])


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> list[DocumentOut]:
    documents = await document_service.list_documents(db, context.workspace_id)
    return [DocumentOut.model_validate(document) for document in documents]


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    response: Response,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
    file: Annotated[UploadFile, File()],
) -> DocumentUploadResponse:
    data = await file.read()
    document, deduplicated = await document_service.upload_document(
        db,
        get_object_store(),
        workspace_id=context.workspace_id,
        uploaded_by=context.user.id,
        filename=file.filename or "unnamed",
        data=data,
    )
    response.status_code = status.HTTP_200_OK if deduplicated else status.HTTP_201_CREATED
    return DocumentUploadResponse(
        document=DocumentOut.model_validate(document), deduplicated=deduplicated
    )


@router.get("/{document_id}/status", response_model=DocumentOut)
async def document_status(
    document_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> DocumentOut:
    document = await document_service.get_document(db, context.workspace_id, document_id)
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> None:
    await document_service.delete_document(
        db, get_object_store(), context.workspace_id, document_id
    )
