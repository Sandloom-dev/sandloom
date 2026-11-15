"""Drivers import only the driver kit, never the kernel (ADR-0011, ADR-0016).

A driver that imports ``sandloom.core`` is the fault ADR-0011 fixed: it ties
the driver to kernel internals, so it cannot leave the kernel wheel, and a
kernel refactor breaks it. ``packages/sandloom-s3`` is the model: it imports
``sandloom.driverkit`` and nothing else of Sandloom's.

- **Driver packages.** Every module shipped from ``packages/*/src`` imports
  nothing of Sandloom's but ``sandloom.driverkit`` (ADR-0016 section 4): not
  ``sandloom.core``, not ``sandloom.sdk``. No allowlist. Exempt:
  ``sandloom-standard`` (:data:`NOT_DRIVER_PACKAGES`), a composition package
  that assembles the standard application from ``sandloom.sdk`` and ships no
  driver. A package's own tests (``packages/*/tests``) are out of scope: they
  may use the kernel's test harness.
- **Built-in drivers.** Every module under ``src/sandloom/core/drivers`` (but
  the package ``__init__`` and the kernel's registration table
  ``bootstrap.py``) and every module a row of ``BUILTIN_DRIVER_ROWS`` targets
  is held to the same rule; the ``sandloom.core`` imports they still make are
  listed in ``drivers_core_import_allowlist.json``, each entry naming
  ``removed_by`` (the tm slice that removes them, or ``stays: <reason>``).
  The list only shrinks: an import it does not list fails, an entry no longer
  imported does not, and :data:`SEEDED` pins it, so an entry added to the
  JSON fails too. No new entry, ever: a driver written since (``atomic_store``,
  ``lease``, ``broadcast``) imports only ``sandloom.driverkit``, and what it needs
  of the kernel is re-exported there (the code stays in ``sandloom.core``). The
  kernel never imports ``sandloom.driverkit`` (``.importlinter``,
  ``kernel-never-imports-driverkit``), so the door stays one way.

Counted, at any depth (function-local and ``TYPE_CHECKING`` imports
included: a moved driver has neither): ``import sandloom...``,
``from sandloom... import``, ``from sandloom import core``, a relative import
that lands in ``sandloom``, an attribute of a bare ``import sandloom``
(``sandloom.core....``), and ``importlib.import_module`` / ``__import__`` of
a ``sandloom...`` name, positional or ``name=``. A call whose name is not a
literal string counts as :data:`DYNAMIC`: it cannot be checked, so it fails.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.drivers.bootstrap import BUILTIN_DRIVER_ROWS

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
DRIVERS = SRC / "sandloom" / "core" / "drivers"
PACKAGES = REPO / "packages"
ALLOWLIST = Path(__file__).with_name("drivers_core_import_allowlist.json")

#: Kernel modules under ``core/drivers`` that are not drivers.
NOT_DRIVERS = frozenset({"__init__.py", "bootstrap.py"})

#: ``packages/<dir>`` trees under ``src`` that ship no driver, and why.
NOT_DRIVER_PACKAGES: dict[str, str] = {
    "sandloom-standard": (
        "a composition package: it assembles the standard application from "
        "sandloom.sdk (sandloom_standard.main) and ships no driver"
    ),
}

#: Kernel-internal providers a row of ``BUILTIN_DRIVER_ROWS`` targets that are
#: not drivers: the volatile-bus shape of ``eventbus`` (``memory``, ``redis``,
#: spec 7) is the kernel's own bus behind the capability, in ``core/eventing``,
#: and implements no transport. It is not exported from the driverkit, so a
#: driver package cannot be written against it. Permanent by design: the
#: volatile buses stay in the kernel, no later slice moves them to a package.
KERNEL_INTERNAL: frozenset[str] = frozenset(
    {"sandloom.core.eventing.volatile_provider"}
)

#: Top-level driver modules that ship as one package (idk-pkg-celery-move moves
#: the three celery files together into ``sandloom-celery``): each imports the
#: others of its group freely, nothing else of Sandloom beyond the driver kit.
#: removed_by: idk-pkg-celery-move (the group goes when the files move; the
#: kernel's own copies are then gone and the package scan covers them). A group
#: member has no allowlist entry: ``test_a_sibling_group_member_has_no_allowlist_entry``.
SIBLING_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "sandloom.core.drivers.celery_broker_probe",
            "sandloom.core.drivers.celery_taskqueue",
            "sandloom.core.drivers.celery_worker",
        }
    ),
)

KERNEL = "sandloom.core"
DRIVERKIT = "sandloom.driverkit"
#: What a dynamic ``import_module`` / ``__import__`` (a non-literal name) counts as.
DYNAMIC = "<import_module of a non-literal name>"
_SLICE = re.compile(r"idk-[a-z0-9]+(?:-[a-z0-9]+)*")

_D = "src/sandloom/core/drivers/"

#: The allowlist as seeded (idk-driver-deps, shaped by idk-driver-depsb). The
#: JSON may only hold a subset of it: delete a pair here when it is removed.
SEEDED: dict[str, frozenset[str]] = {
    _D + "dbapi_aiosqlite.py": frozenset(
        {
            "sandloom.core.drivers.sql_dialects",
            "sandloom.core.sql_alchemy.dbapi",
            "sandloom.core.sql_alchemy.portable",
            "sandloom.core.sql_alchemy.write_admission",
        }
    ),
    _D + "dbapi_asyncpg.py": frozenset(
        {
            "sandloom.core.drivers.sql_dialects",
            "sandloom.core.sql_alchemy.dbapi",
            "sandloom.core.sql_alchemy.dead_connection",
            "sandloom.core.sql_alchemy.driver_compat",
            "sandloom.core.sql_alchemy.portable",
        }
    ),
    # idk-streams-driverkit: the driver reaches the kernel through the driver kit
    # only. What stays is its own transport, a sibling the package move takes
    # along (idk-pkg-redis-streams): no kernel internal. The default Redis client
    # helpers are the cache driver's (idk-streams-driverkitb: the driver kit
    # imports no driver); the package move takes them to sandloom-redis.
    _D + "event_bus_driver.py": frozenset(
        {
            "sandloom.core.drivers.redis_cache",
            "sandloom.core.drivers.redis_eventbus.streams_transport",
        }
    ),
    _D + "filesystem_storage.py": frozenset(
        {
            "sandloom.core.config.settings",
            "sandloom.core.infra.errors",
            "sandloom.core.infra.ports.objectstore",
        }
    ),
    _D + "linger_pool.py": frozenset(
        {
            "sandloom.core.sql_alchemy.driver_compat",
        }
    ),
    _D + "local_objectstore.py": frozenset(
        {
            "sandloom.core.config.settings",
            "sandloom.core.descriptors",
            "sandloom.core.drivers.filesystem_storage",
            "sandloom.core.infra.config",
            "sandloom.core.infra.contracts",
        }
    ),
    _D + "log_notifier.py": frozenset(
        {
            "sandloom.core.config.settings",
            "sandloom.core.descriptors",
            "sandloom.core.drivers.smtp_notifier",
            "sandloom.core.infra.config",
            "sandloom.core.infra.contracts",
            "sandloom.core.observability.metrics_utils",
        }
    ),
    _D + "memory_document_store.py": frozenset(
        {
            "sandloom.core.infra.config",
            "sandloom.core.infra.extra_capabilities",
            "sandloom.core.projections.stores",
        }
    ),
    _D + "redis_cache.py": frozenset(
        {
            "sandloom.core.descriptors",
            "sandloom.core.infra.config",
            "sandloom.core.infra.contracts",
            "sandloom.core.infra.ports._base",
        }
    ),
    _D + "schema_reset.py": frozenset(
        {
            "sandloom.core.drivers.dbapi_aiosqlite",
        }
    ),
    _D + "smtp_notifier.py": frozenset(
        {
            "sandloom.core.config.settings",
            "sandloom.core.infra.config",
            "sandloom.core.infra.contracts",
            "sandloom.core.infra.errors",
            "sandloom.core.observability.metrics_utils",
        }
    ),
    _D + "sql_dialects.py": frozenset(
        {
            "sandloom.core.sql_alchemy.portable",
        }
    ),
    _D + "sqlalchemy_database.py": frozenset(
        {
            "sandloom.core.config.settings",
            "sandloom.core.contracts.uow",
            "sandloom.core.descriptors",
            "sandloom.core.drivers.dbapi_aiosqlite",
            "sandloom.core.drivers.dbapi_asyncpg",
            "sandloom.core.drivers.linger_pool",
            "sandloom.core.infra.capabilities",
            "sandloom.core.infra.config",
            "sandloom.core.infra.contracts",
            "sandloom.core.infra.ports._base",
            "sandloom.core.observability.metrics",
            "sandloom.core.runtime",
            "sandloom.core.sql_alchemy.base",
            "sandloom.core.sql_alchemy.command_log",
            "sandloom.core.sql_alchemy.dbapi",
            "sandloom.core.sql_alchemy.dead_connection",
            "sandloom.core.sql_alchemy.driver_compat",
            "sandloom.core.sql_alchemy.model_schema",
            "sandloom.core.sql_alchemy.outbox_store",
            "sandloom.core.sql_alchemy.planner_adapter",
            "sandloom.core.sql_alchemy.portable",
            "sandloom.core.sql_alchemy.read_only_policy",
            "sandloom.core.sql_alchemy.reconnect",
            "sandloom.core.sql_alchemy.request_log",
            "sandloom.core.sql_alchemy.tenant_rls",
            "sandloom.core.sql_alchemy.uow",
            "sandloom.core.sql_alchemy.write_admission",
        }
    ),
    _D + "system_db.py": frozenset(
        {
            "sandloom.core.drivers.sqlalchemy_database",
            "sandloom.core.infra.config",
            "sandloom.core.sql_alchemy.outbox_store",
            "sandloom.core.sql_alchemy.write_admission",
            "sandloom.core.tenant_context",
            "sandloom.core.unit_of_work.request",
        }
    ),
}


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(
    module: str, level: int, target: str | None, package: bool
) -> str:
    """The absolute module ``from <level dots><target> import`` names."""
    parts = module.split(".")
    base = parts if package else parts[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def _is_sandloom(name: str) -> bool:
    return name == "sandloom" or name.startswith("sandloom.")


def _is_kernel(name: str) -> bool:
    return name == KERNEL or name.startswith(KERNEL + ".")


def _is_driverkit(name: str) -> bool:
    return name == DRIVERKIT or name.startswith(DRIVERKIT + ".")


def sandloom_imports(path: Path, module: str) -> set[str]:
    """The ``sandloom`` modules ``path`` (the module ``module``) imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = path.name == "__init__.py"
    found: set[str] = set()
    # ``import sandloom`` / ``import sandloom.x`` (no ``as``) binds ``sandloom``.
    bare = any(
        isinstance(node, ast.Import)
        and any(_is_sandloom(a.name) and a.asname is None for a in node.names)
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(
                a.name
                for a in node.names
                if _is_sandloom(a.name) and a.name != "sandloom"
            )
        elif isinstance(node, ast.ImportFrom):
            found.update(_from_import(node, module, package))
        elif isinstance(node, ast.Call):
            found.update(_dynamic_import(node))
        elif (
            bare
            and isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "sandloom"
        ):
            found.add(f"sandloom.{node.attr}")
    return found


def _from_import(node: ast.ImportFrom, module: str, package: bool) -> set[str]:
    target = (
        _resolve_relative(module, node.level, node.module, package)
        if node.level
        else node.module or ""
    )
    if target == "sandloom":
        return {f"sandloom.{a.name}" for a in node.names}
    return {target} if _is_sandloom(target) else set()


def _dynamic_import(node: ast.Call) -> set[str]:
    """``import_module`` / ``__import__``: the literal ``sandloom`` name it
    imports, :data:`DYNAMIC` for a name that is not a literal string."""
    func = node.func
    called = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else ""
    )
    if called not in {"import_module", "__import__"}:
        return set()
    arg = node.args[0] if node.args else None
    if arg is None:
        arg = next((k.value for k in node.keywords if k.arg == "name"), None)
    if arg is None:
        return {DYNAMIC}
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return {arg.value} if _is_sandloom(arg.value) else set()
    return {DYNAMIC}


