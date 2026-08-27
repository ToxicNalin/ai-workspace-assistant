import uuid
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import REFRESH_COOKIE_NAME
from app.tests.factories import make_user, random_email


def replay(client: AsyncClient, refresh_token: str) -> None:
    """Put a specific refresh token back in the client's jar.

    On the client rather than on the request: httpx deprecated per-request
    cookies because whether they persist afterwards is ambiguous, and these
    tests care about exactly that.
    """
    client.cookies.set(REFRESH_COOKIE_NAME, refresh_token)


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


async def test_login_returns_an_access_token_and_a_csrf_token(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await make_user(db_session, email="login@example.com", password="correct-password")

    response = await client.post(
        "/auth/login", json={"email": "login@example.com", "password": "correct-password"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["csrf_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


async def test_login_puts_the_refresh_token_in_an_httponly_cookie_and_not_the_body(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """SPEC-v2 D19, and the half of it that is easy to get wrong.

    A refresh token echoed in the response body defeats the cookie entirely:
    script that can read one body can call /auth/refresh -- which the browser
    authenticates from the cookie by itself -- and read the next one.
    """
    await make_user(db_session, email="cookie@example.com", password="password123")

    response = await client.post(
        "/auth/login", json={"email": "cookie@example.com", "password": "password123"}
    )

    assert "refresh_token" not in response.json()
    assert client.cookies[REFRESH_COOKIE_NAME]

    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Path=/auth" in set_cookie


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
    old_cookie = client.cookies[REFRESH_COOKIE_NAME]
    old_csrf = login.json()["csrf_token"]

    first = await client.post("/auth/refresh", headers={"X-CSRF-Token": old_csrf})
    assert first.status_code == 200
    new_cookie = client.cookies[REFRESH_COOKIE_NAME]
    new_csrf = first.json()["csrf_token"]
    assert new_cookie != old_cookie
    assert new_csrf != old_csrf

    # The new pair works.
    second = await client.post("/auth/refresh", headers={"X-CSRF-Token": new_csrf})
    assert second.status_code == 200

    # The rotated-out cookie does not, even presented with the CSRF token it
    # was issued alongside. Reuse means the token was replayed, not renewed.
    replay(client, old_cookie)
    reused = await client.post("/auth/refresh", headers={"X-CSRF-Token": old_csrf})
    assert reused.status_code == 401


async def test_refresh_without_the_cookie_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/auth/refresh", headers={"X-CSRF-Token": "anything"})
    assert response.status_code == 401


async def test_refresh_with_the_cookie_alone_is_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The CSRF half of SPEC-v2 D19.

    A browser attaches the cookie whether or not the page that triggered the
    request is ours, so the cookie alone cannot authorise a rotation -- another
    site could otherwise log a user out at will. The header proves the caller
    could read the login response, which only an allowed origin can.
    """
    await make_user(db_session, email="csrf@example.com", password="password123")
    await client.post(
        "/auth/login", json={"email": "csrf@example.com", "password": "password123"}
    )

    assert client.cookies[REFRESH_COOKIE_NAME]
    no_header = await client.post("/auth/refresh")
    assert no_header.status_code == 401

    wrong_header = await client.post("/auth/refresh", headers={"X-CSRF-Token": "wrong"})
    assert wrong_header.status_code == 401
    # Same message as a bad cookie. Distinguishing them would confirm to a
    # cross-site caller that the session it is poking at is a live one.
    assert wrong_header.json()["detail"] == "Invalid or expired token"


async def test_refresh_rejects_expired_token(db_session: AsyncSession, client: AsyncClient) -> None:
    user = await make_user(db_session, email="expired@example.com", password="password123")
    settings = get_settings()

    now = datetime.now(UTC)
    expired_token = jwt.encode(
        {
            "sub": str(user.id),
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "csrf": "irrelevant",
            "iat": now - timedelta(days=2),
            "exp": now - timedelta(days=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    replay(client, expired_token)
    response = await client.post("/auth/refresh", headers={"X-CSRF-Token": "irrelevant"})

    assert response.status_code == 401


async def test_refresh_rejects_access_token(db_session: AsyncSession, client: AsyncClient) -> None:
    await make_user(db_session, email="wrongtype@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "wrongtype@example.com", "password": "password123"}
    )
    access_token = login.json()["access_token"]

    replay(client, access_token)
    response = await client.post(
        "/auth/refresh", headers={"X-CSRF-Token": login.json()["csrf_token"]}
    )

    assert response.status_code == 401


async def test_logout_invalidates_refresh_token(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await make_user(db_session, email="logout@example.com", password="password123")
    login = await client.post(
        "/auth/login", json={"email": "logout@example.com", "password": "password123"}
    )
    access_token = login.json()["access_token"]
    csrf_token = login.json()["csrf_token"]
    refresh_cookie = client.cookies[REFRESH_COOKIE_NAME]

    logout = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout.status_code == 204

    # The cookie is cleared, but the token is dead server-side regardless --
    # presenting the copy the browser was asked to discard still fails.
    assert not client.cookies.get(REFRESH_COOKIE_NAME)

    replay(client, refresh_cookie)
    refresh_after_logout = await client.post(
        "/auth/refresh", headers={"X-CSRF-Token": csrf_token}
    )
    assert refresh_after_logout.status_code == 401
