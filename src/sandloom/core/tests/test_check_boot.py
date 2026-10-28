"""R3.3: `sandloom check --boot` reports the startup refusals without serving."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.cli.check import boot_diagnostics

_PROBE = """
from uuid import UUID

from fastapi import APIRouter, Depends

from sandloom.core.di import idempotency_guard
from sandloom.core.module_api import define_module
from sandloom.shared.guards.auth import get_current_user_id

router = APIRouter(prefix="/api/v1/bootcheck")


@router.post("/x", dependencies=[Depends(idempotency_guard(scope="x"))])
async def x(user_id: UUID = Depends(get_current_user_id)) -> None: ...


register = define_module(__name__, router=router, events=None)
"""

_ROOT = """
from sandloom.core.app_factory import create_app

app = create_app()
"""


@pytest.fixture
def probe_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from sandloom.core.config.settings import settings

    (tmp_path / "bootcheck_probe").mkdir()
    (tmp_path / "bootcheck_probe" / "__init__.py").write_text(_PROBE)
    (tmp_path / "bootcheck_root.py").write_text(_ROOT)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "sandloom_modules", ["bootcheck_probe"])
    monkeypatch.setattr(settings, "module_required", ["bootcheck_probe"])
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    return "bootcheck_root"


def test_a_startup_refusal_is_rendered_with_its_code(probe_root: str) -> None:
    (problem,) = boot_diagnostics(probe_root)
    assert problem.startswith(
        "error[SANDLOOM-B002]: GuardOrderError: POST /api/v1/bootcheck/x"
    )
    assert "docs: sandloom explain SANDLOOM-B002" in problem


def test_the_framework_app_boots_clean() -> None:
    assert boot_diagnostics() == []
