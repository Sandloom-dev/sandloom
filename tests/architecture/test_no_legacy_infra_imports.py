"""Architecture rule: business modules must not import legacy infra globals.

All infrastructure is reached through the public surface — the operation-based
providers (``sandloom.core.di.cache``, ``CoreFacade.objectstore``/``taskqueue``/
``database``, ``DbSession``, ``publish_event``) — never the raw engine objects.
This ratchet forbids direct imports of the raw infra globals
(``get_cache``/``get_db``/``get_event_bus``) and of the core infra-internal
modules (the driver layer, the ORM models, the raw db/event_bus adapters).
No call site needs them, so the allowed set is empty: the bar only moves down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sandloom.core.archcheck.analyzers import (
    FORBIDDEN_BACKEND_ROOTS as FORBIDDEN_BACKEND_ROOTS,
)
from sandloom.core.archcheck.analyzers import (
    FORBIDDEN_INFRA_MODULES as FORBIDDEN_MODULES,
)
from sandloom.core.archcheck.analyzers import (
    find_legacy_infra_imports as find_legacy_infra_imports,
)

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"

BUSINESS_MODULES = [
    d.name
    for d in SANDLOOM_ROOT.iterdir()
    if (
        d.is_dir()
        and (d / "__init__.py").exists()
        and not d.name.startswith(("_", "."))
        and d.name not in ("core", "shared", "sdk", "driverkit")
    )
]

# The forbidden sets (infra-internal modules, legacy globals, backend SDKs)
# and the analyzer live in sandloom.core.archcheck.analyzers so `sandloom check` holds
# applications to exactly the same rule. Pin the sets here so a weakening is a
# visible test change.
assert {
    "sandloom.core._internal",
    "sandloom.core.eventing.event_bus",
    "sandloom.core.storage",
    "sandloom.core.drivers",
    "sandloom.core.sql_alchemy.models",
} <= FORBIDDEN_MODULES
assert {"motor", "pymongo", "neo4j", "temporalio", "kafka", "boto3"} <= (
    FORBIDDEN_BACKEND_ROOTS
)


@pytest.mark.parametrize("module_name", BUSINESS_MODULES)
def test_module_has_no_legacy_infra_imports(module_name: str) -> None:
    module_path = SANDLOOM_ROOT / module_name
    offences: list[str] = []
    for py_file in module_path.glob("**/*.py"):
        if "tests" in py_file.parts:
            continue
        found = find_legacy_infra_imports(py_file)
        if found:
            offences.append(f"\nIn {py_file}:\n" + "\n".join(found))

    if offences:
        pytest.fail(
            f"Legacy infrastructure import in module '{module_name}'."
            "\nUse the public surface instead: di.cache() (operation-based"
            " CacheProvider), DbSession / Database(name), publish_event,"
            " core.objectstore()/taskqueue()/database(). Importing a raw"
            " provider/global or an infra-internal module couples the module to"
            " a concrete backend.\n" + "".join(offences),
            pytrace=False,
        )


def test_di_public_surface_imports_no_concrete_driver() -> None:
    """``sandloom.core.di`` is the public DI surface modules import from.

    It must reach infrastructure (cache, notifications, …) through the
    capability registry — never by importing a concrete driver
    (``sandloom.core.drivers.*``). The registry/config layer chooses the
    backend; the call site only names a capability. This ratchet keeps the
    door shut so a future "quick fallback" cannot re-couple the public surface
    to one engine.
    """
    di_path = SANDLOOM_ROOT / "core" / "di.py"
    tree = ast.parse(di_path.read_text())
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("sandloom.core.drivers"):
                names = ", ".join(a.name for a in node.names)
                offences.append(
                    f"Line {node.lineno}: from {node.module} import {names}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sandloom.core.drivers"):
                    offences.append(f"Line {node.lineno}: import {alias.name}")
    assert not offences, (
        "sandloom.core.di must resolve infrastructure through the capability "
        "registry (see sandloom.core.infra.access), "
        "never a concrete driver:\n" + "\n".join(offences)
    )


# Kernel modules allowed to make an UPWARD import into the public DI aggregator
# (``sandloom.core.di``) at runtime. The bar has reached the floor: this set is EMPTY
# and must stay empty. The ``cache`` edge (idempotency/transport/rate_limit) was
# cut by importing the accessor from ``sandloom.core.infra.access``; the ``db_session``
# edge (consumers/projections/rpc) by relocating the UoW write-path engine to the
# ``sandloom.core.unit_of_work`` leaf. A low-level module needing a UoW primitive
# imports it from that leaf — never up from ``di``.
DI_UPWARD_BASELINE: set[str] = set()


def _type_checking_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """Line ranges of every ``if TYPE_CHECKING:`` block (imports there are
    typing-only and create no runtime cycle, so they don't count as edges)."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = (
                test.id if isinstance(test, ast.Name) else getattr(test, "attr", None)
            )
            if name == "TYPE_CHECKING":
                lines = [
                    ln
                    for ln in (getattr(n, "lineno", None) for n in ast.walk(node))
                    if ln is not None
                ]
                if lines:
                    spans.append((min(lines), max(lines)))
    return spans


