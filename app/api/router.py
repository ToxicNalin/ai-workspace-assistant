from fastapi import APIRouter

from app.api import auth, documents, health, workspace

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(workspace.router)
api_router.include_router(documents.router)
