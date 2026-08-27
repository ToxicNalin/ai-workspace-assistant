"""Where the refresh token lives, and what has to accompany it.

SPEC-v2 D19 settled a question v1 left open: the access token is held in a
JavaScript variable and the refresh token in an httpOnly cookie. Only the
second half needs code, and it brings two problems the first half does not.

**Cross-site.** The SPA is served from Cloudflare Pages and the API from
Render. Those are different sites, so the cookie needs `SameSite=None`, which
a browser only honours alongside `Secure`. Both are derived from
`environment` rather than hardcoded, because neither is usable over plain
http://localhost.

**CSRF.** A cookie is attached by the browser whether or not the page asking
for it is ours, so `POST /auth/refresh` cannot treat possession of the cookie
as the whole of the authorisation -- any site could cause a session to be
rotated out from under its owner. The defence is a synchroniser token, not the
double-submit variety: the SPA is on a different origin from the API, so
`document.cookie` there cannot see an API cookie at all, and a token the
client is unable to read is one it is unable to echo. Instead the value is
minted into the refresh token as a claim, returned to the client in the
response *body*, and required back in a header. Another origin can neither
read that body nor set a custom header without a preflight that the CORS
allowlist refuses.

Keeping this to the one endpoint that authenticates by cookie is the whole
reason the access token was kept out of one.
"""

import secrets

from fastapi import Response

from app.config import get_settings
from app.constants import (
    CSRF_HEADER_NAME,
    CSRF_TOKEN_BYTES,
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
)
from app.exceptions import Unauthorized


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response: Response) -> None:
    # Every attribute that identified the cookie has to be repeated. A browser
    # keys a cookie on name, domain *and* path, so a delete that omits the path
    # writes a second, empty cookie at "/" and leaves the real one intact.
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


def verify_csrf(*, refresh_claims: dict[str, object], header: str | None) -> None:
    """Check the header against the claim inside the cookie's own token.

    Deliberately the same `Unauthorized` a bad cookie raises. Telling a caller
    that its cookie was fine and only the header was wrong confirms it holds a
    valid session, which is precisely what a cross-site caller is fishing for.
    """
    expected = refresh_claims.get("csrf")
    if not isinstance(expected, str) or not expected:
        # A token minted before this claim existed. It cannot be presented
        # safely, so it is not accepted -- the holder logs in once more.
        raise Unauthorized("Invalid or expired token")

    if header is None or not secrets.compare_digest(header, expected):
        raise Unauthorized("Invalid or expired token")


__all__ = [
    "CSRF_HEADER_NAME",
    "clear_refresh_cookie",
    "new_csrf_token",
    "set_refresh_cookie",
    "verify_csrf",
]
