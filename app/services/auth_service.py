import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import create_access_token, create_refresh_token, decode_refresh_token
from app.auth.password import hash_password, verify_password
from app.database.models.user import User
from app.exceptions import Conflict, Unauthorized
from app.schemas.auth import TokenPair


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


async def issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    jti = uuid.uuid4()
    user.current_refresh_jti = jti
    await db.commit()

    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id, jti),
    )


async def rotate_refresh_token(db: AsyncSession, refresh_token: str) -> TokenPair:
    payload = decode_refresh_token(refresh_token)

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
