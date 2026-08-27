import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import new_csrf_token, verify_csrf
from app.auth.jwt import create_access_token, create_refresh_token, decode_refresh_token
from app.auth.password import hash_password, verify_password
from app.database.models.user import User
from app.exceptions import Conflict, Unauthorized


async def register(db: AsyncSession, *, email: str, password: str, name: str) -> User:
    email = email.strip().lower()

    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise Conflict("An account with this email already exists")

    user = User(email=email, password_hash=hash_password(password), name=name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User:
    email = email.strip().lower()

    user = await db.scalar(select(User).where(User.email == email))
    # Deliberately identical error for "no such user" and "wrong password" --
    # distinguishing them would let a caller enumerate registered emails.
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise Unauthorized("Incorrect email or password")

    return user


@dataclass(frozen=True)
class IssuedTokens:
    """One session's credentials, before the route decides how each travels.

    They leave by three different routes -- access token in the body, refresh
    token in an httpOnly cookie, CSRF value in the body again -- and that is a
    transport decision, so it belongs in app/api/auth.py rather than here.
    """

    access_token: str
    refresh_token: str
    csrf_token: str


async def issue_tokens(db: AsyncSession, user: User) -> IssuedTokens:
    jti = uuid.uuid4()
    csrf = new_csrf_token()
    user.current_refresh_jti = jti
    await db.commit()

    return IssuedTokens(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id, jti, csrf),
        csrf_token=csrf,
    )


async def rotate_refresh_token(
    db: AsyncSession, refresh_token: str, *, csrf_header: str | None
) -> IssuedTokens:
    """Exchange a refresh token for a fresh pair, invalidating the old one.

    The CSRF check happens here, before the token is looked up, because the
    claims it needs come out of the same decode -- and because a rotation that
    the page's own script did not ask for is exactly what it is there to stop.
    """
    payload = decode_refresh_token(refresh_token)
    verify_csrf(refresh_claims=payload, header=csrf_header)

    try:
        user_id = uuid.UUID(payload["sub"])
        token_jti = uuid.UUID(payload["jti"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Invalid or expired token") from exc

    user = await db.get(User, user_id)
    if (
        user is None
        or not user.is_active
        or user.current_refresh_jti is None
        or user.current_refresh_jti != token_jti
    ):
        # A mismatched jti means this refresh token was already rotated out
        # (or never valid) -- possible reuse of a stolen/stale token.
        raise Unauthorized("Invalid or expired token")

    return await issue_tokens(db, user)


async def logout(db: AsyncSession, user: User) -> None:
    user.current_refresh_jti = None
    await db.commit()
