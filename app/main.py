from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.database.session import async_session_factory
from app.exceptions import AppError
from app.lifespan import lifespan
from app.middleware.errors import app_error_handler
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.utils.logger import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title="AI Workspace Assistant", lifespan=lifespan)

# How the rate limiter reaches the database. On app.state rather than imported
# directly by the middleware so the test suite can substitute the session its
# per-test transaction is bound to -- without it the limiter would write
# committed counter rows outside that transaction, leaving rows behind and
# making one test's count depend on how many ran before it.
app.state.db_session_factory = async_session_factory

# Order matters, and it is the reverse of what it reads like: Starlette makes
# the *last* middleware added the outermost one. So this list runs bottom-up
# -- CORS wraps everything, then request context, then the rate limiter.
#
# CORS has to be outermost. The limiter answers 429 without calling the rest
# of the stack, and a 429 that CORS never saw arrives at a browser with no
# Access-Control-Allow-Origin header, where it surfaces as an opaque network
# error rather than "you are being rate limited".
#
# Request context sits above the limiter so that a rejected request still logs
# with an id and a user.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Both are set by middleware rather than by a route, so a browser client
    # cannot read them unless they are named here.
    expose_headers=[
        "X-Request-ID",
        "Retry-After",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
    ],
)

app.add_exception_handler(AppError, app_error_handler)

app.include_router(api_router)
