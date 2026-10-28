"""Architecture rule: business modules must not own the transaction boundary.

``DbSession`` is a UoW-governed session: the kernel commits on success and rolls
back on ANY exception, exactly once per request. A service that calls
``db.commit()`` / ``db.rollback()`` itself defeats rollback-on-exception and —
critically — flushes the transactional outbox prematurely, dispatching events
for a request that may still fail. Services materialize state with
``await db.flush()`` and undo partial writes with ``async with db.begin_nested()``;
the UoW owns ``commit`` / ``rollback``.

This test fails if any business module calls ``.commit()`` or ``.rollback()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    find_uow_boundary_breaks as find_uow_boundary_breaks,
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
def test_module_does_not_break_uow_boundary(module_name: str) -> None:
    module_path = SANDLOOM_ROOT / module_name
    offences: list[str] = []
    for py_file in module_path.glob("**/*.py"):
        if "tests" in py_file.parts:
            continue
        found = find_uow_boundary_breaks(py_file)
        if found:
            offences.append(f"\nIn {py_file}:\n" + "\n".join(found))

    if offences:
        pytest.fail(
            f"Transaction-control call in module '{module_name}'. The UoW owns "
            "commit/rollback (one transaction per request, rollback on any "
            "exception). Use `await db.flush()` to materialize IDs and "
            "`async with db.begin_nested()` to undo partial writes; never call "
            "db.commit()/db.rollback() — it flushes the outbox early and "
            "defeats rollback-on-exception.\n" + "".join(offences),
            pytrace=False,
        )


def test_analyzer_detects_boundary_break(tmp_path: Path) -> None:
    """Self-test: the analyzer must flag commit()/rollback() calls."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "async def f(db):\n    await db.commit()\n    await db.rollback()\n"
    )
    found = find_uow_boundary_breaks(sample)
    assert len(found) == 2, found
