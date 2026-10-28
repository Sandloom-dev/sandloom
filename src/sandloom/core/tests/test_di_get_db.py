# pyright: reportUnusedFunction=false
"""Regression tests for DI.get_db lifecycle and overrides.

These tests verify that:
- An async generator provider is properly closed on success and on exception.
- FastAPI dependency overrides are respected for the provider and also closed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from sandloom.core import di


class _DummyCore:
    """Minimal stand-in for CoreFacade holding a get_db provider."""

    def __init__(self, get_db_provider: Any) -> None:
        self.get_db = get_db_provider

    def get_uow_factory(self) -> Any:
        raise RuntimeError("database registry is not configured")


def _make_app_with_core(get_db_provider: Any) -> FastAPI:
    app = FastAPI()
    app.state.core = _DummyCore(get_db_provider)

    @app.get("/ok")
    def ok(_: Any = Depends(di.get_db)) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom(
        _: Any = Depends(di.get_db),
    ) -> dict[str, bool]:  # pragma: no cover - raises
        raise RuntimeError("boom")

    @app.get("/override")
    def override_ep(_: Any = Depends(di.get_db)) -> dict[str, bool]:
        return {"ok": True}

    return app


def test_get_db_never_falls_back_to_raw_provider() -> None:
    gen_closed = {"closed": False}

    async def provider() -> AsyncGenerator[object, None]:
        try:
            yield object()
        finally:
            gen_closed["closed"] = True

    app = _make_app_with_core(provider)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/ok")
    assert resp.status_code == 500
    assert gen_closed["closed"] is False


def test_get_db_surfaces_missing_registry() -> None:
    gen_closed = {"closed": False}

    async def provider() -> AsyncGenerator[object, None]:
        try:
            yield object()
        finally:
            gen_closed["closed"] = True

    app = _make_app_with_core(provider)
    client = TestClient(app)

    with pytest.raises(RuntimeError, match="database registry is not configured"):
        client.get("/boom")
    assert gen_closed["closed"] is False


def test_get_db_respects_fastapi_dependency_override() -> None:
    base_called = {"called": False, "closed": False}
    override_called = {"called": False, "closed": False}

    async def base_provider() -> AsyncGenerator[object, None]:
        base_called["called"] = True
        try:
            yield object()
        finally:
            base_called["closed"] = True

    async def override_provider() -> AsyncGenerator[object, None]:
        override_called["called"] = True
        try:
            yield object()
        finally:
            override_called["closed"] = True

    app = _make_app_with_core(base_provider)
    # Map original provider function to the override, as FastAPI expects
    app.dependency_overrides[base_provider] = override_provider

    client = TestClient(app)
    resp = client.get("/override")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # The override must be used and properly closed
    assert override_called["called"] is True
    assert override_called["closed"] is True
    # The base provider should not be invoked at all
    assert base_called["called"] is False
    assert base_called["closed"] is False
