import uuid
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.tests.factories import make_user, random_email


async def test_register_returns_user(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "new@example.com", "password": "password123", "name": "New User"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["name"] == "New User"
    assert "password" not in body
    assert "password_hash" not in body


async def test_register_normalises_email_case(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "Mixed.Case@Example.com", "password": "password123", "name": "X"},
    )

    assert response.status_code == 201
    assert response.json()["email"] == "mixed.case@example.com"


async def test_register_duplicate_email_is_rejected(client: AsyncClient) -> None:
    payload = {"email": "dupe@example.com", "password": "password123", "name": "First"}
    first = await client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post(
        "/auth/register",
        json={"email": "DUPE@example.com", "password": "different99", "name": "Second"},
    )
    assert second.status_code == 409


async def test_register_short_password_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "short@example.com", "password": "short", "name": "X"},
    )
    assert response.status_code == 422


async def test_login_returns_tokens(db_session: AsyncSession, client: AsyncClient) -> None:
    await make_user(db_session, email="login@example.com", password="correct-password")

    response = await client.post(
        "/auth/login", json={"email": "login@example.com", "password": "correct-password"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["token_type"] == "bearer"


async def test_login_wrong_password_is_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await make_user(db_session, email="wrongpw@example.com", password="correct-password")

    response = await client.post(
        "/auth/login", json={"email": "wrongpw@example.com", "password": "incorrect-password"}
    )

    assert response.status_code == 401


async def test_login_unknown_email_gives_same_error_as_wrong_password(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/auth/login", json={"email": random_email(), "password": "whatever123"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


async def test_me_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_returns_current_user(db_session: AsyncSession, client: AsyncClient) -> None:
    await make_user(db_session, email="me@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "me@example.com", "password": "password123"}
    )
    access_token = login.json()["access_token"]

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )

    assert response.status_code == 200
    assert response.json()["email"] == "me@example.com"


async def test_refresh_rotates_token_and_invalidates_the_old_one(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await make_user(db_session, email="rotate@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "rotate@example.com", "password": "password123"}
    )
    old_refresh = login.json()["refresh_token"]

    first_refresh = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert first_refresh.status_code == 200
    new_refresh = first_refresh.json()["refresh_token"]
    assert new_refresh != old_refresh

    # The old refresh token was rotated out -- reusing it must now fail.
    reuse_attempt = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_attempt.status_code == 401

    # The new one still works.
    second_refresh = await client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert second_refresh.status_code == 200


async def test_refresh_rejects_expired_token(db_session: AsyncSession, client: AsyncClient) -> None:
    user = await make_user(db_session, email="expired@example.com", password="password123")
    settings = get_settings()

    now = datetime.now(UTC)
    expired_token = jwt.encode(
        {
            "sub": str(user.id),
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "iat": now - timedelta(days=2),
            "exp": now - timedelta(days=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.post("/auth/refresh", json={"refresh_token": expired_token})

    assert response.status_code == 401


async def test_refresh_rejects_access_token(db_session: AsyncSession, client: AsyncClient) -> None:
    await make_user(db_session, email="wrongtype@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "wrongtype@example.com", "password": "password123"}
    )
    access_token = login.json()["access_token"]

    response = await client.post("/auth/refresh", json={"refresh_token": access_token})

    assert response.status_code == 401


async def test_logout_invalidates_refresh_token(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await make_user(db_session, email="logout@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "logout@example.com", "password": "password123"}
    )
    access_token = login.json()["access_token"]
    refresh_token = login.json()["refresh_token"]

    logout = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout.status_code == 204

    refresh_after_logout = await client.post(
        "/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert refresh_after_logout.status_code == 401
