from fastapi import APIRouter

from app.api import (
    approvals,
    auth,
    calendar,
    chat,
    documents,
    email,
    health,
    tasks,
    workspace,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(workspace.router)
api_router.include_router(documents.router)
api_router.include_router(chat.router)
api_router.include_router(approvals.router)
api_router.include_router(tasks.router)
api_router.include_router(calendar.router)
api_router.include_router(email.router)
