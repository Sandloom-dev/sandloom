"""``/docs`` and ``/redoc`` outside production carry the Sandloom brand.

The address ``dev run`` prints lists the API docs; outside production the
kernel serves them itself with the caracal in the tab, a Sandloom bar and the
brand colours. Production keeps FastAPI's own pages: the API docs of a
deployed app belong to that app, not to the framework.
"""

from __future__ import annotations

import re
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
    assert 'class="sl-bar"' in page and "shop</span>" in page
    assert ">Start page<" not in page  # the bar links the docs, not the start page
    assert 'href="data:image/svg+xml,' in page  # the caracal in the tab
    assert '<a href="/docs" class="" aria-current="page">' in page
    assert 'href="/docs/assets/api_docs.css"' in page
    # The theme switch: system, light, dark, kept in this browser.
    assert 'class="sl-theme"' in page
    assert 'src="/docs/assets/api_docs.js"' in page
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
    assert 'class="sl-bar"' in page and 'class="sl-theme"' in page
    # Drawn by script in either theme, so the switch can redraw it.
    assert "<redoc " not in page and '<div id="redoc"></div>' in page
    assert "#8b6841" in page and "#d8b892" in page  # the light and the dark accent
    assert "Redoc.init" in page and "sl-theme" in page


@pytest.mark.asyncio
@pytest.mark.usefixtures("docs_on")
async def test_the_stylesheet_and_fonts_are_served_by_the_app() -> None:
    """No font service and no network for the look: the files ship in the
    wheel next to their licenses."""
    app = app_factory._new_application("shop")
    script = await _get(app, "/docs/assets/api_docs.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "localStorage" in script.text and "dark-mode" in script.text
    css = await _get(app, "/docs/assets/api_docs.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    for font in re.findall(r'url\("/docs/assets/([^"]+)"\)', css.text):
        response = await _get(app, f"/docs/assets/{font}")
        assert response.status_code == 200, font
        assert response.headers["content-type"] == "font/woff2"
    assert "https://" not in css.text
    for name in (
        "README.md",
        "Manrope-OFL.txt",
        "../app_factory/__init__.py",
        "nope.css",
    ):
        assert (await _get(app, f"/docs/assets/{name}")).status_code == 404, name
    licenses = app_factory._API_DOCS
    assert (licenses / "Manrope-OFL.txt").is_file()
    assert (licenses / "Hack-LICENSE.md").is_file()


@pytest.mark.asyncio
@pytest.mark.usefixtures("docs_on")
async def test_production_keeps_fastapis_own_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    app = app_factory._new_application("shop")
    page = (await _get(app, "/docs")).text
    assert "SwaggerUIBundle" in page
    assert "sl-bar" not in page and "persistAuthorization" not in page
    assert (await _get(app, "/docs/assets/api_docs.css")).status_code == 404
