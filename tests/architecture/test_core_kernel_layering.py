"""Architecture ratchets that keep the kernel (``sandloom.core``) layered.

These guard against the class of regressions paid down in the di/facade
decoupling work: a low-level module reaching UP into an aggregator, an infra
accessor pulling a concrete backend onto the hot path, or a lazy
``# noqa: PLC0415`` import quietly turning into an eager import cycle.

Acyclicity and layering of the whole kernel import graph (function-local
imports included) are import-linter contracts (``.importlinter``,
``test_kernel_import_contracts.py``, ADR-0007). The ratchets here are finer
than layers (a baseline that may only SHRINK):

2. No un-baselined module imports the public **facade** aggregator at runtime.
3. Concrete infra **drivers** are imported only in the composition layer
   (the driver tree itself + bootstrap + the relational/raw adapters).
4. The **facade** aggregator does not import the concrete RPC mechanism — it
   resolves remote contracts through an injected resolver instead.

TYPE_CHECKING imports are exempt everywhere here (typing-only, no runtime edge).
"""

from __future__ import annotations

import ast
from pathlib import Path

SANDLOOM_ROOT = Path(__file__).resolve().parents[2] / "src" / "sandloom"
CORE_ROOT = SANDLOOM_ROOT / "core"


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #
def _line_spans(tree: ast.AST, node_types: tuple[type, ...]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, node_types):
            lines = [
                ln
                for ln in (getattr(n, "lineno", None) for n in ast.walk(node))
                if ln is not None
            ]
            if lines:
                spans.append((min(lines), max(lines)))
    return spans


def _type_checking_spans(tree: ast.AST) -> list[tuple[int, int]]:
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


def _imported_modules(file_path: Path, *, eager_only: bool) -> set[str]:
    """Return the ``sandloom.core.*`` modules ``file_path`` imports.

    ``eager_only`` drops imports inside function bodies (lazy / cycle-breaking)
    and inside ``if TYPE_CHECKING:`` blocks. With ``eager_only=False`` only the
    TYPE_CHECKING block is dropped (runtime imports, lazy or not).
    """
    tree = ast.parse(file_path.read_text())
    tc = _type_checking_spans(tree)
    fn = (
        _line_spans(tree, (ast.FunctionDef, ast.AsyncFunctionDef)) if eager_only else []
    )

    def excluded(lineno: int) -> bool:
        if any(lo <= lineno <= hi for lo, hi in tc):
            return True
        return any(lo <= lineno <= hi for lo, hi in fn)

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("sandloom.core") and not excluded(node.lineno):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sandloom.core") and not excluded(node.lineno):
                    found.add(alias.name)
    return found


def _module_name(rel_path: Path) -> str:
    parts = rel_path.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("sandloom", "core", *parts)) if parts else "sandloom.core"


