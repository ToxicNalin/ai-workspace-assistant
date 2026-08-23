from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.exceptions import AppError
from app.middleware.errors import app_error_handler
from app.utils.logger import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="AI Workspace Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(AppError, app_error_handler)

app.include_router(api_router)
