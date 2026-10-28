"""Schema-registry persistence at startup (GLM review finding 12).

A broken ``SCHEMA_REGISTRY_PATH`` file (unreadable / corrupt / not a list)
used to be logged and skipped by a non-critical startup task: in production
the process served traffic without its persisted schemas. In production the
load is now strict and its startup task critical; elsewhere it stays
best-effort. A MISSING file is a first boot, never an error.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import app_factory
from sandloom.core.schema.registry import InMemorySchemaRegistry


def _registry(path: Path) -> InMemorySchemaRegistry:
    reg = InMemorySchemaRegistry()
    reg.configure_persistence(str(path))
    return reg


@pytest.mark.parametrize("content", ["{not json", '{"a": 1}'])
def test_strict_load_refuses_a_broken_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "schemas.json"
    path.write_text(content)
    with pytest.raises(RuntimeError, match="SCHEMA_REGISTRY_PATH"):
        _registry(path).load_from_disk(strict=True)
    _registry(path).load_from_disk()  # best-effort: logged, no raise


def test_missing_file_is_a_first_boot_even_when_strict(tmp_path: Path) -> None:
    _registry(tmp_path / "absent.json").load_from_disk(strict=True)


def test_persistence_status_accessors(tmp_path: Path) -> None:
    # /contracts/persistence read the private _persistence_path /
    # _loaded_from_disk; it now uses these properties.
    path = tmp_path / "schemas.json"
    path.write_text("[]")
    reg = _registry(path)
    assert (reg.persistence_path, reg.loaded_from_disk) == (str(path), False)
    reg.load_from_disk()
    assert reg.loaded_from_disk is True


class _CaptureCore:
    def __init__(self) -> None:
        self.startup: dict[str, tuple[Any, bool]] = {}

    def add_startup_task(
        self, fn: Any, *, critical: bool = False, phase: str = "modules"
    ) -> None:
        self.startup[fn.__name__] = (fn, critical)

    def add_shutdown_task(self, fn: Any) -> None:
        pass


@pytest.mark.parametrize("is_prod", [True, False])
async def test_startup_task_is_critical_and_strict_in_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_prod: bool
) -> None:
    path = tmp_path / "schemas.json"
    path.write_text("{not json")
    monkeypatch.setattr(
        app_factory,
        "settings",
        SimpleNamespace(
            is_prod=is_prod,
            schema_registry_path=str(path),
            production_profile="standard",
        ),
    )
    core = _CaptureCore()
    app_factory._register_infra_lifecycle(
        core,  # type: ignore[arg-type]
        InMemorySchemaRegistry(),
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    task, critical = core.startup["infra_load_schema_registry"]
    assert critical is is_prod
    if is_prod:
        with pytest.raises(RuntimeError, match="SCHEMA_REGISTRY_PATH"):
            await task()
    else:
        await task()
