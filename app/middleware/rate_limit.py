"""Per-user and per-IP request limits, counted in Postgres.

SPEC-v2 D15 chose Postgres counters over Redis so the free tier needs one
fewer service, and §7 lists this as non-negotiable for a public demo: an app
holding a live LLM key with no ceiling in front of it is an open invitation.

The counter is a fixed window -- one row per identity per window, incremented
by a single upsert that returns the new value. A sliding window would need a
row per request, which is a great deal of writing to answer a question this
coarse, and Neon's free tier is 0.5 GB.

Two decisions here are worth stating plainly because neither is obvious:

*The limiter fails open.* If the counter query raises -- Neon scaling from
zero, a dropped connection, a pool timeout -- the request is allowed through
and the failure is logged. A limiter that takes the site down whenever the
database hiccups has caused more outage than the abuse it exists to prevent,
and on a tier that sleeps after five minutes idle those hiccups are routine.

*The client address comes from the last X-Forwarded-For entry, not the first.*
Render terminates TLS and proxies, so request.client.host is Render's address
and every visitor would share one bucket. Each proxy appends the address it
received the request from, so the entry Render appended -- the last one -- is
the real client. The first entry is whatever the client claimed, which is
attacker-controlled and exactly the wrong thing to key a limit on.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth.jwt import decode_access_token
from app.config import get_settings
from app.constants import (
    RATE_LIMIT_EXEMPT_PATHS,
    RATE_LIMIT_PRUNE_PROBABILITY,
    RATE_LIMIT_RETENTION_SECONDS,
    RATE_LIMIT_WINDOW_SECONDS,
    REGISTRATION_WINDOW_SECONDS,
    RateLimitScope,
)
from app.database.models.rate_limit_counter import RateLimitCounter
from app.database.session import async_session_factory
from app.exceptions import Unauthorized

logger = logging.getLogger(__name__)

REGISTRATION_PATH = "/auth/register"


def client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Last entry: see the module docstring. Empty segments are skipped so
        # a trailing comma cannot produce a bucket everyone shares.
        candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
        if candidates:
            return candidates[-1]

    return request.client.host if request.client else "unknown"


def identify(request: Request) -> str:
    """Who this request counts against.

    The token is decoded, never looked up -- the limiter runs before routing
    and must not spend a database round trip establishing identity for one it
    is about to spend another counting. An invalid or absent token simply
    falls back to the address, which is the correct behaviour anyway: an
    unauthenticated caller is exactly who a per-IP limit is for.
    """
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")

    if scheme.lower() == "bearer" and token:
        try:
            subject = decode_access_token(token).get("sub")
        except Unauthorized:
            subject = None
        if subject:
            return f"user:{subject}"

    return f"ip:{client_address(request)}"


def window_start(now: datetime, seconds: int) -> datetime:
    """Floor `now` to the start of its window.

    Every caller in the same window derives the same timestamp, which is what
    makes the upsert's conflict target land on one shared row rather than one
    row per request.
    """
    epoch_seconds = int(now.timestamp())
    return datetime.fromtimestamp(epoch_seconds - (epoch_seconds % seconds), tz=UTC)


async def hit(
    db: AsyncSession, *, bucket: str, scope: RateLimitScope, window_seconds: int
) -> tuple[int, int]:
    """Count one request. Returns (count in this window, seconds until it resets)."""
    now = datetime.now(UTC)
    start = window_start(now, window_seconds)

    statement = (
        pg_insert(RateLimitCounter)
        .values(bucket=bucket, scope=scope, window_start=start, count=1)
        .on_conflict_do_update(
            constraint="uq_rate_limit_counters_window",
            # Refers to the row already there; `excluded` would be the one we
            # just tried to insert, which is always 1.
            set_={"count": RateLimitCounter.count + 1},
        )
        .returning(RateLimitCounter.count)
    )

    count = await db.scalar(statement)
    reset_in = max(1, int((start + timedelta(seconds=window_seconds) - now).total_seconds()))
    return int(count or 1), reset_in


async def prune(db: AsyncSession) -> None:
    cutoff = datetime.now(UTC) - timedelta(seconds=RATE_LIMIT_RETENTION_SECONDS)
    await db.execute(delete(RateLimitCounter).where(RateLimitCounter.window_start < cutoff))


def _too_many(detail: str, retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": detail},
        headers={"Retry-After": str(retry_after)},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware rather than a dependency, deliberately.

    A dependency would run only on routes that remembered to declare it, and
    the route most worth limiting is whichever one gets added next without
    anyone thinking about it. This sits in front of everything and exempts by
    name instead -- a much shorter list to keep honest.

    Note it returns its 429 directly rather than raising RateLimited. Starlette
    puts the exception middleware *inside* the user middleware stack, so an
    AppError raised here would never reach app_error_handler and would surface
    as a 500.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()

        if not settings.rate_limit_enabled or request.url.path in RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)

        bucket = identify(request)
        is_registration = (
            request.url.path == REGISTRATION_PATH and request.method == "POST"
        )

        try:
            # Opened, used and released before the route runs, so the
            # limiter's connection is never held for the duration of a request
            # -- the pool is pool_size=2 on a 512 MB instance.
            factory = getattr(request.app.state, "db_session_factory", async_session_factory)
            async with factory() as db:
                count, reset_in = await hit(
                    db,
                    bucket=bucket,
                    scope=RateLimitScope.REQUEST,
                    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
                )
                over_request_limit = count > settings.rate_limit_requests_per_minute

                over_registration_limit = False
                registration_reset = reset_in
                if is_registration and not over_request_limit:
                    # Registration carries its own, much smaller allowance
                    # (SPEC-v2 §7). Folding it into the general limit would
                    # make sixty requests a minute mean sixty accounts a
                    # minute, which is not a limit on anything.
                    registrations, registration_reset = await hit(
                        db,
                        bucket=f"ip:{client_address(request)}",
                        scope=RateLimitScope.REGISTER,
                        window_seconds=REGISTRATION_WINDOW_SECONDS,
                    )
                    over_registration_limit = (
                        registrations > settings.rate_limit_registrations_per_hour
                    )

                if random.random() < RATE_LIMIT_PRUNE_PROBABILITY:
                    await prune(db)

                await db.commit()
        except SQLAlchemyError:
            # Fail open. See the module docstring.
            logger.warning("rate limiter unavailable, allowing request", exc_info=True)
            return await call_next(request)

        if over_request_limit:
            logger.info("rate limited", extra={"bucket": bucket, "count": count})
            return _too_many(
                "Too many requests. Please wait a moment and try again.", reset_in
            )

        if over_registration_limit:
            logger.info("registration rate limited", extra={"bucket": bucket})
            return _too_many(
                "Too many accounts created from this address. Please try again later.",
                registration_reset,
            )

        response = await call_next(request)
        remaining = max(0, settings.rate_limit_requests_per_minute - count)
        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
