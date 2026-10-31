"""Pure AST analyzers behind the Sandloom architecture rules.

Every function here takes a file path (plus the namespace it is judged in) and
returns human-readable violation lines (``"Line N: ..."``). They import nothing
from the framework, so both the framework's own architecture tests and
``sandloom check`` (an application's CI gate) run the SAME analyzers — the
framework's strictness is the application's strictness.

Namespace parameters default to the framework tree (``sandloom``); an application
passes its own modules package (e.g. ``helpdesk.modules``) and model roots.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

#: FastAPI HTTP method decorator names.
HTTP_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "options", "head", "trace", "websocket"}
)

#: Directory / file names that mark a module's API (presentation) layer.
API_DIR_HINTS = frozenset({"api", "routers", "routes", "endpoints", "presentation"})
API_FILE_HINTS = frozenset({"api.py", "router.py", "routes.py"})


@functools.lru_cache(maxsize=4096)
def _parse_cached(path: str, _mtime_ns: int, _size: int) -> tuple[str, ast.AST]:
    source = Path(path).read_text(encoding="utf-8")
    return source, ast.parse(source, filename=path)


def _parsed(file_path: Path) -> tuple[str, ast.AST]:
    """``(source, tree)`` of a file, parsed once per run.

    ``sandloom check`` runs a dozen analyzers over every module file; the cache key
    includes the file's mtime and size, so an edited file is re-parsed. The
    returned tree is shared — analyzers only read it.
    """
    st = file_path.stat()
    return _parse_cached(str(file_path), st.st_mtime_ns, st.st_size)


def read_source(file_path: Path) -> str:
    return _parsed(file_path)[0]


def parse_file(file_path: Path) -> ast.AST:
    return _parsed(file_path)[1]


def _const_str(node: ast.AST | None) -> str | None:
    """A string the analyzer can prove statically (literal, ``+`` or f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left), _const_str(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [_const_str(v) for v in node.values]
        return "".join(p for p in parts if p is not None) if None not in parts else None
    return None


def _is_getattr(func: ast.AST, builtins_aliases: set[str]) -> bool:
    return (isinstance(func, ast.Name) and func.id == "getattr") or (
        isinstance(func, ast.Attribute)
        and func.attr == "getattr"
        and isinstance(func.value, ast.Name)
        and func.value.id in builtins_aliases
    )


def _reflective_attr_names(tree: ast.AST) -> list[tuple[int, ast.AST | None, str]]:  # noqa: C901
    """``(line, target, attr)`` of attributes reached by name, not by dot.

    ``getattr(obj, "x")`` / ``builtins.getattr(obj, "x")`` (target ``obj``) and
    ``operator.attrgetter("x")`` / ``attrgetter("a.x")`` (target ``None``; one
    entry per dotted segment) — the reflective spellings of ``obj.x``.
    """
    builtins_aliases = {"builtins"}
    operator_aliases = {"operator"}
    attrgetters = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "builtins":
                    builtins_aliases.add(alias.asname or "builtins")
                elif alias.name == "operator":
                    operator_aliases.add(alias.asname or "operator")
        elif isinstance(node, ast.ImportFrom) and node.module == "operator":
            for alias in node.names:
                if alias.name == "attrgetter":
                    attrgetters.add(alias.asname or "attrgetter")
    out: list[tuple[int, ast.AST | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if _is_getattr(func, builtins_aliases) and len(node.args) >= 2:
            attr = _const_str(node.args[1])
            if attr is not None:
                out.append((node.lineno, node.args[0], attr))
        elif (isinstance(func, ast.Name) and func.id in attrgetters) or (
            isinstance(func, ast.Attribute)
            and func.attr == "attrgetter"
            and isinstance(func.value, ast.Name)
            and func.value.id in operator_aliases
        ):
            for arg in node.args:
                dotted = _const_str(arg)
                for attr in (dotted or "").split("."):
                    if attr:
                        out.append((node.lineno, None, attr))  # noqa: PERF401 - cold path (startup / diagnostics)
    return out


def _dynamic_import_callables(tree: ast.AST) -> tuple[set[str], set[str]]:  # noqa: C901
    """``(module_aliases, function_names)`` that perform a dynamic import.

    ``module_aliases`` name the ``importlib`` / ``builtins`` modules (so
    ``<alias>.import_module`` / ``<alias>.__import__`` import); ``function_names``
    are bare names bound to ``importlib.import_module`` or ``__import__``
    (``from importlib import import_module as im``, ``im = il.import_module``).
    """
    modules = {"importlib", "builtins"}
    functions = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("importlib", "builtins"):
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if (node.module, alias.name) in (
                    ("importlib", "import_module"),
                    ("builtins", "__import__"),
                ):
                    functions.add(alias.asname or alias.name)
    # One level of plain re-binding: ``imp = importlib.import_module``.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if not isinstance(target, ast.Name):
                continue
            if (isinstance(value, ast.Name) and value.id in functions) or (
                isinstance(value, ast.Attribute)
                and value.attr in ("import_module", "__import__")
                and isinstance(value.value, ast.Name)
                and value.value.id in modules
            ):
                functions.add(target.id)
    return modules, functions


def dynamic_imports(tree: ast.AST) -> list[tuple[int, str, str]]:
    """``(line, module, call text)`` of each constant-string dynamic import.

    Covers ``importlib.import_module`` and ``__import__`` under any alias
    (``import importlib as il``, ``from importlib import import_module as im``,
    ``builtins.__import__``, a re-bound name) with a statically known module
    name (literal, constant concatenation, constant f-string). A relative
    ``import_module(".x", "pkg")`` is resolved against a constant package.
    """
    modules, functions = _dynamic_import_callables(tree)
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in functions:
            label = func.id
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in ("import_module", "__import__")
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            label = f"{func.value.id}.{func.attr}"
        else:
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        name = _const_str(node.args[0] if node.args else kwargs.get("name"))
        if not name:
            continue
        if name.startswith("."):
            package = _const_str(
                node.args[1] if len(node.args) > 1 else kwargs.get("package")
            )
            level = len(name) - len(name.lstrip("."))
            resolved = (
                resolve_relative(package.split("."), level, name[level:] or None)
                if package
                else None
            )
            if resolved is None:
                continue
            # resolve_relative treats ``package`` as the importing module's
            # package: ``import_module(".x", "a.b")`` is ``a.b.x``.
            name = resolved
        out.append((node.lineno, name, f"{label}('{name}')"))
    return out


def _root_name_and_attrs(expr: ast.AST) -> tuple[str | None, list[str]]:
    """Return ``(root_name, attrs)`` for attribute chains like ``x.y.z``."""
    attrs: list[str] = []
    cur = expr
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    root = cur.id if isinstance(cur, ast.Name) else None
    attrs.reverse()
    return root, attrs


def is_endpoint_decorator(dec: ast.AST) -> bool:
    """Detect ``@router.get`` / ``@app.post`` / ``@api_router.put(...)``."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    root, attrs = _root_name_and_attrs(target)
    if not attrs or attrs[-1] not in HTTP_METHODS:
        return False
    if isinstance(root, str) and any(
        k in root.lower() for k in ("router", "app", "api")
    ):
        return True
    return any("router" in a.lower() for a in attrs)


def endpoint_method(dec: ast.AST) -> str | None:
    target = dec.func if isinstance(dec, ast.Call) else dec
    _root, attrs = _root_name_and_attrs(target)
    if not attrs:
        return None
    return attrs[-1] if attrs[-1] in HTTP_METHODS else None


def has_any_endpoint(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(is_endpoint_decorator(d) for d in node.decorator_list)
        for node in ast.walk(tree)
    )


def _package_relative_dirs(path: Path) -> tuple[str, ...]:
    """Directories between the top of ``path``'s package chain and the file.

    Without an explicit module root the API layer is judged only inside the
    file's own package (directories carrying ``__init__.py``), never by an
    unrelated ancestor such as ``/srv/api/`` or ``~/routes/``. A file outside
    any package (an analyzer fixture) is judged by its parent directory.
    """
    dirs: list[str] = []
    cur = path.parent
    while (cur / "__init__.py").exists() and cur.parent != cur:
        dirs.append(cur.name)
        cur = cur.parent
    if not dirs:
        return (path.parent.name,)
    return tuple(reversed(dirs))


def is_api_file(path: Path, module_root: Path | None = None) -> bool:
    """``path`` belongs to a module's API (presentation) layer.

    Judged on the path RELATIVE to ``module_root`` (the business module's
    package directory) — an ``api`` component above the module (a checkout in
    ``/srv/api/``) must not exempt every file from the rule. Without a module
    root the file's own package chain is used.
    """
    if module_root is not None:
        try:
            dirs: tuple[str, ...] = path.relative_to(module_root).parts[:-1]
        except ValueError:
            dirs = _package_relative_dirs(path)
    else:
        dirs = _package_relative_dirs(path)
    return bool(set(dirs) & API_DIR_HINTS) or path.name in API_FILE_HINTS


# --------------------------------------------------------------------------- #
# 1. Module boundaries: no cross-business-module imports (every spelling)
# --------------------------------------------------------------------------- #


def find_cross_module_imports(
    file_path: Path,
    current_module: str,
    business_modules: Iterable[str],
    *,
    modules_package: str = "sandloom",
) -> list[str]:
    """Imports of a SIBLING business module (static, dynamic or reflective).

    ``modules_package`` is the dotted package holding the business modules
    (``sandloom`` for the framework, ``<app>.modules`` for an application).
    Catches ``import``/``from`` (absolute and relative), ``__import__``,
    ``importlib.import_module``, ``getattr(pkg, "mod")``, ``sys.modules`` reads
    and membership tests, and attribute access through an alias of the package.
    """
    tree = parse_file(file_path)
    scan = _CrossModuleScan(
        current_module,
        set(business_modules),
        modules_package,
        package=_file_package(file_path, current_module, modules_package),
    )
    scan.bind_aliases(tree)

    # importlib.import_module / __import__ under any alias (shared helper).
    for line, name, text in dynamic_imports(tree):
        if scan.target_of(name):
            scan.found.append(f"Line {line}: {text}")

    for node in ast.walk(tree):
        visit = _CROSS_MODULE_VISITORS.get(type(node))
        if visit is not None:
            visit(scan, node)
    return scan.found


def _file_package(
    file_path: Path, current_module: str, modules_package: str
) -> list[str]:
    """Dotted package of ``file_path`` (the package a relative import resolves in).

    Located from the module's directory (``.../<modules_package>/<module>/``);
    a file outside such a tree (an analyzer fixture) counts as sitting in the
    module's root package.
    """
    root = [*_package_parts(modules_package), current_module]
    parts = file_path.resolve().parts
    leaf = modules_package.split(".")[-1]
    for i in range(len(parts) - 1):
        # A top-level module package (``modules_package`` empty) has no
        # parent package directory to anchor on: the module's own does.
        if parts[i] == current_module and i > 0 and (not leaf or parts[i - 1] == leaf):
            inner = list(parts[i + 1 : -1])
            return [*root, *inner]
    return root


def _package_parts(package: str) -> list[str]:
    """``"a.b"`` -> ``["a", "b"]``; the top level (``""``) -> ``[]``."""
    return package.split(".") if package else []


class _CrossModuleScan:
    """State of one :func:`find_cross_module_imports` run, one visitor per node type."""

    def __init__(
        self,
        current_module: str,
        modules: set[str],
        modules_package: str,
        *,
        package: list[str] | None = None,
    ) -> None:
        self.current_module = current_module
        self.modules = modules
        self.modules_package = modules_package
        #: The file's own package: relative imports resolve against it.
        self.package = package or [*_package_parts(modules_package), current_module]
        # Top-level module packages: every dotted name is a candidate.
        self.prefix = modules_package + "." if modules_package else ""
        # Names bound to the modules package (``import sandloom`` / ``import sandloom as
        # p``).
        self.root_aliases: dict[str, str] = {}
        self.sys_aliases: set[str] = set()
        self.found: list[str] = []

    def target_of(self, dotted: str) -> str | None:
        if not dotted or not dotted.startswith(self.prefix):
            return None
        top = dotted[len(self.prefix) :].split(".")[0]
        return top if top != self.current_module and top in self.modules else None

    def sibling(self, name: str) -> bool:
        top = name.split(".")[0]
        return top != self.current_module and top in self.modules

    def bind_aliases(self, tree: ast.AST) -> None:
        modules_package = self.modules_package
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if modules_package and alias.name == modules_package:
                        if alias.asname:
                            self.root_aliases[alias.asname] = modules_package
                        else:
                            top = modules_package.split(".")[0]
                            self.root_aliases[top] = top
                    elif alias.name == "sys":
                        self.sys_aliases.add(alias.asname or "sys")
            elif isinstance(node, ast.ImportFrom) and node.module == "sys":
                self.sys_aliases.add("sys")

    def _is_sys_modules(self, node: ast.AST) -> bool:
        """``<sys alias>.modules``."""
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_aliases
            and node.attr == "modules"
        )

    def visit_import_from(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            if self.modules_package and node.module == self.modules_package:
                for alias in node.names:
                    if self.sibling(alias.name):
                        self.found.append(
                            f"Line {node.lineno}: from {node.module} "
                            f"import {alias.name}"
                        )
            elif self.target_of(node.module):
                self.found.append(f"Line {node.lineno}: from {node.module} import ...")
        elif node.level > 0:
            self._relative_import_from(node)

    def _relative_import_from(self, node: ast.ImportFrom) -> None:
        """Resolve against the file's package, then judge the absolute name: a
        relative import that stays inside the module is never cross-module
        (``from .catalog import X`` in ``billing/models`` is billing's own)."""
        dots = "." * node.level
        up = node.level - 1
        if up >= len(self.package):
            return  # beyond the top-level package: not importable
        base = self.package[: len(self.package) - up]
        if node.module:
            if self.target_of(".".join([*base, node.module])):
                self.found.append(
                    f"Line {node.lineno}: from {dots}{node.module} import ..."
                )
            return
        for alias in node.names:
            if self.target_of(".".join([*base, alias.name])):
                self.found.append(
                    f"Line {node.lineno}: from {dots} import {alias.name}"
                )

    def visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self.target_of(alias.name):
                self.found.append(f"Line {node.lineno}: import {alias.name}")

    def visit_call(self, node: ast.Call) -> None:
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "getattr") or (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "builtins"
            and func.attr == "getattr"
        ):
            self._reflective_getattr(node, func)
            return
        first = _const_str(node.args[0]) if node.args else None
        if (
            isinstance(func, ast.Attribute)
            and self._is_sys_modules(func.value)
            and func.attr == "get"
            and first
            and self.target_of(first)
        ):
            self.found.append(f"Line {node.lineno}: sys.modules.get('{first}')")

    def _reflective_getattr(self, node: ast.Call, func: ast.expr) -> None:
        if not (
            len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in self.root_aliases
        ):
            return
        attr = _const_str(node.args[1])
        base = self.root_aliases[node.args[0].id]
        if attr and self.target_of(f"{base}.{attr}"):
            label = "getattr" if isinstance(func, ast.Name) else ("builtins.getattr")
            self.found.append(
                f"Line {node.lineno}: {label}({node.args[0].id}, '{attr}')"
            )

    def visit_subscript(self, node: ast.Subscript) -> None:
        if self._is_sys_modules(node.value):
            key = _const_str(node.slice)
            if key and self.target_of(key):
                self.found.append(f"Line {node.lineno}: sys.modules['{key}']")

    def visit_compare(self, node: ast.Compare) -> None:
        left = _const_str(node.left)
        if (
            left
            and node.comparators
            and self._is_sys_modules(node.comparators[0])
            and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
            and self.target_of(left)
        ):
            self.found.append(
                f"Line {node.lineno}: membership check '{left}' in sys.modules"
            )

    def visit_attribute(self, node: ast.Attribute) -> None:
        root, attrs = _root_name_and_attrs(node)
        if root in self.root_aliases and attrs:
            # ``import sandloom`` + ``sandloom_identity`` / ``import sandloom as p`` +
            # ``p.identity`` / ``import helpdesk.modules`` + ``helpdesk.
            # modules.x`` — resolve the alias to the package it denotes.
            dotted = ".".join([self.root_aliases[root], *attrs])
            if self.target_of(dotted):
                self.found.append(
                    f"Line {node.lineno}: attribute access {root}."
                    f"{self.target_of(dotted)}"
                )


#: ``type(node)`` -> its :class:`_CrossModuleScan` visitor (AST node classes
#: are final, so an exact-type lookup equals the ``isinstance`` chain).
_CROSS_MODULE_VISITORS: dict[type[ast.AST], Callable[[_CrossModuleScan, Any], None]] = {
    ast.ImportFrom: _CrossModuleScan.visit_import_from,
    ast.Import: _CrossModuleScan.visit_import,
    ast.Call: _CrossModuleScan.visit_call,
    ast.Subscript: _CrossModuleScan.visit_subscript,
    ast.Compare: _CrossModuleScan.visit_compare,
    ast.Attribute: _CrossModuleScan.visit_attribute,
}


# --------------------------------------------------------------------------- #
# 2. Provider escape hatches (raw_client / raw_backend / raw)
# --------------------------------------------------------------------------- #

RAW_PROVIDER_METHODS = frozenset({"raw_client", "raw_backend", "raw"})


def find_raw_provider_calls(file_path: Path) -> list[str]:
    """Escape-hatch calls ``p.raw_client()`` — and the reflective spellings
    ``getattr(p, "raw_client")`` / ``operator.attrgetter("raw_client")``."""
    tree = parse_file(file_path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RAW_PROVIDER_METHODS
        ):
            violations.append(f"Line {node.lineno}: .{node.func.attr}(...)")  # noqa: PERF401 - cold path (startup / diagnostics)
    for line, _target, attr in _reflective_attr_names(tree):
        if attr in RAW_PROVIDER_METHODS:
            violations.append(f"Line {line}: reflective access to '{attr}'")
    return violations


# --------------------------------------------------------------------------- #
# 3. Legacy infra globals / infra internals / raw backend SDKs
# --------------------------------------------------------------------------- #

#: Infra-internal framework modules a business module must never import.
FORBIDDEN_INFRA_MODULES = frozenset(
    {
        "sandloom.core._internal",
        "sandloom.core.eventing.event_bus",
        "sandloom.core.storage",
        "sandloom.core.drivers",
        "sandloom.core.sql_alchemy.models",
        # The RLS hook internals are kernel-only. The privileged system
        # channel (sandloom.core.drivers.system_db) is covered by the drivers
        # prefix above: a business module never reaches the system engine
        # (docs/reference/security-model.md).
        "sandloom.core.sql_alchemy.tenant_rls",
        # For driver packages: a module asks for a capability and never
        # implements one. C002 misses it in the framework tree.
        "sandloom.driverkit",
    }
)
FORBIDDEN_FROM_CORE = frozenset({"get_db", "get_event_bus"})
FORBIDDEN_FROM_DI = frozenset({"get_cache"})
#: Concrete backend SDKs: reached only through a capability provider.
FORBIDDEN_BACKEND_ROOTS = frozenset(
    {"motor", "pymongo", "neo4j", "temporalio", "kafka", "boto3", "redis", "celery"}
)


def _is_forbidden_infra_module(module: str) -> bool:
    return any(
        module == m or module.startswith(m + ".") for m in FORBIDDEN_INFRA_MODULES
    )


def find_legacy_infra_imports(file_path: Path) -> list[str]:  # noqa: C901
    tree = parse_file(file_path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = ", ".join(a.name for a in node.names)
            root = node.module.split(".")[0]
            if (
                root in FORBIDDEN_BACKEND_ROOTS
                or _is_forbidden_infra_module(node.module)
                # ``from sandloom import driverkit`` names the module as an alias.
                or any(
                    _is_forbidden_infra_module(f"{node.module}.{a.name}")
                    for a in node.names
                )
            ):
                violations.append(
                    f"Line {node.lineno}: from {node.module} import {names}"
                )
            elif node.module == "sandloom.core":
                for alias in node.names:
                    if alias.name in FORBIDDEN_FROM_CORE:
                        violations.append(  # noqa: PERF401 - cold path (startup / diagnostics)
                            f"Line {node.lineno}: from sandloom.core import "
                            f"{alias.name}"
                        )
            elif node.module == "sandloom.core.di":
                for alias in node.names:
                    if alias.name in FORBIDDEN_FROM_DI:
                        violations.append(  # noqa: PERF401 - cold path (startup / diagnostics)
                            f"Line {node.lineno}: from sandloom.core.di import "
                            f"{alias.name}"
                        )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    _is_forbidden_infra_module(alias.name)
                    or alias.name.split(".")[0] in FORBIDDEN_BACKEND_ROOTS
                    # The bare package reaches every kernel module by attribute.
                    or alias.name == "sandloom"
                ):
                    violations.append(f"Line {node.lineno}: import {alias.name}")  # noqa: PERF401 - cold path (startup / diagnostics)
    # Dynamic imports of an infra-internal module (importlib / __import__,
    # under any alias) are the same bypass as a static import.
    for line, name, _text in dynamic_imports(tree):
        if (
            _is_forbidden_infra_module(name)
            or name.split(".")[0] in FORBIDDEN_BACKEND_ROOTS
            or name == "sandloom"
        ):
            violations.append(f"Line {line}: dynamic import of {name}")
    return violations


# --------------------------------------------------------------------------- #
# 3b. Kernel handles: no core.event_bus / .infra / .service_registry / state.core
# --------------------------------------------------------------------------- #

#: Attribute names that are kernel handles a business module must not touch.
#: At runtime a module gets the read-only ``CoreRuntime`` view (``get_core``)
#: and at registration the ``ModuleAPI``; neither exposes these, so reaching
#: them by attribute on whatever object a module holds is a bypass.
KERNEL_HANDLE_ATTRIBUTES = frozenset(
    {
        "event_bus",
        "infra",
        "service_registry",
        "set_event_bus",
        "set_infra",
        "set_service_resolver",
    }
)


def find_kernel_handle_access_in_source(source: str) -> list[str]:
    """Forbidden kernel-handle accesses in ``source`` (see :func:`find_...`)."""
    return _kernel_handle_hits(ast.parse(source))


def _kernel_handle_hits(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for line, target, attr in _reflective_attr_names(tree):
        # getattr(core, "event_bus") / attrgetter("infra") / getattr(state,
        # "core") — the reflective spellings of the attribute accesses below.
        if attr in KERNEL_HANDLE_ATTRIBUTES:
            violations.append(f"Line {line}: reflective access to '{attr}'")
        elif attr == "core" and (
            (isinstance(target, ast.Attribute) and target.attr == "state")
            or (isinstance(target, ast.Name) and target.id == "state")
        ):
            violations.append(f"Line {line}: reflective access to state.core")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr in KERNEL_HANDLE_ATTRIBUTES:
            violations.append(f"Line {node.lineno}: .{node.attr}")
        # request.app.state.core — the FULL facade behind get_core's view.
        if (
            node.attr == "core"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "state"
        ):
            violations.append(f"Line {node.lineno}: .state.core")
    return violations


def find_kernel_handle_access(file_path: Path) -> list[str]:
    """Kernel handles reached by attribute in a business-module file.

    Publish with ``publish_event`` (outbox), react with
    ``register_event_consumer``, resolve infrastructure with
    ``core.cache()/database()/capability()`` or the ``Capability`` dependency
    and contracts with ``core.get_service`` — never ``core.event_bus`` /
    ``core.infra`` / ``core.service_registry`` / ``app.state.core``. (Provider
    escape hatches such as ``.raw_client()`` are the ``raw-provider`` rule.)
    """
    return _kernel_handle_hits(parse_file(file_path))


# --------------------------------------------------------------------------- #
# 4. UoW boundary: business code never commits / rolls back
# --------------------------------------------------------------------------- #

UOW_FORBIDDEN_METHODS = frozenset({"commit", "rollback"})


def find_uow_boundary_breaks(file_path: Path) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(parse_file(file_path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in UOW_FORBIDDEN_METHODS
            and not node.args
            and not node.keywords
        ):
            violations.append(f"Line {node.lineno}: .{node.func.attr}()")  # noqa: PERF401 - cold path (startup / diagnostics)
    return violations


def find_unawaited_get_core(file_path: Path) -> list[str]:
    """Direct ``get_core(request)`` calls that are not awaited (0.6.0 break).

    ``sandloom.core.di.get_core`` is ``async`` since 0.6.0 (no thread-pool hop per
    request). ``Depends(get_core)`` is unchanged; a DIRECT call must be
    ``await get_core(request)`` — or use the sync accessor
    ``core_from_request(request)``. An un-awaited call returns a coroutine
    (and a "never awaited" warning) instead of the runtime.
    """
    tree = parse_file(file_path)
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in awaited:
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == "get_core" and (node.args or node.keywords):
            out.append(
                f"Line {node.lineno}: get_core(...) not awaited — use "
                "`await get_core(request)` or `core_from_request(request)`"
            )
    return out


#: Write-path effects whose ``required=False`` form is best-effort (0.7).
BEST_EFFORT_EFFECTS = frozenset(
    {"publish_event", "publish_event_versions", "record_audit"}
)

#: Comment that allows ONE best-effort call; it must carry a justification.
BEST_EFFORT_MARKER = "# sandloom: best-effort"


def _best_effort_effect_names(tree: ast.AST) -> dict[str, str]:
    """Local name -> effect it is bound to (``publish_event as emit``, ``emit =
    publish_event``, ``emit = di.publish_event``); the plain names map to
    themselves."""
    names = {n: n for n in BEST_EFFORT_EFFECTS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in BEST_EFFORT_EFFECTS and alias.asname:
                    names[alias.asname] = alias.name
    # One level of plain re-binding (``functools.partial`` is not followed).
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            value = node.value
            if isinstance(value, ast.Name) and value.id in names:
                names[node.targets[0].id] = names[value.id]
            elif isinstance(value, ast.Attribute) and value.attr in BEST_EFFORT_EFFECTS:
                names[node.targets[0].id] = value.attr
    return names


def find_unmarked_best_effort_effects(file_path: Path) -> list[str]:  # noqa: C901
    """``publish_event`` / ``record_audit`` downgraded to best-effort, unmarked.

    Since 0.7 both are GUARANTEED by default (outside a Unit of Work they
    raise). ``required=False`` — or any non-literal ``required=`` the analyzer
    cannot prove is ``True``, including one that may arrive through
    ``**kwargs`` — opts into best-effort: an event published straight to the
    bus (no outbox, no retry) or an audit record silently skipped. A business
    module may do that only with an explicit ``# sandloom: best-effort <reason>``
    comment on a line of the call or on the line directly above it. Aliased
    imports (``publish_event as emit``) and re-bound names count as the effect.
    """
    source, tree = _parsed(file_path)
    lines = source.splitlines()
    effects = _best_effort_effect_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = effects.get(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in BEST_EFFORT_EFFECTS:
            name = func.attr
        else:
            name = None
        if name is None:
            continue
        kw = next((k for k in node.keywords if k.arg == "required"), None)
        splat = next((k for k in node.keywords if k.arg is None), None)
        if kw is None and splat is None:
            continue
        if kw is not None and (
            isinstance(kw.value, ast.Constant) and kw.value.value is True
        ):
            continue
        first = max(node.lineno - 1, 1)
        last = node.end_lineno or node.lineno
        if any(_has_best_effort_marker(lines[i - 1]) for i in range(first, last + 1)):
            continue
        shown = func.id if isinstance(func, ast.Name) else name
        if shown != name:
            shown = f"{shown} (= {name})"
        if kw is not None:
            required = f"required={ast.unparse(kw.value)}"
        else:
            assert splat is not None  # noqa: S101 - type narrowing of an invariant
            required = f"**{ast.unparse(splat.value)}"
        out.append(
            f"Line {node.lineno}: {shown}(..., {required}) "
            f"is best-effort — drop it (guaranteed, needs a UoW) or justify it "
            f"with `{BEST_EFFORT_MARKER} <reason>`"
        )
    return out


def _has_best_effort_marker(line: str) -> bool:
    return _has_marker(line, BEST_EFFORT_MARKER)


def _has_marker(line: str, marker: str) -> bool:
    """``marker`` on ``line`` followed by a non-empty justification."""
    idx = line.find(marker)
    return idx >= 0 and bool(line[idx + len(marker) :].strip(" :-—"))


# --------------------------------------------------------------------------- #
# 5. Endpoints only in the API layer
# --------------------------------------------------------------------------- #


def find_endpoints_outside_api(
    file_path: Path, *, module_root: Path | None = None
) -> list[str]:
    """Endpoints defined in a non-API file (empty for API-layer files).

    ``module_root`` is the business module's directory; the API layer is
    judged on the path relative to it (see :func:`is_api_file`).
    """
    if is_api_file(file_path, module_root):
        return []
    try:
        tree = parse_file(file_path)
    except SyntaxError:
        return []
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            decs = [d for d in node.decorator_list if is_endpoint_decorator(d)]
            if decs:
                methods = sorted({m for m in map(endpoint_method, decs) if m})
                violations.append(
                    f"Line {node.lineno}: endpoint ({', '.join(methods) or 'router'})"
                    f" defined in non-API file: function '{node.name}'"
                )
    return violations


# --------------------------------------------------------------------------- #
# 6. No ORM / SQLAlchemy leakage into endpoint files
# --------------------------------------------------------------------------- #


def _is_models_module(name: str, roots: Sequence[str]) -> bool:
    return any(name.startswith(r + ".") for r in roots) and ".models" in name


def collect_models_imports(  # noqa: C901
    tree: ast.AST, *, model_roots: Sequence[str] = ("sandloom",)
) -> tuple[set[str], set[str], list[tuple[int, str]], list[tuple[int, str]]]:
    """``(models_aliases, imported_model_names, model_imports, sqlalchemy_imports)``.

    ``model_roots`` are the top-level packages whose ``.models`` count as ORM
    (``sandloom`` for the framework; an application adds its own package).
    """
    models_aliases: set[str] = set()
    imported_model_names: set[str] = set()
    import_violations: list[tuple[int, str]] = []
    sqlalchemy_violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                fullname = alias.name
                if fullname.startswith("sqlalchemy"):
                    sqlalchemy_violations.append((node.lineno, f"import {fullname}"))
                if _is_models_module(fullname, model_roots):
                    import_violations.append((node.lineno, f"import {fullname}"))
                    if alias.asname:
                        models_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("sqlalchemy"):
                target = ", ".join(a.asname or a.name for a in node.names)
                sqlalchemy_violations.append(
                    (node.lineno, f"from {mod} import {target}")
                )
            if _is_models_module(mod, model_roots):
                targets = ", ".join(a.asname or a.name for a in node.names)
                import_violations.append((node.lineno, f"from {mod} import {targets}"))
                for a in node.names:
                    imported_model_names.add(a.asname or a.name)
            if any(mod.startswith(r + ".") for r in model_roots) and (
                ".models" not in mod
            ):
                for a in node.names:
                    if a.name == "models":
                        models_aliases.add(a.asname or "models")
                        import_violations.append(
                            (node.lineno, f"from {mod} import {a.asname or a.name}")
                        )
            # Relative ``from ..models import X`` / ``from .. import models``.
            if node.level > 0:
                if (
                    mod == "models"
                    or mod.startswith("models.")
                    or (".models" in f".{mod}")
                ):
                    targets = ", ".join(a.asname or a.name for a in node.names)
                    import_violations.append(
                        (node.lineno, f"from {'.' * node.level}{mod} import {targets}")
                    )
                    for a in node.names:
                        imported_model_names.add(a.asname or a.name)
                elif not mod:
                    for a in node.names:
                        if a.name == "models":
                            models_aliases.add(a.asname or "models")
                            import_violations.append(
                                (node.lineno, f"from {'.' * node.level} import models")
                            )
    return (
        models_aliases,
        imported_model_names,
        import_violations,
        sqlalchemy_violations,
    )


def _annotation_uses_orm(  # noqa: C901
    expr: ast.AST, models_aliases: set[str], imported_model_names: set[str]
) -> tuple[bool, str]:
    if isinstance(expr, ast.Name):
        if expr.id in imported_model_names:
            return True, f"annotation references ORM model '{expr.id}'"
        return False, ""
    if isinstance(expr, ast.Attribute):
        root, attrs = _root_name_and_attrs(expr)
        if root in models_aliases:
            dotted = ".".join([root, *attrs])
            return True, f"annotation references ORM model via alias '{dotted}'"
        if root in imported_model_names:
            return True, f"annotation references ORM model '{root}'"
        return False, ""
    if isinstance(expr, ast.Subscript):
        ok, msg = _annotation_uses_orm(expr.value, models_aliases, imported_model_names)
        if ok:
            return ok, msg
        elts = expr.slice.elts if isinstance(expr.slice, ast.Tuple) else [expr.slice]
        for elt in elts:
            ok, msg = _annotation_uses_orm(elt, models_aliases, imported_model_names)
            if ok:
                return ok, msg
        return False, ""
    if isinstance(expr, ast.BinOp):  # ``Model | None``
        for side in (expr.left, expr.right):
            ok, msg = _annotation_uses_orm(side, models_aliases, imported_model_names)
            if ok:
                return ok, msg
        return False, ""
    if isinstance(expr, ast.Tuple | ast.List | ast.Set):
        for elt in expr.elts:
            ok, msg = _annotation_uses_orm(elt, models_aliases, imported_model_names)
            if ok:
                return ok, msg
    return False, ""


def scan_endpoint(  # noqa: C901
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    models_aliases: set[str],
    imported_model_names: set[str],
) -> list[str]:
    violations: list[str] = []
    for dec in func.decorator_list:
        if isinstance(dec, ast.Call) and is_endpoint_decorator(dec):
            for kw in dec.keywords or []:
                if kw.arg == "response_model":
                    ok, msg = _annotation_uses_orm(
                        kw.value, models_aliases, imported_model_names
                    )
                    if ok:
                        violations.append(f"Line {dec.lineno}: response_model {msg}")
    if func.returns:
        ok, msg = _annotation_uses_orm(
            func.returns, models_aliases, imported_model_names
        )
        if ok:
            violations.append(f"Line {func.lineno}: function return {msg}")
    args = [
        *getattr(func.args, "posonlyargs", []),
        *func.args.args,
        *func.args.kwonlyargs,
    ]
    if func.args.vararg:
        args.append(func.args.vararg)
    if func.args.kwarg:
        args.append(func.args.kwarg)
    for arg in args:
        if arg.annotation:
            ok, msg = _annotation_uses_orm(
                arg.annotation, models_aliases, imported_model_names
            )
            if ok:
                violations.append(f"Line {arg.lineno}: parameter {msg}")
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id in imported_model_names:
            violations.append(f"Line {node.lineno}: body uses ORM model '{node.id}'")
        elif isinstance(node, ast.Attribute):
            root, attrs = _root_name_and_attrs(node)
            if root in models_aliases and attrs:
                violations.append(
                    f"Line {node.lineno}: body uses ORM via alias '{root}.{attrs[0]}'"
                )
    return violations


def find_orm_leakage(
    file_path: Path, *, model_roots: Sequence[str] = ("sandloom",)
) -> list[str]:
    """ORM/SQLAlchemy leakage in a file that defines endpoints."""
    try:
        tree = parse_file(file_path)
    except SyntaxError:
        return []
    if not has_any_endpoint(tree):
        return []
    aliases, names, model_imports, sqla_imports = collect_models_imports(
        tree, model_roots=model_roots
    )
    violations = [
        f"Line {ln}: import of ORM models in endpoint file: {text}"
        for ln, text in model_imports
    ] + [
        f"Line {ln}: sqlalchemy import in endpoint file: {text}"
        for ln, text in sqla_imports
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
            is_endpoint_decorator(d) for d in node.decorator_list
        ):
            violations += scan_endpoint(node, aliases, names)
    return violations


# --------------------------------------------------------------------------- #
# 7. Schemas (DTOs) never import ORM / SQLAlchemy
# --------------------------------------------------------------------------- #


def _names_models(dotted: str) -> bool:
    return "models" in dotted.split(".")


def find_schema_orm_imports(file_path: Path) -> list[str]:
    """ORM / SQLAlchemy imports in a module's ``schemas`` (package or file).

    Every spelling of a ``models`` import counts: ``import x.models``,
    ``from x.models import M``, ``from x import models``, the relative
    ``from ..models import M`` / ``from .. import models``, and a dynamic
    import of a ``*.models`` module; so does any ``sqlalchemy`` import.
    """
    tree = parse_file(file_path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _names_models(alias.name) or alias.name.startswith("sqlalchemy"):
                    violations.append(f"Line {node.lineno}: import {alias.name}")  # noqa: PERF401 - cold path (startup / diagnostics)
        elif isinstance(node, ast.ImportFrom):
            names = ", ".join(a.name for a in node.names)
            mod = node.module or ""
            shown = f"{'.' * node.level}{mod}"
            if (
                _names_models(mod)
                or (node.level == 0 and mod.startswith("sqlalchemy"))
                or any(a.name == "models" for a in node.names)
            ):
                violations.append(f"Line {node.lineno}: from {shown} import {names}")
    for line, name, text in dynamic_imports(tree):
        if _names_models(name) or name.startswith("sqlalchemy"):
            violations.append(f"Line {line}: {text}")
    return violations


# --------------------------------------------------------------------------- #
# 8. Module tests reach the framework only through sandloom.core.testing
# --------------------------------------------------------------------------- #

#: Kernel internals a module's TESTS must not import: the harness
#: (``sandloom.core.testing`` — plugin fixtures, ``make_user_headers``, UoW helpers)
#: covers what they were used for, and it is the one surface the framework
#: keeps stable for tests.
FORBIDDEN_TEST_IMPORTS = frozenset(
    {
        "sandloom.core._internal",
        "sandloom.core.sql_alchemy",
        "sandloom.core.database",
        "sandloom.core.drivers",
    }
)


def _is_forbidden_test_import(module: str) -> bool:
    return any(
        module == m or module.startswith(m + ".") for m in FORBIDDEN_TEST_IMPORTS
    )


def kernel_internal_test_imports(file_path: Path) -> list[tuple[int, str, str]]:
    """``(line, imported module, source text)`` of each kernel-internal import.

    The context-free subset of the ``test-imports`` rule (no module context
    needed); ``sandloom check`` runs the whitelist :func:`module_test_import_hits`.

    Covers ``import x``, ``from x import y`` and ``from sandloom.core import
    database``-style spellings, at any nesting (function-local imports too).
    """
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(parse_file(file_path)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = ", ".join(a.name for a in node.names)
            text = f"from {node.module} import {names}"
            if _is_forbidden_test_import(node.module):
                hits.append((node.lineno, node.module, text))
                continue
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if _is_forbidden_test_import(dotted):
                    hits.append((node.lineno, dotted, text))
                    break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_test_import(alias.name):
                    hits.append((node.lineno, alias.name, (f"import {alias.name}")))  # noqa: PERF401 - cold path (startup / diagnostics)
    return hits


def find_kernel_internal_test_imports(file_path: Path) -> list[str]:
    """Kernel-internal imports in a module test file (or its conftest)."""
    return [
        f"Line {line}: {text}"
        for line, _m, text in kernel_internal_test_imports(file_path)
    ]


def _covers(module: str, prefixes: Iterable[str]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def module_test_import_hits(
    file_path: Path,
    *,
    own_package: str,
    allowed: Iterable[str],
    namespaces: Iterable[str] = ("sandloom",),
    source_root: Path | None = None,
) -> list[tuple[int, str, str]]:
    """``(line, imported module, source text)`` of each disallowed test import.

    WHITELIST, not a list of banned internals: an import inside one of the
    ``namespaces`` (``sandloom`` and the application's modules package) must be
    covered by ``allowed`` (public API + test API + shared API) or be the
    module's ``own_package``. So a module test reaches neither a kernel
    internal nor ANOTHER business module (cross-module imports are as
    forbidden in tests as in production code — tests build other modules'
    state through their contracts / events, or mint users via JWT). Static,
    relative and constant-string dynamic imports are all resolved.
    """
    scope = tuple(namespaces)
    permitted = (*allowed, own_package)
    refs = import_refs(parse_file(file_path), _package_of(file_path, source_root))
    return [
        (line, module, text)
        for line, module, text in refs
        if _covers(module, scope) and not _covers(module, permitted)
    ]


# --------------------------------------------------------------------------- #
# Import parsing for the manifest (public API / forbidden pattern) check
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Contract DTOs must live in a shared package (local == remote)
# --------------------------------------------------------------------------- #


def _is_contract_method_decorator(dec: ast.AST) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "contract_method"
    return isinstance(target, ast.Attribute) and target.attr == "contract_method"


def _annotation_roots(node: ast.AST | None) -> list[tuple[str, list[str]]]:
    """``(root_name, attrs)`` of every name an annotation expression uses."""
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return []
    if isinstance(node, ast.Attribute):
        root, attrs = _root_name_and_attrs(node)
        return [(root, attrs)] if root is not None else []
    if isinstance(node, ast.Name):
        return [(node.id, [])]
    roots: list[tuple[str, list[str]]] = []
    for child in ast.iter_child_nodes(node):
        roots += _annotation_roots(child)
    return roots


def find_contract_dto_leaks(
    file_path: Path,
    module_package: str,
    *,
    source_root: Path,
    generated: Path | None = None,
) -> list[str]:
    """Contract signatures naming a DTO of the module's OWN package that has
    no generated copy (SANDLOOM-C012).

    A provider authors its contract DTOs in its own package; ``sandloom
    generate`` copies each one a signature names into the shared DTO package
    (``generated``: the module's ``<shared>/contracts/<module>_dto.py``),
    where every caller imports the copy. A ``@contract`` class's public
    method or a ``@contract_method`` whose annotation names a model / Enum
    defined in this file or imported from ``module_package`` (absolute or
    relative import) is reported when ``generated`` lacks a class of that
    name: run ``sandloom generate``. Static mirror of the generated-files
    check, so ``sandloom check`` catches a missing copy early (without
    ``generated``, nothing is reported).
    """
    if generated is None:
        return []
    try:
        tree = parse_file(file_path)
    except SyntaxError:
        return []
    package = _dto_file_package(file_path, module_package, source_root)
    from_imports, module_aliases = _dto_import_bindings(tree, package)
    origins = _DtoOrigins(
        package=package,
        own=module_package,
        local_classes={
            node.name
            for node in getattr(tree, "body", [])
            if isinstance(node, ast.ClassDef)
        },
        from_imports=from_imports,
        module_aliases=module_aliases,
    )
    copies = _generated_classes(generated)
    violations: list[str] = []
    for node in _contract_signature_functions(tree):
        violations += _contract_method_dto_leaks(node, origins, copies, generated)
    return violations


def _generated_classes(path: Path) -> set[str]:
    """Class names a generated DTO module defines (empty when absent)."""
    try:
        tree = parse_file(path)
    except (OSError, SyntaxError):
        return set()
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def _is_contract_class_decorator(dec: ast.AST) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "contract"
    return isinstance(target, ast.Attribute) and target.attr == "contract"


def _contract_signature_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """``@contract_method`` functions and the public methods of ``@contract``
    classes (the L1 provider)."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(_is_contract_method_decorator(d) for d in node.decorator_list):
                found.append(node)
        elif isinstance(node, ast.ClassDef) and any(
            _is_contract_class_decorator(d) for d in node.decorator_list
        ):
            found += [
                item
                for item in node.body
                if isinstance(item, ast.AsyncFunctionDef)
                and not item.name.startswith("_")
                and item not in found
            ]
    return found


def _dto_file_package(
    file_path: Path, module_package: str, source_root: Path
) -> list[str]:
    """The package (as parts) ``file_path`` lives in; the module's if outside."""
    try:
        rel = file_path.relative_to(source_root)
        package = list(rel.with_suffix("").parts)[:-1]
        if file_path.name == "__init__.py":
            package = list(rel.parts[:-1])
    except ValueError:
        package = module_package.split(".")
    return package


def _dto_import_bindings(
    tree: ast.AST, package: list[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """``(from_imports, module_aliases)`` bound by the file's imports.

    ``from_imports``: name -> the module it comes from (``from m import X``);
    ``module_aliases``: name -> the module it IS (``import m as X``).
    """
    from_imports: dict[str, str] = {}
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = (
                resolve_relative(package, node.level, node.module)
                if node.level
                else node.module
            )
            if not source:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                from_imports[bound] = source
                module_aliases.setdefault(bound, f"{source}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module_aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    module_aliases[top] = top
    return from_imports, module_aliases


@dataclass(frozen=True)
class _DtoOrigins:
    """Where the names a contract-method annotation uses come from."""

    package: list[str]
    own: str
    local_classes: set[str]
    from_imports: dict[str, str]
    module_aliases: dict[str, str]

    def owned(self, dotted: str | None) -> bool:
        own = self.own
        return bool(dotted) and (dotted == own or str(dotted).startswith(own + "."))

    def origin(self, root: str, attrs: list[str]) -> str | None:
        """Why ``root.attrs`` is the module's OWN class, else ``None``."""
        if not attrs and root in self.local_classes:
            return f"defined in {'.'.join(self.package)}"
        if (
            not attrs
            and root in self.from_imports
            and self.owned(self.from_imports[root])
        ):
            return f"imported from {self.from_imports[root]}"
        if attrs and root in self.module_aliases:
            dotted = ".".join([self.module_aliases[root], *attrs[:-1]])
            if self.owned(dotted):
                return f"imported from {dotted}"
        return None


def _contract_method_dto_leaks(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    origins: _DtoOrigins,
    copies: set[str],
    generated: Path,
) -> list[str]:
    all_args = [
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ]
    annotations = [(a.arg, a.annotation) for a in all_args]
    annotations.append(("return", node.returns))
    reported: set[str] = set()
    violations: list[str] = []
    for position, ann in annotations:
        for root, attrs in _annotation_roots(ann):
            origin = origins.origin(root, attrs)
            if origin is None:
                continue
            shown = ".".join([root, *attrs])
            if shown in reported or (attrs[-1] if attrs else root) in copies:
                continue
            reported.add(shown)
            violations.append(
                f"Line {node.lineno}: contract method '{node.name}' "
                f"({position}) uses {shown} {origin}, which has no generated "
                f"copy in {generated.name} (callers import the copy): run "
                "`sandloom generate`"
            )
    return violations


def resolve_relative(package: list[str], level: int, module: str | None) -> str | None:
    """Resolve ``from <dots><module> import ...`` to an absolute module name."""
    if level - 1 > len(package):
        return None
    anchor = package[: len(package) - (level - 1)]
    if module:
        anchor = anchor + module.split(".")
    return ".".join(anchor) if anchor else None


def _package_of(py_path: Path, source_root: Path | None) -> list[str]:
    """The dotted package (as parts) ``py_path`` lives in under ``source_root``."""
    if source_root is None:
        return []
    try:
        rel = py_path.relative_to(source_root)
    except ValueError:
        return []
    # A module and a package's ``__init__`` both live in their directory.
    return list(rel.parts[:-1])


def find_unsafe_tenant_sql(file_path: Path) -> list[str]:
    """Runtime Core SQL requires explicit trusted ``unscoped()`` admission.

    Class/module-level SQL declarations (indexes/server defaults) are not
    runtime queries and need no tenant context.
    """
    tree = parse_file(file_path)
    sql_text_names, sqlalchemy_names, unscoped_names = _tenant_sql_import_names(tree)
    found: list[str] = []
    _TenantSqlVisitor(sql_text_names, sqlalchemy_names, unscoped_names, found).visit(
        tree
    )
    return found


def _tenant_sql_import_names(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Names bound to ``sqlalchemy.text``, ``sqlalchemy`` and ``unscoped``."""
    sql_text_names: set[str] = set()
    sqlalchemy_names: set[str] = set()
    unscoped_names = {"unscoped"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy":
                    sqlalchemy_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (node.module or "").startswith(
                    "sqlalchemy"
                ) and alias.name == "text":
                    sql_text_names.add(alias.asname or alias.name)
                if alias.name == "unscoped":
                    unscoped_names.add(alias.asname or alias.name)
    return sql_text_names, sqlalchemy_names, unscoped_names


class _TenantSqlVisitor(ast.NodeVisitor):
    """Runtime raw SQL / Core table access outside an ``unscoped()`` block."""

    def __init__(
        self,
        sql_text_names: set[str],
        sqlalchemy_names: set[str],
        unscoped_names: set[str],
        found: list[str],
    ) -> None:
        self.sql_text_names = sql_text_names
        self.sqlalchemy_names = sqlalchemy_names
        self.unscoped_names = unscoped_names
        self.found = found
        self.function_depth = 0
        self.system_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        old_system = self.system_depth
        self.system_depth = 0
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1
        self.system_depth = old_system

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Defaults execute when the function is created; its body may run
        # after the surrounding unscoped() context has already exited.
        self.visit(node.args)
        old_system = self.system_depth
        self.system_depth = 0
        self.function_depth += 1
        self.visit(node.body)
        self.function_depth -= 1
        self.system_depth = old_system

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        # Only the outer iterable is evaluated at generator creation.
        # Element/filters/inner iterables are deferred until consumption.
        first, *remaining = node.generators
        self.visit(first.iter)
        old_system = self.system_depth
        self.system_depth = 0
        self.function_depth += 1
        self.visit(first.target)
        for condition in first.ifs:
            self.visit(condition)
        for generator in remaining:
            self.visit(generator)
        self.visit(node.elt)
        self.function_depth -= 1
        self.system_depth = old_system

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        old_system = self.system_depth
        for item in node.items:
            # Context expressions run left-to-right, before that context
            # is entered. Earlier contexts already protect later ones.
            self.visit(item.context_expr)
            system = (
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id in self.unscoped_names
            )
            self.system_depth += int(system)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        for statement in node.body:
            self.visit(statement)
        self.system_depth = old_system

    visit_AsyncWith = visit_With

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        raw_text = isinstance(fn, ast.Name) and fn.id in self.sql_text_names
        raw_method = isinstance(fn, ast.Attribute) and (
            fn.attr == "exec_driver_sql"
            or (
                fn.attr == "text"
                and isinstance(fn.value, ast.Name)
                and fn.value.id in self.sqlalchemy_names
            )
        )
        if self.function_depth and not self.system_depth and (raw_text or raw_method):
            self.found.append(
                f"Line {node.lineno}: raw SQL requires explicit "
                "unscoped() system access"
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.function_depth and not self.system_depth and node.attr == "__table__":
            self.found.append(
                f"Line {node.lineno}: Core table access requires explicit "
                "unscoped() system access"
            )
        self.generic_visit(node)


def import_refs(tree: ast.AST, package: Sequence[str]) -> list[tuple[int, str, str]]:
    """``(line, absolute module, source text)`` of every import in ``tree``.

    ``from m import a, b`` yields ``m.a`` and ``m.b`` — the analyzer cannot
    tell a submodule from a symbol, and ``from sandloom import identity`` must
    resolve to ``sandloom_identity``, not to the bare namespace ``sandloom`` (a star
    import yields ``m``). Relative imports are resolved against ``package``;
    constant-string dynamic imports (:func:`dynamic_imports`) are included.
    """
    refs: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.extend(
                (node.lineno, a.name, f"import {a.name}") for a in node.names if a.name
            )
        elif isinstance(node, ast.ImportFrom):
            base = (
                resolve_relative(list(package), node.level, node.module)
                if node.level
                else node.module
            )
            if not base:
                continue
            names = ", ".join(a.name for a in node.names)
            text = f"from {'.' * node.level}{node.module or ''} import {names}"
            for alias in node.names:
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                refs.append((node.lineno, target, text))
    refs.extend(dynamic_imports(tree))
    return refs


def parse_imports(py_path: Path, *, source_root: Path) -> list[str]:
    """Absolute module names a file imports (see :func:`import_refs`)."""
    try:
        tree = parse_file(py_path)
    except Exception:  # noqa: BLE001 - unparsable file contributes no imports
        return []
    return [m for _l, m, _t in import_refs(tree, _package_of(py_path, source_root))]


#: The mixin that declares ``tenant_id`` AND opts a model into the tenant guard.
TENANT_SCOPED_MIXIN = "TenantScoped"


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):  # Generic[...]-style bases
        return _base_name(node.value)
    return None


def _assigned_names(stmt: ast.stmt) -> list[str]:
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    return []


def _class_assignment(cls: ast.ClassDef, name: str) -> ast.stmt | None:
    for stmt in cls.body:
        if name in _assigned_names(stmt):
            return stmt
    return None


def _call_name(call: ast.Call) -> str | None:
    return _base_name(call.func)


#: Column names that mark a table as holding per-tenant rows.
TENANT_LIKE_COLUMNS = ("tenant_id", "organization_id", "org_id", "workspace_id")


def tenant_model_hits(file_path: Path) -> list[tuple[int, str, str | None]]:
    """``(line, name, table)`` of per-tenant tables outside ``TenantScoped``.

    See :func:`tenant_column_hits` (this drops the column name).
    """
    return [
        (line, name, table) for line, name, table, _ in tenant_column_hits(file_path)
    ]


def tenant_column_hits(  # noqa: C901
    file_path: Path,
) -> list[tuple[int, str, str | None, str]]:
    """``(line, name, table, column)`` of per-tenant tables outside ``TenantScoped``.

    An ORM model (a class assigning ``__tablename__`` / ``__table__``) that
    declares a tenant-like column (:data:`TENANT_LIKE_COLUMNS`: ``tenant_id``,
    ``organization_id``, ``org_id``, ``workspace_id``) — itself or through a
    mixin defined in the same file — but does not inherit ``TenantScoped``
    (directly or through a same-file base): the ORM tenant guard never filters
    it, so one forgotten ``where(organization_id == ...)`` leaks across
    tenants. Also every Core ``Table(name, metadata, Column("<tenant-like>",
    ...), ...)`` declaration (a per-tenant table the ORM guard cannot see at
    all). ``table`` is the literal table name, or ``None`` when it is not a
    string literal.
    """
    tree = parse_file(file_path)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    by_name = {c.name: c for c in classes}

    def bases(cls: ast.ClassDef) -> list[str]:
        return [b for b in (_base_name(x) for x in cls.bases) if b]

    def inherits(
        cls: ast.ClassDef,
        pred: Callable[[ast.ClassDef], bool],
        seen: frozenset[str],
    ) -> bool:
        if pred(cls):
            return True
        for base in bases(cls):
            local = by_name.get(base)
            if (
                local is not None
                and base not in seen
                and inherits(local, pred, seen | {base})
            ):
                return True
        return False

    def scoped(cls: ast.ClassDef) -> bool:
        return inherits(
            cls, lambda c: TENANT_SCOPED_MIXIN in bases(c), frozenset({cls.name})
        )

    def tenant_column(cls: ast.ClassDef) -> str | None:
        for column in TENANT_LIKE_COLUMNS:
            if inherits(
                cls,
                lambda c, col=column: _class_assignment(c, col) is not None,  # type: ignore[misc]
                frozenset({cls.name}),
            ):
                return column
        return None

    hits: list[tuple[int, str, str | None, str]] = []
    for cls in classes:
        table_stmt = _class_assignment(cls, "__tablename__") or _class_assignment(
            cls, "__table__"
        )
        if table_stmt is None or scoped(cls):
            continue
        column = tenant_column(cls)
        if column is None:
            continue
        value = getattr(table_stmt, "value", None)
        hits.append((cls.lineno, cls.name, _const_str(value), column))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "Table"):
            continue
        for arg in node.args[1:]:
            if (
                isinstance(arg, ast.Call)
                and _call_name(arg) in {"Column", "mapped_column"}
                and arg.args
                and _const_str(arg.args[0]) in TENANT_LIKE_COLUMNS
            ):
                table = _const_str(node.args[0]) if node.args else None
                column = _const_str(arg.args[0]) or "tenant_id"
                hits.append((node.lineno, f"Table({table!r})", table, column))
                break
    return sorted(hits)


__all__ = [
    "API_DIR_HINTS",
    "BEST_EFFORT_EFFECTS",
    "BEST_EFFORT_MARKER",
    "API_FILE_HINTS",
    "FORBIDDEN_BACKEND_ROOTS",
    "FORBIDDEN_FROM_CORE",
    "FORBIDDEN_FROM_DI",
    "FORBIDDEN_INFRA_MODULES",
    "FORBIDDEN_TEST_IMPORTS",
    "HTTP_METHODS",
    "RAW_PROVIDER_METHODS",
    "TENANT_LIKE_COLUMNS",
    "TENANT_SCOPED_MIXIN",
    "UOW_FORBIDDEN_METHODS",
    "collect_models_imports",
    "dynamic_imports",
    "import_refs",
    "find_unsafe_tenant_sql",
    "module_test_import_hits",
    "read_source",
    "endpoint_method",
    "find_contract_dto_leaks",
    "find_cross_module_imports",
    "find_endpoints_outside_api",
    "find_legacy_infra_imports",
    "find_kernel_handle_access",
    "find_kernel_handle_access_in_source",
    "find_kernel_internal_test_imports",
    "kernel_internal_test_imports",
    "find_orm_leakage",
    "find_raw_provider_calls",
    "find_schema_orm_imports",
    "find_unawaited_get_core",
    "find_unmarked_best_effort_effects",
    "find_uow_boundary_breaks",
    "has_any_endpoint",
    "is_api_file",
    "is_endpoint_decorator",
    "parse_file",
    "parse_imports",
    "resolve_relative",
    "scan_endpoint",
    "tenant_column_hits",
    "tenant_model_hits",
]
