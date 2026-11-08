"""Run the kernel's tests with no bundled module importable (ADR-0012, KB-05).

ADR-0012's fitness test "kernel unit and architecture tests in a venv with no
module wheel": the ``kernel-without-batteries`` CI job builds a venv with the
``sandloom`` package alone, then this tool makes sure no battery is reachable
even from the checkout's ``src/`` (which pytest puts on ``sys.path``):

``python tools/ci/no_batteries.py install``
    copies this module into the RUNNING interpreter's ``site-packages`` with a
    ``.pth`` that activates it: :data:`BATTERIES` are hidden from every import,
    also in the subprocesses the tests start with that interpreter (the
    ``sandloom`` CLI, uvicorn). The checkout stays whole, so repository scans
    (ratchets, the source export) see what they see everywhere else; only
    importing a battery fails, as where no module wheel is installed.

``pytest -p tools.ci.no_batteries ...``
    refuses to run unless the hook is active, and deselects the tests listed
    in ``no_batteries_pending.json`` — tests that still need a battery and are
    not yet moved to a suite that installs one; each entry names what it waits
    for. The list only goes down: an entry whose test no longer exists fails
    the run, and a new kernel test that needs a battery fails the job instead
    of joining the list.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import sysconfig
from pathlib import Path
from typing import Any

#: The bundled module packages (identity, tenancy, billing, the admin console).
BATTERIES = frozenset(
    {"sandloom_identity", "sandloom_tenancy", "sandloom_billing", "sandloom_admin"}
)
HOOK = "_sandloom_no_batteries"
PENDING = Path(__file__).with_name("no_batteries_pending.json")


class _NoBatteriesFinder(importlib.machinery.PathFinder):
    """The standard path finder, blind to :data:`BATTERIES` and their subpackages."""

    @classmethod
    def find_spec(  # type: ignore[override]
        cls, fullname: str, path: Any = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        """No spec for a battery: ``find_spec`` answers None, an import fails."""
        if fullname.partition(".")[0] in BATTERIES:
            return None
        return super().find_spec(fullname, path, target)


def activate() -> None:
    """Put the battery-blind finder in place of the path finder (idempotent)."""
    sys.meta_path[:] = [
        _NoBatteriesFinder if finder is importlib.machinery.PathFinder else finder
        for finder in sys.meta_path
    ]
    for name in [m for m in sys.modules if m.partition(".")[0] in BATTERIES]:
        del sys.modules[name]


def install(site_packages: Path) -> list[Path]:
    """Copy this module into ``site_packages`` with a ``.pth`` that activates it."""
    module = site_packages / f"{HOOK}.py"
    module.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
    pth = site_packages / f"{HOOK}.pth"
    # A .pth line that starts with ``import`` runs at every interpreter start.
    pth.write_text(f"import {HOOK}; {HOOK}.activate()\n", encoding="utf-8")
    return [module, pth]


def importable_batteries() -> list[str]:
    """The batteries this interpreter could import (none under the hook)."""
    return sorted(name for name in BATTERIES if importlib.util.find_spec(name))


def load_pending(path: Path = PENDING) -> dict[str, str]:
    """``{test id without parameters: what it waits for}`` from the pending file.

    The file maps what a group waits for to its test ids; keys starting with
    ``_`` are notes.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        test: waits_for
        for waits_for, tests in data.items()
        if not waits_for.startswith("_")
        for test in tests
    }


def _base_id(nodeid: str) -> str:
    """``nodeid`` without its parametrization suffix."""
    return nodeid.split("[", 1)[0]


def pytest_configure(config: Any) -> None:
    """Refuse a run in which a battery is importable."""
    import pytest

    found = importable_batteries()
    if found:
        raise pytest.UsageError(
            f"tools.ci.no_batteries: {', '.join(found)} importable; run "
            "`python tools/ci/no_batteries.py install` with this interpreter first"
        )


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Deselect the pending tests; fail on an entry that names no collected test.

    Only entries in a collected file are checked, so a run of part of the
    suites does not report the rest as stale.
    """
    import pytest

    pending = load_pending()
    keep: list[Any] = []
    dropped: list[Any] = []
    seen: set[str] = set()
    for item in items:
        base = _base_id(item.nodeid)
        if base in pending:
            seen.add(base)
            dropped.append(item)
        else:
            keep.append(item)
    files = {item.nodeid.split("::", 1)[0] for item in items}
    stale = sorted(
        test for test in pending if test not in seen and test.split("::", 1)[0] in files
    )
    if stale:
        raise pytest.UsageError(
            "tools/ci/no_batteries_pending.json names tests that no longer exist "
            "(moved, renamed or fixed): remove them:\n  " + "\n  ".join(stale)
        )
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


def main(argv: list[str]) -> int:
    """``install`` the hook into this interpreter, or ``check`` none is importable."""
    if argv[1:] == ["install"]:
        written = install(Path(sysconfig.get_paths()["purelib"]))
        print("installed:", ", ".join(str(p) for p in written))
        return 0
    if argv[1:] == ["check"]:
        found = importable_batteries()
        print("importable batteries:", ", ".join(found) or "none")
        return 1 if found else 0
    print("usage: no_batteries.py install|check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
