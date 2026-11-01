"""Serve the console's files, its configuration and the application's manifest."""

import json
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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


def _json(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("", include_in_schema=False)
async def console_root() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=308)


@router.get(f"/{MANIFEST}", include_in_schema=False)
async def admin_manifest() -> JSONResponse:
    return _json(_usable_manifest())


@router.get("/config.json", include_in_schema=False)
async def console_config() -> JSONResponse:
    """Every section the manifest declares: composition already selected them."""
    modules = [m for m in _usable_manifest()["modules"] if m != "core"]
    if "identity" not in modules:
        raise HTTPException(503, "The admin console needs the identity module.")
    return _json({"modules": modules})


@router.get("/{path:path}", include_in_schema=False)
async def console_file(path: str) -> FileResponse:
    root = _console().resolve()
    file = (root / path).resolve()
    if file != root and root in file.parents and file.is_file():
        if "assets" in file.relative_to(root).parts:
            # The file name carries its content hash.
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-store"
        return FileResponse(file, headers={"Cache-Control": cache})
    # A deep link is the browser's route. A missing file must stay a 404, or a
    # stale script tag would receive HTML.
    if "." in path.rsplit("/", 1)[-1]:
        raise HTTPException(404, "Not found")
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(503, "The admin console files are missing.")
    return FileResponse(
        index,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP},
    )
