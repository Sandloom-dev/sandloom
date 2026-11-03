"""``GET /`` outside production: the address ``dev run`` prints never 404s.

A generated app has no ``/`` route. Outside production the kernel answers it
with a small page: the app, links to the console (when installed), the API
docs (when served), ``/health``, and the next commands. Production leaves
``/`` to the app.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core import app_factory
from sandloom.core.config.settings import settings
from sandloom.core.route_policy import resolve_route_policy


def _app(*, console: bool) -> FastAPI:
    app = FastAPI(title="shop")
    if console:

        @app.get("/admin/{path:path}")
        async def files(path: str) -> dict[str, str]:
            return {}

    return app


async def _get(app: FastAPI, path: str = "/") -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_the_start_page_links_the_console_docs_and_health() -> None:
    app = _app(console=True)
    app_factory._install_start_page(app)
    response = await _get(app)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert 'href="/admin/"' in page and 'href="/health"' in page
    assert ('href="/docs"' in page) is settings.docs_exposed
    assert "uv run sandloom admin create" in page
    assert "<script" not in page


@pytest.mark.asyncio
async def test_without_the_console_there_is_no_console_link() -> None:
    app = _app(console=False)
    app_factory._install_start_page(app)
    response = await _get(app)
    assert response.status_code == 200
    assert "/admin/" not in response.text


@pytest.mark.asyncio
async def test_an_apps_own_root_route_wins() -> None:
    app = FastAPI(title="shop")

    @app.get("/")
    async def mine() -> dict[str, str]:
        return {"app": "mine"}

    app_factory._install_start_page(app)
    assert (await _get(app)).json() == {"app": "mine"}


@pytest.mark.asyncio
async def test_production_leaves_the_root_unrouted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    app = FastAPI(title="shop")
    app_factory._install_start_page(app)
    assert (await _get(app)).status_code == 404


def test_the_composed_app_serves_the_start_page_as_a_public_read() -> None:
    from sandloom.main import app

    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/"]
    policy = resolve_route_policy(route, "GET")
    assert policy.auth == "public" and policy.public is not None


@pytest.mark.asyncio
async def test_the_favicon_is_the_caracal_outside_production() -> None:
    app = FastAPI(title="shop")
    app_factory._install_favicon(app)
    response = await _get(app, "/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    mark = Path(__file__).resolve().parents[4] / "docs" / "assets" / "sandloom-mark.svg"
    if mark.is_file():  # the docs ship with the repository, not the wheel
        path = re.search(r' d="([^"]+)"', mark.read_text()).group(1)  # type: ignore[union-attr]
        assert path in response.text
    assert "#8b6841" in response.text  # the brand colour
    app_factory._install_start_page(app)
    assert 'rel="icon" href="/favicon.ico"' in (await _get(app)).text


@pytest.mark.asyncio
async def test_production_serves_no_favicon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    app = FastAPI(title="shop")
    app_factory._install_favicon(app)
    assert (await _get(app, "/favicon.ico")).status_code == 404
