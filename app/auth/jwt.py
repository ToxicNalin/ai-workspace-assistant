import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.config import get_settings
from app.exceptions import Unauthorized

TokenType = Literal["access", "refresh"]


def _encode(
    user_id: uuid.UUID, token_type: TokenType, expires_delta: timedelta, **claims: Any
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    return _encode(user_id, "access", timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(user_id: uuid.UUID, jti: uuid.UUID) -> str:
    settings = get_settings()
    return _encode(
        user_id,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
        jti=str(jti),
    )


def _decode(token: str, expected_type: TokenType) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid or expired token") from exc

    if payload.get("type") != expected_type:
        raise Unauthorized("Invalid or expired token")

    return payload


def decode_access_token(token: str) -> dict[str, Any]:
    return _decode(token, "access")


def decode_refresh_token(token: str) -> dict[str, Any]:
    return _decode(token, "refresh")
