"""Serve the console's files, its configuration and the application's manifest."""

import gzip
import json
import mimetypes
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from sandloom.sdk import module_router, public
from sandloom.sdk.infra import settings

MANIFEST = "admin-manifest.json"
PACKAGED = Path(__file__).parent / "console"
#: The UI loads nothing from elsewhere and is framed by nothing.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
NOT_GENERATED = "The admin manifest is missing: run `sandloom generate` and restart."
#: Precompressed copies the build writes beside each hashed asset, best first.
ENCODINGS = (("br", ".br"), ("gzip", ".gz"))
#: A JSON body worth compressing per request (the manifest is tens of KiB).
COMPRESS_FROM = 1024


def _enabled() -> None:
    enabled = settings.admin_console_enabled
    if enabled is None:
        enabled = settings.environment.value != "prod"
    if not enabled:
        raise HTTPException(404, "Not found")


router = module_router(
    prefix="/admin",
    tags=["admin-console"],
    dependencies=[
        public(reason="the console's static files; every API it calls checks access"),
        Depends(_enabled),
    ],
)


def _console() -> Path:
    custom = settings.admin_console_dist
    return Path(custom) if custom else PACKAGED


def _read_manifest() -> dict[str, Any] | None:
    """The first manifest found in the application's generated packages."""
    for package in (*settings.contract_dto_packages, "sandloom.shared"):
        try:
            file = resources.files(package) / MANIFEST
            if file.is_file():
                parsed: dict[str, Any] = json.loads(file.read_text(encoding="utf-8"))
                return parsed
        except ModuleNotFoundError:
            continue
    return None


def _problem(manifest: dict[str, Any], console: dict[str, Any]) -> str | None:
    """Why this console cannot show this manifest, in terms the operator can act on."""
    if manifest.get("version") not in console["manifest_versions"]:
        return (
            f"This console does not support admin manifest version "
            f"{manifest.get('version')!r}: align the sandloom and sandloom-admin "
            "versions, then run `sandloom generate`."
        )
    named = {
        widget
        for resource in manifest.get("resources", [])
        for field in ("show_widgets", "edit_widgets")
        for widget in resource.get(field, [])
    }
    unknown = sorted(named - set(console["widgets"]))
    if unknown:
        return (
            f"This console does not include {', '.join(unknown)}: upgrade "
            "sandloom-admin to match the installed modules."
        )
    return None


def _usable_manifest() -> dict[str, Any]:
    manifest = _read_manifest()
    if manifest is None:
        raise HTTPException(503, NOT_GENERATED)
    described = _console() / "console.json"
    if described.is_file():
        problem = _problem(manifest, json.loads(described.read_text(encoding="utf-8")))
        if problem:
            raise HTTPException(503, problem)
    return manifest


def _accepted(request: Request) -> set[str]:
    """The content codings the client accepts (``q=0`` refuses one)."""
    accepted = set()
    for part in request.headers.get("accept-encoding", "").split(","):
        coding, _, params = part.strip().partition(";")
        quality = params.strip().removeprefix("q=").strip()
        if coding and quality not in {"0", "0.0", "0.00", "0.000"}:
            accepted.add(coding.strip().lower())
    return accepted


def _json(payload: dict[str, Any], request: Request) -> Response:
    headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
    body = JSONResponse(payload).body
    if len(body) >= COMPRESS_FROM and "gzip" in _accepted(request):
        body = gzip.compress(body, compresslevel=6, mtime=0)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


def _revalidated(file: Path, request: Request, headers: dict[str, str]) -> Response:
    """A file without a hash in its name (the entry page, fonts, icons): the browser
    may keep it but asks each time, and an unchanged one costs a bodiless 304."""
    stat = file.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {**headers, "Cache-Control": "no-cache", "ETag": etag}
    candidates = request.headers.get("if-none-match", "").split(",")
    if etag in (candidate.strip().removeprefix("W/") for candidate in candidates):
        return Response(status_code=304, headers=headers)
    return FileResponse(file, headers=headers, stat_result=stat)


def _asset(file: Path, request: Request) -> FileResponse:
    """A hashed asset, from its precompressed copy when the client accepts one."""
    headers = {
        # The file name carries its content hash.
        "Cache-Control": "public, max-age=31536000, immutable",
        "Vary": "Accept-Encoding",
    }
    accepted = _accepted(request)
    for coding, suffix in ENCODINGS:
        encoded = file.with_name(file.name + suffix)
        if coding in accepted and encoded.is_file():
            media_type = (
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            )
            headers["Content-Encoding"] = coding
            return FileResponse(encoded, media_type=media_type, headers=headers)
    return FileResponse(file, headers=headers)


@router.get("", include_in_schema=False)
async def console_root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=308)


@router.get(f"/{MANIFEST}", include_in_schema=False)
async def admin_manifest(request: Request) -> Response:
    return _json(_usable_manifest(), request)


@router.get("/config.json", include_in_schema=False)
async def console_config(request: Request) -> Response:
    """Every section the manifest declares: composition already selected them.

    ``development`` (outside production only) lets the console keep its
    session across a reload of the tab (sessionStorage) and show the local
    sign-in hints; production keeps sessions in memory and says nothing."""
    modules = [m for m in _usable_manifest()["modules"] if m != "core"]
    if "identity" not in modules:
        raise HTTPException(503, "The admin console needs the identity module.")
    payload: dict[str, Any] = {"modules": modules}
    if settings.environment.value != "prod":
        payload["development"] = True
    return _json(payload, request)


@router.get("/{path:path}", include_in_schema=False)
async def console_file(path: str, request: Request) -> Response:
    root = _console().resolve()
    file = (root / path).resolve()
    if file != root and root in file.parents and file.is_file():
        if "assets" in file.relative_to(root).parts:
            return _asset(file, request)
        return _revalidated(file, request, {})
    # A deep link is the browser's route. A missing file must stay a 404, or a
    # stale script tag would receive HTML.
    if "." in path.rsplit("/", 1)[-1]:
        raise HTTPException(404, "Not found")
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(503, "The admin console files are missing.")
    return _revalidated(index, request, {"Content-Security-Policy": CSP})
