from fastapi import Request
from fastapi.responses import JSONResponse

from app.exceptions import AppError


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's add_exception_handler stub is typed for the Exception base
    # regardless of the class it's registered against; this is only ever
    # invoked for AppError and its subclasses.
    assert isinstance(exc, AppError)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