def _row_modules() -> set[str]:
    return {target.partition(":")[0] for _, _, target, *_ in BUILTIN_DRIVER_ROWS}


def builtin_driver_files() -> list[Path]:
    """Every built-in driver module this checkout has."""
    # rglob: a driver subpackage (drivers/<pkg>/) is scanned file by file, its
    # ``__init__.py`` too. NOT_DRIVERS names the top-level kernel modules only.
    files = {p for p in DRIVERS.rglob("*.py") if not _not_a_driver(p)}
    for module in _row_modules() - KERNEL_INTERNAL:
        base = SRC.joinpath(*module.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file() and not _not_a_driver(candidate):
                files.add(candidate)
    return sorted(files)


def _not_a_driver(path: Path) -> bool:
    """A top-level ``core/drivers/__init__.py`` or ``bootstrap.py``: the kernel's."""
    return path.name in NOT_DRIVERS and path.parent == DRIVERS


def _own_package(path: Path, root: Path) -> str:
    """The ``drivers/<pkg>`` module a file of a driver subpackage belongs to
    (``""`` for a top-level driver file): imports inside it stay in the driver."""
    parts = path.relative_to(root).with_suffix("").parts
    base = ("sandloom", "core", "drivers")
    if len(parts) > len(base) + 1 and parts[: len(base)] == base:
        return ".".join(parts[: len(base) + 1])
    return ""


def package_files() -> list[Path]:
    """Every module shipped from a driver package's ``packages/*/src`` tree."""
    return sorted(
        path
        for path in PACKAGES.glob("*/src/**/*.py")
        if path.relative_to(PACKAGES).parts[0] not in NOT_DRIVER_PACKAGES
    )


def violations(
    files: Iterable[Path],
    root: Path,
    repo: Path,
    allowed: Mapping[str, Iterable[str]],
) -> list[str]:
    """``file: name`` for each import beyond the driver kit ``allowed`` omits."""
    out: list[str] = []
    for path in files:
        rel = path.relative_to(repo).as_posix()
        listed = set(allowed.get(rel, ()))
        own = _own_package(path, root)
        module = _module_name(path, root)
        group = next((g for g in SIBLING_GROUPS if module in g), frozenset())
        found = {
            name
            for name in sandloom_imports(path, module)
            if not _is_driverkit(name)
            and name not in group
            # a file of drivers/<pkg>/ importing its own package is not a
            # kernel import: the package moves as one
            and not (own and (name == own or name.startswith(own + ".")))
        }
        out.extend(f"{rel}: {name}" for name in sorted(found - listed))
    return out


def _entries() -> list[dict[str, Any]]:
    return list(json.loads(ALLOWLIST.read_text(encoding="utf-8"))["entries"])


def _allowlist() -> dict[str, list[str]]:
    """``file -> imports`` of the allowlist, cut to what :data:`SEEDED` holds
    (a pair beyond the seed fails its own test and is never allowed)."""
    return {
        e["file"]: [i for i in e["imports"] if i in SEEDED.get(e["file"], ())]
        for e in _entries()
    }


def test_packages_import_only_the_driverkit() -> None:
    files = package_files()
    assert files, "no driver package module found under packages/*/src"
    found: list[str] = []
    for path in files:
        src = next(p for p in path.parents if p.name == "src")
        found += violations([path], src, REPO, {})
    assert not found, (
        "a driver package imports Sandloom beyond sandloom.driverkit (ADR-0016 "
        "section 4, ADR-0011); import it from sandloom.driverkit:\n  "
        + "\n  ".join(found)
    )


def test_builtin_drivers_gain_no_kernel_import() -> None:
    found = violations(builtin_driver_files(), SRC, REPO, _allowlist())
    assert not found, (
        "a built-in driver gained a sandloom.core import (ADR-0016). Drivers "
        "read options, not settings; a built-in records metrics through "
        "sandloom.driverkit.observe. The "
        "allowlist only shrinks:\n  " + "\n  ".join(found)
    )


def test_the_allowlist_adds_nothing_to_its_seed() -> None:
    """:data:`SEEDED` pins the allowlist: an entry beyond it fails."""
    beyond = [
        f"{e['file']}: {name}"
        for e in _entries()
        for name in e["imports"]
        if name not in SEEDED.get(e["file"], ())
    ]
    assert not beyond, (
        "the allowlist gained an entry; it only shrinks (remove the import "
        "instead):\n  " + "\n  ".join(beyond)
    )


def test_every_allowlist_entry_names_its_removal() -> None:
    entries = _entries()
    problems = [str(e.get("file")) for e in entries if not _well_formed(e)]
    assert not problems, (
        "allowlist entries need file, a sorted list of sandloom.core imports "
        f"and removed_by (an idk-* slice or 'stays: <reason>'): {problems}"
    )
    files = [e["file"] for e in entries]
    assert len(files) == len(set(files)), "duplicate allowlist entry"
    assert files == sorted(files), "keep the allowlist sorted by file"
    known = {p.relative_to(REPO).as_posix() for p in builtin_driver_files()}
    # A file that left (moved to a package) may stay listed until pruned, but
    # a typo'd path guards nothing.
    unknown = sorted(f for f in files if f not in known and (REPO / f).exists())
    assert not unknown, f"allowlist names files that are no drivers: {unknown}"


def _well_formed(entry: Mapping[str, Any]) -> bool:
    imports = entry.get("imports")
    removed_by = str(entry.get("removed_by", "")).strip()
    return (
        isinstance(entry.get("file"), str)
        and isinstance(imports, list)
        and bool(imports)
        and imports == sorted(set(imports))
        and all(isinstance(i, str) and _is_kernel(i) for i in imports)
        and (
            bool(_SLICE.fullmatch(removed_by))
            or (removed_by.startswith("stays: ") and len(removed_by) > len("stays: "))
        )
    )


# ---------------------------------------------------------------- self-test


def _plant(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    for rel, body in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return src


def _scan(tmp_path: Path, rel: str, body: str) -> set[str]:
    src = _plant(tmp_path, {rel: body})
    path = src / rel
    return sandloom_imports(path, _module_name(path, src))


def test_every_import_form_is_counted(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        "sandloom/core/drivers/x.py",
        "import sandloom.core.config.settings\n"
        "from sandloom.core.effects import RECORD_TTL_SECONDS\n"
        "from sandloom import core\n"
        "from ..infra import errors\n"
        "from . import redis_cache\n"
        "import importlib\n"
        "def f():\n"
        "    from sandloom.core.tasks.enqueue import enqueue_once\n"
        "    importlib.import_module('sandloom.core.worker')\n"
        "    importlib.import_module(name='sandloom.core.di')\n"
        "    __import__('sandloom.core.tenant_context')\n"
        "from sandloom.driverkit import observe\n"
        "import sandloom.driverkit.metrics\n",
    )
    assert found == {
        "sandloom.core.config.settings",
        "sandloom.core.effects",
        "sandloom.core",
        "sandloom.core.infra",
        "sandloom.core.drivers",
        "sandloom.core.tasks.enqueue",
        "sandloom.core.worker",
        "sandloom.core.di",
        "sandloom.core.tenant_context",
        "sandloom.driverkit",
        "sandloom.driverkit.metrics",
    }


def test_the_sdk_counts_beyond_the_driverkit(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        "sandloom_x/a.py",
        "from sandloom.sdk.infra import settings\n"
        "from sandloom.sdk import infra\n"
        "from sandloom import sdk\n",
    )
    assert found == {"sandloom.sdk.infra", "sandloom.sdk"}


def test_attribute_access_on_a_bare_import_is_counted(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        "sandloom_x/a.py",
        "import sandloom\n"
        "import sandloom.driverkit\n"
        "settings = sandloom.core.config.settings.settings\n"
        "observe = sandloom.driverkit.observe\n"
        "def f():\n"
        "    return sandloom.sdk.infra\n",
    )
    assert found == {"sandloom.driverkit", "sandloom.core", "sandloom.sdk"}


def test_a_dynamic_import_is_counted(tmp_path: Path) -> None:
    found = _scan(
        tmp_path,
        "sandloom_x/a.py",
        "import importlib\n"
        "name = 'core'\n"
        "importlib.import_module('sandloom.' + name)\n"
        "importlib.import_module(name=f'sandloom.{name}')\n"
        "__import__(name)\n"
        "importlib.import_module('celery')\n"
        "importlib.import_module('sandloom.driverkit.metrics')\n",
    )
    assert found == {DYNAMIC, "sandloom.driverkit.metrics"}


def test_a_new_import_bites_and_a_stale_entry_does_not(tmp_path: Path) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom/core/drivers/y.py": (
                "from sandloom.core.infra.config import InstanceConfig\n"
                "from sandloom.core.config.settings import settings\n"
                "from sandloom.driverkit import observe\n"
            ),
        },
    )
    path = src / "sandloom/core/drivers/y.py"
    rel = "src/sandloom/core/drivers/y.py"
    allowed = {rel: ["sandloom.core.infra.config", "sandloom.core.effects"]}
    # settings is new: it bites; the stale effects entry is tolerated; the
    # driver kit never counts.
    assert violations([path], src, tmp_path, allowed) == [
        f"{rel}: sandloom.core.config.settings"
    ]
    allowed[rel].append("sandloom.core.config.settings")
    assert violations([path], src, tmp_path, allowed) == []


