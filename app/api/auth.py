from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Response, status

from app.auth.cookies import clear_refresh_cookie, set_refresh_cookie
from app.config import get_settings
from app.constants import REFRESH_COOKIE_NAME
from app.dependencies import CurrentUser, DbSession
from app.exceptions import Unauthorized
from app.schemas.auth import LoginRequest, RegisterRequest, SessionOut, UserOut
from app.services import auth_service
from app.services.auth_service import IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])

# FastAPI reads the header name from the parameter name, and a Python
# identifier cannot contain a hyphen -- so the alias is what makes this
# `X-CSRF-Token` rather than `x_csrf_token`.
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)]


def _session(response: Response, tokens: IssuedTokens) -> SessionOut:
    """Split one set of credentials across the two channels D19 chose.

    The refresh token goes into the cookie and is deliberately absent from the
    body; the access token and the CSRF value go into the body and are
    deliberately absent from any cookie.
    """
    set_refresh_cookie(response, tokens.refresh_token)
    return SessionOut(
        access_token=tokens.access_token,
        csrf_token=tokens.csrf_token,
        expires_in=get_settings().access_token_expire_minutes * 60,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: DbSession) -> UserOut:
    user = await auth_service.register(db, email=body.email, password=body.password, name=body.name)
    return UserOut.model_validate(user)


@router.post("/login", response_model=SessionOut)
async def login(body: LoginRequest, response: Response, db: DbSession) -> SessionOut:
    user = await auth_service.authenticate(db, email=body.email, password=body.password)
    return _session(response, await auth_service.issue_tokens(db, user))


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    response: Response,
    db: DbSession,
    refresh_token: RefreshCookie = None,
    csrf_token: CsrfHeader = None,
) -> SessionOut:
    """Trade the cookie for a new access token, rotating the cookie.

    The only endpoint in the application authenticated by a cookie, which is
    why it is also the only one needing a CSRF header. Confining that to one
    route is what SPEC-v2 D19 bought by keeping the *access* token out of a
    cookie.
    """
    if refresh_token is None:
        raise Unauthorized("Invalid or expired token")

    tokens = await auth_service.rotate_refresh_token(
        db, refresh_token, csrf_header=csrf_token
    )
    return _session(response, tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, db: DbSession, user: CurrentUser) -> None:
    """Authenticated by the access token, not the cookie.

    So it needs no CSRF header: another site cannot set an Authorization
    header on a cross-origin request without a preflight the CORS allowlist
    refuses. Clearing the cookie is a courtesy -- the refresh token is dead
    server-side the moment `current_refresh_jti` is cleared, whether or not the
    browser co-operates.
    """
    await auth_service.logout(db, user)
    clear_refresh_cookie(response)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
