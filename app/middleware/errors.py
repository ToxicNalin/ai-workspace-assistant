from fastapi import Request
from fastapi.responses import JSONResponse

from app.exceptions import AppError, RateLimited


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's add_exception_handler stub is typed for the Exception base
    # regardless of the class it's registered against; this is only ever
    # invoked for AppError and its subclasses.
    assert isinstance(exc, AppError)

    headers: dict[str, str] = {}
    # A 429 raised from inside a route -- the daily token budget, rather than
    # the request limiter, which returns its own response before any route is
    # reached. Both must answer with the same header, or a client would have
    # to know which of the two limits it hit to know whether to wait.
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)

    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=headers or None
    )