def test_a_driver_subpackage_is_scanned_and_its_own_imports_are_exempt(
    tmp_path: Path,
) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom/core/drivers/sub/__init__.py": "",
            "sandloom/core/drivers/sub/a.py": "from . import b\n",
            "sandloom/core/drivers/sub/b.py": (
                "from sandloom.core.drivers.sub.a import A\n"
                "from sandloom.driverkit import observe\n"
            ),
            "sandloom/core/drivers/sub/x.py": (
                "from sandloom.core.config import settings\n"
                "from sandloom.core.drivers.sub_other import y\n"
                "from sandloom.core.drivers.sub import a\n"
            ),
        },
    )
    files = sorted((src / "sandloom/core/drivers/sub").glob("*.py"))
    assert violations(files, src, tmp_path, {}) == [
        "src/sandloom/core/drivers/sub/x.py: sandloom.core.config",
        "src/sandloom/core/drivers/sub/x.py: sandloom.core.drivers.sub_other",
    ]


def test_a_sibling_group_exempts_only_its_own_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom/core/drivers/a.py": (
                "from sandloom.core.drivers.b import B\n"
                "from sandloom.core.drivers.c import C\n"
                "from sandloom.core.infra.config import InstanceConfig\n"
            ),
            "sandloom/core/drivers/b.py": "from sandloom.driverkit import observe\n",
            "sandloom/core/drivers/c.py": "from sandloom.core.drivers.a import A\n",
        },
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "SIBLING_GROUPS",
        (frozenset({"sandloom.core.drivers.a", "sandloom.core.drivers.b"}),),
    )
    files = [src / f"sandloom/core/drivers/{n}.py" for n in "abc"]
    assert violations(files, src, tmp_path, {}) == [
        "src/sandloom/core/drivers/a.py: sandloom.core.drivers.c",
        "src/sandloom/core/drivers/a.py: sandloom.core.infra.config",
        "src/sandloom/core/drivers/c.py: sandloom.core.drivers.a",
    ]


