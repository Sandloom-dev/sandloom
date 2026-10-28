"""R3.1: a test sees the exception behind a masked 500 (SANDLOOM-R006); prod does not."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.testing.server_errors import (
    UnhandledServerError,
    raise_server_errors,
)
from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        # (a MissingTenantError is the 400 of TenantScope(), G-TENANT-12)
        raise UnitOfWorkRequiredError("publish_event outside a unit")  # SANDLOOM-R002

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_the_client_reraises_the_original_exception_with_its_fix() -> None:
    async with _client(_app()) as http:
        with raise_server_errors(http):
            assert (await http.get("/ok")).status_code == 200
            with pytest.raises(UnhandledServerError) as excinfo:
                await http.get("/boom")
    message = str(excinfo.value)
    assert message.startswith("error[SANDLOOM-R002]: GET /boom answered 500")
    assert "UnitOfWorkRequiredError" in message
    assert type(excinfo.value.__cause__).__name__ == "UnitOfWorkRequiredError"


def test_a_message_naming_its_code_sets_the_headline() -> None:
    from sandloom.core.testing.server_errors import describe

    exc = ValueError(
        "Missing schema for 'x.y.v1'. fix: ... (sandloom explain SANDLOOM-R003)"
    )
    assert describe("POST", "/x", exc).startswith("error[SANDLOOM-R003]: POST /x")


async def test_the_response_stays_opaque_without_the_harness() -> None:
    async with _client(_app()) as http:
        response = await http.get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "detail": "Internal server error",
        "code": "internal_error",
    }
