"""Architecture rule: business modules must not use provider escape hatches.

Capability providers expose a small, portable, opinionated surface
(``cache.get/set``, ``taskqueue.enqueue``, …). The ``raw_client()`` /
``raw_backend()`` / ``raw()`` escape hatches return the underlying engine
(Redis client, Celery app, …) and exist ONLY for framework/plugin authors. A
business module that reaches for them couples itself to one concrete backend —
exactly what the capability runtime prevents — and hands an AI agent the full,
confusing backend API instead of the small sanctioned one.

This test fails if any business module calls one of those methods.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    RAW_PROVIDER_METHODS as FORBIDDEN_METHODS,
)
from sandloom.core.archcheck.analyzers import (
    find_raw_provider_calls as find_raw_provider_calls,
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


assert FORBIDDEN_METHODS == {"raw_client", "raw_backend", "raw"}


@pytest.mark.parametrize("module_name", BUSINESS_MODULES)
def test_module_has_no_raw_provider_usage(module_name: str) -> None:
    module_path = SANDLOOM_ROOT / module_name
    offences: list[str] = []
    for py_file in module_path.glob("**/*.py"):
        if "tests" in py_file.parts:
            continue
        found = find_raw_provider_calls(py_file)
        if found:
            offences.append(f"\nIn {py_file}:\n" + "\n".join(found))

    if offences:
        pytest.fail(
            f"Provider escape-hatch used in module '{module_name}'. Use the "
            "portable capability surface (cache.get/set, taskqueue.enqueue, "
            "objectstore.put/get, …) — raw_client()/raw_backend()/raw() are for "
            "framework/plugin authors only and couple the module to one "
            "backend.\n" + "".join(offences),
            pytrace=False,
        )


def test_analyzer_detects_raw_call(tmp_path: Path) -> None:
    """Self-test: the analyzer must flag escape-hatch calls."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "async def f(c):\n"
        "    client = await c.raw_client()\n"
        "    app = await c.raw_backend()\n"
        "    x = c.raw()\n"
    )
    found = find_raw_provider_calls(sample)
    assert len(found) == 3, found


def test_analyzer_detects_reflective_raw_access() -> None:
    fixture = (
        Path(__file__).parent
        / "_fixtures"
        / "broken_module"
        / "reflective_handles.py.txt"
    )
    found = find_raw_provider_calls(fixture)
    joined = "\n".join(found)
    assert "'raw_client'" in joined and "'raw_backend'" in joined, found
    assert len(found) == 2, found