def test_every_sibling_group_member_is_a_driver_file() -> None:
    found = {_module_name(p, SRC) for p in builtin_driver_files()}
    assert all(group <= found for group in SIBLING_GROUPS)


def group_members_with_entries(
    groups: tuple[frozenset[str], ...], entries: list[dict[str, Any]]
) -> list[str]:
    """The allowlist files that are members of a sibling group (a group member
    imports only the driver kit and its siblings: an entry would hide an import
    the group's exemption must not cover)."""
    members = {
        "src/" + module.replace(".", "/") + ".py"
        for group in groups
        for module in group
    }
    return sorted({e["file"] for e in entries} & members)


def test_a_sibling_group_member_has_no_allowlist_entry() -> None:
    assert group_members_with_entries(SIBLING_GROUPS, _entries()) == []


def test_a_group_member_with_an_allowlist_entry_bites() -> None:
    entries = [
        {"file": _D + "celery_worker.py", "imports": ["sandloom.core.x"]},
        {"file": _D + "redis_cache.py", "imports": ["sandloom.core.y"]},
    ]
    assert group_members_with_entries(SIBLING_GROUPS, entries) == [
        _D + "celery_worker.py"
    ]


def test_the_scan_reaches_the_driver_subpackages() -> None:
    found = {p.relative_to(DRIVERS).as_posix() for p in builtin_driver_files()}
    assert "redis_eventbus/streams_transport.py" in found
    assert "__init__.py" not in found
    assert "bootstrap.py" not in found


