"""Pages the kernel serves outside the API: the start page, the favicon and
the branded ``/docs`` and ``/redoc``."""

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

import sandloom.core.route_policy as _route_policy
from sandloom.core.config.settings import settings

_START_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 3rem auto;
  padding: 0 1rem; color: #1f1d1a; background: #faf8f5; }}
@media (prefers-color-scheme: dark) {{ body {{ color: #ece8e1; background: #1c1a17; }}
  a {{ color: #e0b07a; }} }}
code {{ font-size: 0.9em; }}
</style></head><body>
<h1>{title}</h1>
<p>The app is running. This page is served outside production only.</p>
<ul>
{links}
</ul>
<p>Next:</p>
<ul>
{commands}
</ul>
</body></html>
"""


#: The caracal of ``docs/assets/sandloom-mark.svg`` in the brand colours
#: (``website/assets/brand.css``: #8b6841, #d8b892 on a dark scheme).
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80">'
    "<style>path{fill:#8b6841}"
    "@media (prefers-color-scheme:dark){path{fill:#d8b892}}</style>"
    '<path fill-rule="evenodd" d="M17 3 22.5 7.5 27 28Q40 22 53 28L57.5 7.5 63 3 64 '
    "29 70 42.5 64 59 52 69 40 74 28 69 16 59 10 42.5 16 29ZM19.5 17 23.5 31.5 "
    "18 34ZM60.5 17 56.5 31.5 62 34ZM20.5 41.8C25.8 40.3 31.3 41.4 34 45.6C32.4 "
    "48.4 29.5 49 26.6 47.7C23.4 46.3 21.4 44.4 20.5 41.8ZM59.5 41.8C54.2 40.3 "
    "48.7 41.4 46 45.6C47.6 48.4 50.5 49 53.4 47.7C56.6 46.3 58.6 44.4 59.5 "
    "41.8ZM34.5 54.5Q40 52.5 45.5 54.5L42 58.5 41 59V61.5L45.5 63.2 44.9 64.6 40 "
    '62.8 35.1 64.6 34.5 63.2 39 61.5V59L38 58.5Z"/></svg>'
)


#: ``/docs`` and ``/redoc`` outside production look like sandloom.dev: the
#: stylesheet and the brand fonts ship in ``sandloom/core/http/api_docs`` and
#: are served from ``/docs/assets/`` (never a font service).
_API_DOCS = Path(__file__).parent.parent / "http" / "api_docs"
_API_DOCS_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".woff2": "font/woff2",
}

#: ReDoc's two themes: the site's fonts, accent and surfaces. ReDoc has no dark
#: mode of its own: api_docs.js redraws it with the other theme on a switch.
_REDOC_CODE = {"fontFamily": "Hack Nerd Font Mono, ui-monospace, monospace"}
_REDOC_FONTS: dict[str, Any] = {
    "fontFamily": "Manrope, system-ui, sans-serif",
    "headings": {"fontFamily": "Manrope, system-ui, sans-serif", "fontWeight": "700"},
    "code": _REDOC_CODE,
}
_REDOC_THEMES = {
    "light": {
        "colors": {
            "primary": {"main": "#8b6841"},
            "text": {"primary": "#24272a", "secondary": "#686c6f"},
            "border": {"dark": "#dedfda", "light": "#eeede8"},
        },
        "typography": _REDOC_FONTS,
        "sidebar": {"backgroundColor": "#f7f6f2", "textColor": "#24272a"},
        "rightPanel": {"backgroundColor": "#24272a"},
    },
    "dark": {
        "colors": {
            "primary": {"main": "#d8b892"},
            "text": {"primary": "#edeee9", "secondary": "#a5aaa8"},
            "border": {"dark": "#303337", "light": "#222528"},
        },
        "typography": {
            **_REDOC_FONTS,
            "code": {
                **_REDOC_CODE,
                "color": "#d8b892",
                "backgroundColor": "#222528",
            },
        },
        "schema": {"nestedBackground": "#191b1d", "typeNameColor": "#a5aaa8"},
        "sidebar": {"backgroundColor": "#191b1d", "textColor": "#edeee9"},
        "rightPanel": {"backgroundColor": "#0b0c0d"},
    },
}

#: The switch in the header: system, light, dark (api_docs.css shows one icon).
_THEME_SWITCH = (
    '<button class="sl-theme" type="button" aria-label="Theme">'
    '<svg class="sl-i-system" viewBox="0 0 24 24"><rect x="3" y="4" width="18" '
    'height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg>'
    '<svg class="sl-i-light" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    '<svg class="sl-i-dark" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4 '
    '8 8 0 1 0 20 14.5Z"/></svg></button>'
)


def _docs_favicon() -> str:
    return "data:image/svg+xml," + quote(_FAVICON_SVG)


def _docs_bar(application: FastAPI, current: str) -> str:
    """The site's header with the app's name: the console (when the app
    serves it), Swagger UI or ReDoc, and the Sandloom docs."""
    mark = re.sub(r"<style>.*?</style>", "", _FAVICON_SVG)
    console = any(
        str(getattr(route, "path", "")).startswith("/admin")
        for route in application.routes
    )
    links = []
    if console:
        links.append(("/admin/", "Console", ""))
    links += [("/docs", "Swagger UI", ""), ("/redoc", "ReDoc", "")]
    nav = "".join(
        f'<a href="{href}" class="{cls}"'
        + (' aria-current="page"' if href == current else "")
        + f">{label}</a>"
        for href, label, cls in links
    )
    return (
        f'<header class="sl-bar"><span class="sl-brand">{mark}'
        f"{html.escape(application.title)}</span>"
        '<span class="sl-pill">API</span>'
        f"<nav>{nav}"
        '<a class="sl-out sl-wide" href="https://sandloom.dev/docs/" target="_blank"'
        f' rel="noopener">Sandloom docs</a></nav>{_THEME_SWITCH}</header>'
    )


def _docs_head() -> str:
    return (
        '<link rel="stylesheet" href="/docs/assets/api_docs.css">'
        '<script src="/docs/assets/api_docs.js"></script>'
    )


def _install_branded_docs(application: FastAPI) -> None:
    """Swagger UI at ``/docs`` and ReDoc at ``/redoc``, as FastAPI serves them
    (same paths, the OAuth2 redirect, ``root_path``), in the sandloom.dev look:
    the site's header, fonts, colours and dark mode. Plain Starlette routes,
    like FastAPI's own: no route policy, not in the schema."""
    openapi_url = application.openapi_url or "/openapi.json"
    redirect_url = application.swagger_ui_oauth2_redirect_url

    async def swagger_ui(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        page = get_swagger_ui_html(
            openapi_url=root + openapi_url,
            title=f"{application.title} API",
            swagger_favicon_url=_docs_favicon(),
            oauth2_redirect_url=root + redirect_url if redirect_url else None,
            init_oauth=application.swagger_ui_init_oauth,
            swagger_ui_parameters={
                "displayRequestDuration": True,
                "filter": True,
                "persistAuthorization": True,  # outside production only
                "syntaxHighlight": {"theme": "tomorrow-night"},
                **(application.swagger_ui_parameters or {}),
            },
        )
        body = bytes(page.body).decode()
        body = body.replace("</head>", _docs_head() + "\n</head>", 1)
        body = body.replace("<body>", "<body>\n" + _docs_bar(application, "/docs"), 1)
        return HTMLResponse(body)

    async def swagger_ui_redirect(request: Request) -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    async def redoc(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        page = get_redoc_html(
            openapi_url=root + openapi_url,
            title=f"{application.title} API",
            redoc_favicon_url=_docs_favicon(),
            with_google_fonts=False,
        )
        body = bytes(page.body).decode()
        body = body.replace("</head>", _docs_head() + "\n</head>", 1)
        body = body.replace("<body>", "<body>\n" + _docs_bar(application, "/redoc"), 1)
        # ReDoc drawn by script, so the switch can redraw it in the other theme.
        tag = re.search(r"<redoc [^>]*></redoc>", body)
        if tag is not None:
            spec = json.dumps(root + openapi_url)
            themes = json.dumps(_REDOC_THEMES)
            body = body.replace(
                tag.group(0),
                '<div id="redoc"></div><script>window.slRedoc = '
                f"{{spec: {spec}, themes: {themes}}};</script>",
                1,
            )
            body = body.replace(
                "</body>",
                "<script>(() => { const r = window.slRedoc; const draw = () => "
                "Redoc.init(r.spec, {scrollYOffset: '.sl-bar', theme: "
                "window.slTheme.dark() ? r.themes.dark : r.themes.light}, "
                "document.getElementById('redoc')); draw(); "
                "document.addEventListener('sl-theme', draw); })();</script>\n</body>",
                1,
            )
        return HTMLResponse(body)

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        file = _API_DOCS / name
        kind = _API_DOCS_TYPES.get(file.suffix)
        if kind is None or "/" in name or not file.is_file():
            return Response(status_code=404)
        # The stylesheet changes with the framework: revalidated each time.
        cache = "no-cache" if file.suffix == ".css" else "public, max-age=86400"
        return Response(
            file.read_bytes(), media_type=kind, headers={"Cache-Control": cache}
        )

    application.add_route("/docs", swagger_ui, include_in_schema=False)
    application.add_route("/docs/assets/{name}", asset, include_in_schema=False)
    if redirect_url:
        application.add_route(
            redirect_url, swagger_ui_redirect, include_in_schema=False
        )
    application.add_route("/redoc", redoc, include_in_schema=False)


def _install_favicon(application: FastAPI) -> None:
    """``GET /favicon.ico`` outside production, unless the app serves it:
    the caracal, so a browser on the start page, the docs or the console
    stops logging a 404. Production leaves it to the app."""
    if settings.is_prod:
        return
    for route in application.routes:
        if getattr(route, "path", None) == "/favicon.ico":
            return

    @application.get(
        "/favicon.ico",
        include_in_schema=False,
        dependencies=[
            _route_policy.public(reason="development favicon (never in production)")
        ],
    )
    async def favicon() -> Response:
        return Response(
            _FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


def _install_start_page(application: FastAPI) -> None:
    """``GET /`` outside production, unless the app routes ``/`` itself: the
    address ``sandloom dev run`` prints answers with links to the console,
    the API docs and ``/health`` instead of a 404. Production leaves ``/``
    unrouted."""
    if settings.is_prod:
        return
    for route in application.routes:
        if getattr(route, "path", None) == "/" and "GET" in (
            getattr(route, "methods", None) or ()
        ):
            return
    # The console is whatever serves /admin/ (the kernel names no module).
    console = any(
        str(getattr(route, "path", "")).startswith("/admin")
        for route in application.routes
    )
    links = []
    if console:
        links.append('<li><a href="/admin/">The admin console</a> (/admin/)</li>')
    if settings.docs_exposed:
        links.append('<li><a href="/docs">The API docs</a> (/docs)</li>')
    links.append('<li><a href="/health">Health</a> (/health)</li>')
    commands = []
    if console:
        commands.append(
            "<li><code>uv run sandloom admin list</code> (the administrators; "
            "<code>uv run sandloom admin create --email &lt;address&gt;</code> "
            "adds one)</li>"
        )
    commands += [
        "<li><code>uv run sandloom add module notes</code> (a module)</li>",
        "<li><code>uv run sandloom test</code> (tests and every gate)</li>",
    ]
    page = _START_PAGE.format(
        title=html.escape(application.title),
        links="\n".join(links),
        commands="\n".join(commands),
    )

    @application.get(
        "/",
        include_in_schema=False,
        dependencies=[
            _route_policy.public(reason="development start page (never in production)")
        ],
    )
    async def start_page() -> HTMLResponse:
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})