def _kernel_files() -> list[Path]:
    return [p for p in CORE_ROOT.glob("**/*.py") if "tests" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(CORE_ROOT).as_posix()


# --------------------------------------------------------------------------- #
# 2. Nobody (un-baselined) imports the facade aggregator at runtime
# --------------------------------------------------------------------------- #
# Modules allowed to import ``sandloom.core.facade`` at runtime: the FastAPI adapter
# hub (di) and the composition/orchestration layer that legitimately drives the
# facade. A low-level domain module must NOT — it should depend on a contract or
# receive what it needs. Entries may be REMOVED, never added.
FACADE_IMPORT_BASELINE = {
    # The kernel application factory: the composition root itself (moved here
    # from sandloom/main.py so a kernel-only install can build an app) — it is what
    # constructs the facade, not a low-level module reaching up to it.
    "app_factory.py",
    "di.py",
    "lifecycle.py",
    "cli/migrate.py",
    # The test harness's module-subset helpers (module_topology,
    # load_topology: also `sandloom add deploy` and the split-profile checks) build
    # a registration-only facade per module set (test composition).
    "testing/boot.py",
}


FACADE_PACKAGE = CORE_ROOT / "facade"


def test_no_unbaselined_runtime_facade_import() -> None:
    offenders = {
        _rel(p)
        for p in _kernel_files()
        if FACADE_PACKAGE not in p.parents
        and any(
            m == "sandloom.core.facade" or m.startswith("sandloom.core.facade.")
            for m in _imported_modules(p, eager_only=False)
        )
    }
    new = sorted(offenders - FACADE_IMPORT_BASELINE)
    stale = sorted(FACADE_IMPORT_BASELINE - offenders)
    assert not new, (
        "New runtime import of sandloom.core.facade from inside the kernel: "
        + ", ".join(new)
        + ".\nLow-level modules must not reach UP to the facade aggregator — "
        "depend on a contract or have the dependency injected. If this really is "
        "composition/orchestration code, add it to FACADE_IMPORT_BASELINE."
    )
    assert not stale, (
        "These modules no longer import sandloom.core.facade at runtime — remove them "
        "from FACADE_IMPORT_BASELINE so the ratchet stays tight: " + ", ".join(stale)
    )


# --------------------------------------------------------------------------- #
# 3. Concrete drivers live only in the composition layer
# --------------------------------------------------------------------------- #
# Files OUTSIDE sandloom/core/drivers/ allowed to import a concrete driver
# (``sandloom.core.drivers.*``): the registry composition root (bootstrap),
# the raw relational/session adapters. Every other kernel module — crucially
# the infra ACCESS layer (infra/access.py) — must stay
# driver-agnostic and reach backends only through the capability registry.
# Entries may be REMOVED, never added.
DRIVER_IMPORT_ALLOWLIST = {
    "_internal/db_provider.py",
    # The composition root builds the registry (sandloom.core.drivers.bootstrap).
    "app_factory.py",
}


def test_concrete_drivers_imported_only_in_composition_layer() -> None:
    drivers_dir = CORE_ROOT / "drivers"
    offenders = set()
    for p in _kernel_files():
        if drivers_dir in p.parents:  # the driver tree itself
            continue
        if any(
            m == "sandloom.core.drivers" or m.startswith("sandloom.core.drivers.")
            for m in _imported_modules(p, eager_only=False)
        ):
            offenders.add(_rel(p))
    new = sorted(offenders - DRIVER_IMPORT_ALLOWLIST)
    stale = sorted(DRIVER_IMPORT_ALLOWLIST - offenders)
    assert not new, (
        "Concrete driver imported outside the composition layer: "
        + ", ".join(new)
        + ".\nReach infrastructure through the capability registry "
        "(core.capability(...) / di.cache() / sandloom.core.infra.access), not a "
        "concrete sandloom.core.drivers.* module. If this genuinely belongs to "
        "the composition layer, add it to DRIVER_IMPORT_ALLOWLIST."
    )
    assert not stale, (
        "These files no longer import a concrete driver — remove them from "
        "DRIVER_IMPORT_ALLOWLIST so the ratchet stays tight: " + ", ".join(stale)
    )


# --------------------------------------------------------------------------- #
# 4. The facade aggregator does not import the concrete RPC mechanism
# --------------------------------------------------------------------------- #
def test_facade_does_not_import_concrete_rpc() -> None:
    """``sandloom.core.facade`` must not import ``sandloom.core.rpc``.

    ``CoreFacade.get_service`` resolves a cross-process contract through an
    injected resolver (``set_service_resolver``, wired by
    ``rpc.install_service_rpc_routes`` at composition) and raises the relocated
    ``ServiceUnavailableInDeployment`` from ``module_api._service_registry`` — so the
    aggregator stays ignorant of the concrete RPC implementation. Re-coupling it
    (even via a lazy ``# noqa`` import) is the regression this guards.
    """
    offending = sorted(
        m
        for facade in sorted(FACADE_PACKAGE.glob("*.py"))
        for m in _imported_modules(facade, eager_only=False)
        if m == "sandloom.core.rpc" or m.startswith("sandloom.core.rpc.")
    )
    assert not offending, (
        "sandloom.core.facade imports the concrete RPC mechanism: "
        + ", ".join(offending)
        + ".\nThe facade must depend on the injected service resolver "
        "(set_service_resolver), not import sandloom.core.rpc."
    )
