"""Architecture rule: business modules never reach the kernel's raw handles.

At runtime a module gets the read-only ``CoreRuntime`` view (``get_core``) and
at registration the ``ModuleAPI``; neither exposes the event bus, the
infrastructure registry or the service registry. This rule closes the
remaining bypass — reaching those handles by attribute on whatever object a
module holds (``core.event_bus``, ``core.infra``, ``core.service_registry``,
``request.app.state.core`` …).

The analyzer is the ``kernel-handles`` rule of :mod:`sandloom.core.archcheck`
(shared with ``sandloom check``); provider escape hatches (``.raw_client()``) are
the ``raw-provider`` rule (``test_no_raw_provider_usage``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    find_kernel_handle_access,
    find_kernel_handle_access_in_source,
)

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"

BUSINESS_MODULES = [
    d.name
    for d in SANDLOOM_ROOT.iterdir()
    if (
        d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name not in ("core", "shared", "sdk")
    )
]


@pytest.mark.parametrize("module_name", BUSINESS_MODULES)
def test_module_has_no_kernel_handle_access(module_name: str) -> None:
    offences: list[str] = []
    for py_file in (SANDLOOM_ROOT / module_name).glob("**/*.py"):
        if "tests" in py_file.parts:
            continue
        found = find_kernel_handle_access(py_file)
        if found:
            offences.append(f"\nIn {py_file}:\n" + "\n".join(found))
    if offences:
        pytest.fail(
            f"Kernel handle accessed in module '{module_name}'. Publish with "
            "publish_event(...), consume with register_event_consumer(...), "
            "resolve infrastructure with core.cache()/database()/capability() "
            "and contracts with core.get_service(...) — never core.event_bus / "
            "core.infra / core.service_registry / app.state.core." + "".join(offences),
            pytrace=False,
        )


def test_analyzer_detects_kernel_handles() -> None:
    """Self-test: every forbidden shape is flagged (positive detection)."""
    sample = (
        "async def f(core, request):\n"
        "    await core.event_bus.publish('x.y.v1', {})\n"
        "    reg = core.infra\n"
        "    svc = core.service_registry.get('a.b.v1')\n"
        "    full = request.app.state.core\n"
    )
    found = find_kernel_handle_access_in_source(sample)
    assert any(".event_bus" in f for f in found), found
    assert any(".infra" in f for f in found), found
    assert any(".service_registry" in f for f in found), found
    assert any(".state.core" in f for f in found), found


def test_analyzer_allows_sanctioned_surface() -> None:
    sample = (
        "async def f(core):\n"
        "    svc = core.get_service('a.b.v1')\n"
        "    cache = core.cache()\n"
        "    await cache.get('k')\n"
    )
    assert find_kernel_handle_access_in_source(sample) == []


def test_analyzer_detects_reflective_kernel_handles() -> None:
    """``getattr(core, "event_bus")`` is ``core.event_bus`` by another name."""
    fixture = (
        Path(__file__).parent
        / "_fixtures"
        / "broken_module"
        / "reflective_handles.py.txt"
    )
    found = find_kernel_handle_access(fixture)
    joined = "\n".join(found)
    for shape in ("'event_bus'", "'infra'", "'service_registry'", "state.core"):
        assert shape in joined, (shape, found)
    assert "get_service" not in joined
    assert len(found) == 4, found