def test_a_package_importing_beyond_the_driverkit_bites(tmp_path: Path) -> None:
    src = _plant(
        tmp_path,
        {
            "sandloom_x/__init__.py": (
                "from sandloom.core.descriptors import HealthResult\n"
                "from sandloom.sdk.infra import settings\n"
                "from sandloom.driverkit import observe\n"
            )
        },
    )
    path = src / "sandloom_x/__init__.py"
    assert violations([path], src, tmp_path, {}) == [
        "src/sandloom_x/__init__.py: sandloom.core.descriptors",
        "src/sandloom_x/__init__.py: sandloom.sdk.infra",
    ]


def test_the_seed_holds_the_live_allowlist() -> None:
    """Every pair the JSON lists is in :data:`SEEDED` (so a new one fails
    ``test_the_allowlist_adds_nothing_to_its_seed``), and every seeded file
    is a known driver path or one that left."""
    for entry in _entries():
        assert set(entry["imports"]) <= SEEDED.get(entry["file"], frozenset())
    assert all(f.startswith(_D) for f in SEEDED)
    # The seed itself only shrinks: lower this number with every pair removed.
    # 89 -> 103 once (idk-eventbus-moveb, +14): pre-existing imports of the files
    # idk-eventbus-move moved out of core/eventing, newly counted by the subpackage
    # scan, not newly added; removed_by idk-pkg-redis-streams.
    # 103 -> 83 (idk-streams-driverkit, -20): the four files of redis_eventbus/
    # (14 pairs) and six of event_bus_driver's seven import only the driver kit.
    # 83 -> 84 (idk-streams-driverkitb, +1): event_bus_driver imports redis_cache
    # directly (the driver kit's redis_client wrapper hid that edge and inverted
    # the layering); removed_by idk-pkg-redis-streams.
    # 84 -> 79 (idk-pkg-celery, -5): the three celery files import only the driver
    # kit (the probe's timeout is a driver option); their sibling imports are the
    # package's own (SIBLING_GROUPS).
    assert sum(len(v) for v in SEEDED.values()) <= 79


@pytest.mark.parametrize(
    ("entry", "ok"),
    [
        ({"imports": ["sandloom.core.x"], "removed_by": "idk-pkg-redis"}, True),
        ({"imports": ["sandloom.core.x"], "removed_by": "stays: kernel"}, True),
        ({"imports": ["sandloom.core.x"], "removed_by": ""}, False),
        ({"imports": ["sandloom.core.x"], "removed_by": "stays: "}, False),
        ({"imports": ["sandloom.core.x"], "removed_by": "later"}, False),
        ({"imports": ["sandloom.core.x"]}, False),
        ({"imports": [], "removed_by": "idk-pkg-redis"}, False),
        ({"imports": ["sandloom.sdk"], "removed_by": "idk-pkg-redis"}, False),
        (
            {"imports": ["sandloom.core.y", "sandloom.core.x"], "removed_by": "idk-x"},
            False,
        ),
    ],
)
def test_the_entry_format(entry: dict[str, Any], ok: bool) -> None:
    assert _well_formed({"file": _D + "a.py", **entry}) is ok