def _runtime_di_imports(file_path: Path) -> list[int]:
    """Line numbers of runtime (non-TYPE_CHECKING) imports of ``sandloom.core.di``."""
    tree = ast.parse(file_path.read_text())
    spans = _type_checking_spans(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            is_di = node.module == "sandloom.core.di" or node.module.startswith(
                "sandloom.core.di."
            )
            in_tc = any(lo <= node.lineno <= hi for lo, hi in spans)
            if is_di and not in_tc:
                found.append(node.lineno)
    return found


def test_kernel_has_no_unbaselined_upward_di_import() -> None:
    """No kernel module may import the public DI aggregator at runtime.

    ``sandloom.core.di`` is the module-facing aggregator; a lower-level kernel module
    importing it is a structural cycle (the lazy ``# noqa`` imports that papered
    over it). Such a module must reach the primitive from its leaf instead —
    e.g. ``from sandloom.core.infra.access import cache``. Only the grandfathered
    ``db_session`` importers in ``DI_UPWARD_BASELINE`` are tolerated, and that
    set may only shrink.
    """
    core_root = SANDLOOM_ROOT / "core"
    offenders: set[str] = set()
    for py_file in core_root.glob("**/*.py"):
        if "tests" in py_file.parts:
            continue
        if py_file.name == "di.py":  # the aggregator itself
            continue
        if _runtime_di_imports(py_file):
            offenders.add(py_file.relative_to(core_root).as_posix())

    new = sorted(offenders - DI_UPWARD_BASELINE)
    stale = sorted(DI_UPWARD_BASELINE - offenders)
    assert not new, (
        "New upward import of sandloom.core.di from inside the kernel: "
        + ", ".join(new)
        + ".\nA low-level module must not reach UP to the public DI aggregator. "
        "Import the primitive from its leaf (e.g. sandloom.core.infra.access.cache) "
        "or have it injected."
    )
    assert not stale, (
        "These modules no longer import sandloom.core.di at runtime — remove them "
        "from DI_UPWARD_BASELINE so the ratchet stays tight: " + ", ".join(stale)
    )


def test_analyzer_detects_legacy_infra_import(tmp_path: Path) -> None:
    """Self-test: the analyzer must flag a forbidden import."""
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "from sandloom.core._internal.db_provider import SessionLocal\n"
        "from sandloom.core import get_event_bus\n"
        "from sandloom.core.di import get_cache as gc\n"
        "import neo4j\n"
        "from temporalio.client import Client\n"
    )
    found = find_legacy_infra_imports(sample)
    assert len(found) == 5, found
