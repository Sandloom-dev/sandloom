"""``/docs`` and ``/redoc`` outside production carry the Sandloom brand.

The address ``dev run`` prints lists the API docs; outside production the
kernel serves them itself with the caracal in the tab, a Sandloom bar and the
brand colours. Production keeps FastAPI's own pages: the API docs of a
deployed app belong to that app, not to the framework.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sandloom.core import app_factory
from sandloom.core.config.settings import settings


async def _get(app: FastAPI, path: str) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as client:
        return await client.get(path)


@pytest.fixture
def docs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "docs_exposed", property(lambda self: True))


@pytest.mark.asyncio
@pytest.mark.usefixtures("docs_on")
async def test_swagger_ui_carries_the_bar_the_favicon_and_the_colours() -> None:
    app = app_factory._new_application("shop")
    response = await _get(app, "/docs")
    assert response.status_code == 200
    page = response.text
    assert "<title>shop API</title>" in page
    assert 'class="sandloom-bar"' in page and "<span>shop</span>" in page
    assert 'href="data:image/svg+xml,' in page  # the caracal in the tab
    assert "#8b6841" in page  # the brand colour
    assert "url: '/openapi.json'" in page
    assert '"persistAuthorization": true' in page
    # Still FastAPI's Swagger UI underneath, with its OAuth2 redirect.
    assert "SwaggerUIBundle" in page
    redirect = await _get(app, "/docs/oauth2-redirect")
    assert redirect.status_code == 200
    # Not an API route: no route policy, not in the schema.
    schema = (await _get(app, "/openapi.json")).json()
    assert "/docs" not in schema.get("paths", {})


@pytest.mark.asyncio
@pytest.mark.usefixtures("docs_on")
async def test_redoc_carries_the_favicon_and_no_google_fonts() -> None:
    app = app_factory._new_application("shop")
    page = (await _get(app, "/redoc")).text
    assert "<title>shop API</title>" in page
    assert 'href="data:image/svg+xml,' in page
    assert "fonts.googleapis.com" not in page
    assert "<redoc theme=" in page and "#8b6841" in page


@pytest.mark.asyncio
@pytest.mark.usefixtures("docs_on")
async def test_production_keeps_fastapis_own_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    app = app_factory._new_application("shop")
    page = (await _get(app, "/docs")).text
    assert "SwaggerUIBundle" in page
    assert "sandloom-bar" not in page and "persistAuthorization" not in page
