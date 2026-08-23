import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.exceptions import AppError, Conflict, Forbidden, NotFound, RateLimited
from app.middleware.errors import app_error_handler


def _build_test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.add_exception_handler(AppError, app_error_handler)

    @test_app.get("/not-found")
    async def raise_not_found() -> None:
        raise NotFound

    @test_app.get("/forbidden")
    async def raise_forbidden() -> None:
        raise Forbidden

    @test_app.get("/conflict")
    async def raise_conflict() -> None:
        raise Conflict("already exists")

    @test_app.get("/rate-limited")
    async def raise_rate_limited() -> None:
        raise RateLimited

    return test_app


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_detail"),
    [
        ("/not-found", 404, "Not found"),
        ("/forbidden", 403, "Forbidden"),
        ("/conflict", 409, "already exists"),
        ("/rate-limited", 429, "Rate limited"),
    ],
)
async def test_app_error_maps_to_expected_response(
    path: str, expected_status: int, expected_detail: str
) -> None:
    transport = ASGITransport(app=_build_test_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
