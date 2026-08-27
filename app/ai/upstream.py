"""Turning a model provider's failure into an answer a caller can act on.

SPEC-v2 §7 is explicit about this one: "Gemini free tier has its own rate
limits — surface a clean 'busy, try again' rather than a 500." Without it, the
single error a public demo is most likely to hit — the free tier's own quota —
reaches the browser as an unexplained 500, which reads as "this project is
broken" rather than "this project is free".

Two outcomes, because they are different facts and a caller does different
things with each. Over quota or rate limited is **429 with Retry-After**:
waiting genuinely helps, and the frontend already renders that case specially
(`ApiError.isRateLimited`). Anything else from the provider is **502**: this
deployment is fine, the upstream is not, and retrying now is reasonable.
Neither is a 500, which would be a claim that this application has a bug.

The classification reads the exception's text, which is crude, and is
deliberately the same compromise `retry_after_backoff` callers already made:
the provider SDKs do not surface a consistent exception type, and importing
`google.api_core` to catch `ResourceExhausted` would tie this module to the one
provider that SPEC-v2 D18 exists to keep swappable.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from app.constants import UPSTREAM_BUSY_RETRY_SECONDS
from app.exceptions import AppError, RateLimited, UpstreamFailure

logger = logging.getLogger(__name__)

# Over quota, or asked to slow down. Waiting is the correct response.
_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "resourceexhausted", "rate limit", "quota")

# Reachable but unwell, or not reachable at all. Also worth retrying, but it is
# not the caller's allowance that is the problem.
_TRANSIENT_MARKERS = ("503", "unavailable", "timeout", "timed out", "deadline", "connection")


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__} {exc}".lower()


def is_rate_limited(exc: Exception) -> bool:
    return any(marker in _describe(exc) for marker in _RATE_LIMIT_MARKERS)


def is_retryable(exc: Exception) -> bool:
    """Whether another attempt at the same call could plausibly succeed.

    Used by the embedder's backoff loop as well as by the classification
    below, so the two can never disagree about what "transient" means.
    """
    text = _describe(exc)
    return any(
        marker in text for marker in (*_RATE_LIMIT_MARKERS, *_TRANSIENT_MARKERS)
    )


def as_app_error(exc: Exception, *, what: str) -> AppError:
    if is_rate_limited(exc):
        return RateLimited(
            f"{what} is busy: the model provider's free-tier limit has been reached. "
            "Please try again shortly.",
            retry_after=UPSTREAM_BUSY_RETRY_SECONDS,
        )

    return UpstreamFailure(f"{what} is temporarily unavailable. Please try again.")


@contextmanager
def provider_errors(what: str) -> Iterator[None]:
    """Wrap a call into a model provider so its failures get a real status code.

    A plain (not async) context manager on purpose: it is only ever used around
    an `await`, and an exception raised by that await propagates through
    `__exit__` exactly the same way. `asyncio.CancelledError` and
    `GeneratorExit` are BaseExceptions, so an abandoned stream passes through
    untouched rather than being reported as an upstream outage.
    """
    try:
        yield
    except AppError:
        # Already carries the status it should. Most often the budget check,
        # which must not be relabelled as somebody else's fault.
        raise
    except Exception as exc:
        # The message is logged here and deliberately not returned to the
        # caller: a provider's error text can name the model, the project and
        # occasionally the key.
        logger.warning(
            "model provider call failed",
            extra={"what": what, "error": type(exc).__name__},
            exc_info=True,
        )
        raise as_app_error(exc, what=what) from exc


# What to call each of them in a message a user reads. Named here so the two
# services and the two providers cannot drift into describing the same outage
# three different ways.
THE_ASSISTANT = "The assistant"
THE_AGENT = "The agent"
THE_EMBEDDER = "Document search"
