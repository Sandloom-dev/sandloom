"""Helpers for the hidden DX-eval checkers (static: they never import the app).

A checker runs AFTER the agent finished. Tests named ``test_fn_*`` check that
the feature exists at all; ``test_dev_*`` check a rule the framework states
but no gate enforces — a failure there while the gates are green is a
"deviation no check caught".

Two layouts, one checker:

* ``--mode framework``: the agent's worktree of the framework repository
  (``DX_WORKTREE``) and the commit it started from (``DX_BASE``); modules are
  ``src/sandloom/<name>``.
* ``--mode app``: an application the agent generated with ``sandloom new``
  (``DX_APP``, its root); modules are ``<app>/modules/<name>``, and the runner
  lists the changed paths (relative to the app root) in ``DX_CHANGED``.

Modules are located through the project's own ``architecture_rules.yml``
(``modules_path`` / ``modules_package`` / ``source_root``), so a checker
written as ``module_dir("notes")`` works in both.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

HTTP_VERBS = {"get", "post", "put", "patch", "delete"}
MUTATING = {"post", "put", "patch", "delete"}


def app_mode() -> bool:
    """The agent built an ``sandloom new`` application (``DX_APP``)."""
    return bool(os.environ.get("DX_APP"))


def pick(*, framework: str, app: str) -> str:
    """A name that differs per mode (e.g. the module a task extends)."""
    return app if app_mode() else framework


def worktree() -> Path:
    """The root the changed paths are relative to: the framework worktree, or
    the application's root in app mode."""
    if app_mode():
        return Path(os.environ["DX_APP"]).resolve()
    return Path(os.environ["DX_WORKTREE"]).resolve()


def project_root() -> Path:
    """The directory holding ``architecture_rules.yml``."""
    return worktree() if app_mode() else worktree()


@cache
def _rules() -> dict[str, str]:
    """Top-level scalar keys of ``architecture_rules.yml`` (no YAML dependency)."""
    rules = project_root() / "architecture_rules.yml"
    found: dict[str, str] = {}
    if rules.is_file():
        for m in re.finditer(r"^([a-z_]+):[ \t]*([^\s#]+)", rules.read_text(), re.M):
            found[m.group(1)] = m.group(2).strip("\"'")
    return found


def modules_root() -> Path:
    """``modules_path``: ``src/sandloom`` or ``<app>/modules``."""
    return project_root() / _rules().get("modules_path", "src/sandloom")


def source_root() -> Path:
    return project_root() / _rules().get("source_root", "src")


def root_package() -> str:
    """The top-level package modules live in: ``sandloom`` or ``<app>``."""
    return _rules().get("modules_package", "sandloom").split(".")[0]


def contracts_package() -> str:
    """Where ``sandloom test`` writes contract stubs: ``sandloom.shared.contracts`` or
    ``<app>.shared.contracts``."""
    return f"{root_package()}.shared.contracts"


def module_dir(name: str) -> Path:
    return modules_root() / name


def module_package(name: str) -> str:
    """``name``'s dotted package: ``sandloom.<name>`` or ``<app>.modules.<name>``."""
    return f"{_rules().get('modules_package', 'sandloom')}.{name}"


def module_prefix(name: str, sub: str = "") -> str:
    """``name``'s package (or ``sub`` inside it) as a changed-path prefix."""
    rel = module_dir(name).relative_to(worktree()).as_posix()
    return f"{rel}/{sub.strip('/')}/" if sub else f"{rel}/"


def deploy_dir() -> Path:
    """The deploy profiles: ``deploy/`` of the repository or of the app."""
    return worktree() / "deploy"


def source_files(root: Path, *, tests: bool = False) -> list[Path]:
    if not root.exists():
        return []
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    if not tests:
        files = [p for p in files if "tests" not in p.relative_to(root).parts]
    return files


def text(root: Path, *, tests: bool = False) -> str:
    return "\n".join(
        p.read_text(errors="replace") for p in source_files(root, tests=tests)
    )


def module_tests(root: Path) -> list[Path]:
    return [p for p in source_files(root, tests=True) if p.name.startswith("test_")]


@cache
def changed_files() -> tuple[str, ...]:
    """Paths changed vs ``DX_BASE`` (tracked diff + untracked files); in app
    mode the runner's list (``DX_CHANGED``: one path per line, relative to the
    app root, changes against the app ``sandloom new`` generated)."""
    listed = os.environ.get("DX_CHANGED")
    if listed:
        lines = Path(listed).read_text().splitlines()
        return tuple(sorted({line.strip() for line in lines if line.strip()}))
    base = os.environ.get("DX_BASE", "HEAD")
    wt = worktree()
    tracked = subprocess.run(
        ["git", "diff", "--name-only", base],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return tuple(sorted(set(tracked) | set(untracked)))


def changed_under(prefix: str) -> list[str]:
    return [p for p in changed_files() if p.startswith(prefix)]


@dataclass
class Route:
    method: str
    path: str
    function: str
    file: Path
    text: str  # decorator + signature + the router's own definition + body
    head: str = ""  # decorator + signature + the router's own definition


@dataclass
class Model:
    name: str
    bases: list[str]
    table: str | None
    columns: list[str] = field(default_factory=list)
    file: Path | None = None


def _seg(src: str, node: ast.AST) -> str:
    return ast.get_source_segment(src, node) or ""


def routes(root: Path) -> list[Route]:
    found: list[Route] = []
    for path in source_files(root):
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        routers: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        routers[target.id] = _seg(src, node.value)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not (
                    isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                ):
                    continue
                verb = deco.func.attr
                if verb not in HTTP_VERBS:
                    continue
                owner = deco.func.value
                router_src = (
                    routers.get(owner.id, "") if isinstance(owner, ast.Name) else ""
                )
                route_path = ""
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    route_path = str(deco.args[0].value)
                signature = _seg(src, node.args)
                body = _seg(src, node)
                head = "\n".join((_seg(src, deco), signature, router_src))
                found.append(
                    Route(
                        method=verb.upper(),
                        path=route_path,
                        function=node.name,
                        file=path,
                        text="\n".join((head, body)),
                        head=head,
                    )
                )
    return found


def mutating_routes(root: Path) -> list[Route]:
    return [r for r in routes(root) if r.method.lower() in MUTATING]


def models(root: Path) -> list[Model]:
    found: list[Model] = []
    for path in source_files(root):
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [_seg(src, b) for b in node.bases]
            table = None
            columns: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                    if "__tablename__" in names and isinstance(
                        stmt.value, ast.Constant
                    ):
                        table = str(stmt.value.value)
                    elif names and re.search(
                        r"\b(Column|mapped_column)\(", _seg(src, stmt.value)
                    ):
                        columns.extend(names)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    if "Mapped" in _seg(src, stmt.annotation):
                        columns.append(stmt.target.id)
            if table or columns:
                found.append(Model(node.name, bases, table, columns, path))
    return found


TENANT_LIKE = re.compile(r"^(tenant_id|organization_id|org_id|workspace_id)$")


def tenant_like_models(root: Path) -> list[Model]:
    return [m for m in models(root) if any(TENANT_LIKE.match(c) for c in m.columns)]


def is_tenant_scoped(model: Model) -> bool:
    return any("TenantScoped" in b for b in model.bases)


def exempt_tables() -> str:
    rules = project_root() / "architecture_rules.yml"
    return rules.read_text() if rules.exists() else ""


def migrations_text(root: Path) -> str:
    mig = root / "migrations"
    if not mig.exists():
        return ""
    return "\n".join(p.read_text(errors="replace") for p in mig.rglob("*.py"))


# --- Public API 1.0 (sandloom.sdk) ---------------------------------------------- #
#
# Model E (ADR-0009) derives a route's policy: authentication is the default
# of module_router(), keyed retries of a write replay their status
# automatically. What an author can still get wrong is an opt-out.


def opts_out_of_idempotency(route: Route) -> bool:
    """The route turns the automatic retry dedupe off (``no_idempotency``)."""
    return "no_idempotency(" in route.text


def is_public(route: Route) -> bool:
    """The route declares itself unauthenticated (``public(reason=...)``)."""
    return re.search(r"(?<![\w.])public\(", route.text) is not None


def keeps_route_defaults(route: Route) -> bool:
    """A write route that stays authenticated and keeps automatic idempotency."""
    return not is_public(route) and not opts_out_of_idempotency(route)


def route_snapshot(root: Path) -> list[dict]:
    """The module's resolved route policies (``routes.snapshot.json``, written
    by ``sandloom test`` / ``sandloom routes snapshot --write`` and checked by the gates)."""
    path = root / "routes.snapshot.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    found = data.get("routes", []) if isinstance(data, dict) else []
    return [r for r in found if isinstance(r, dict)]


def automatically_audited(route: Route, root: Path) -> bool:
    """The framework records the route's ``sandloom.sensitive_write`` audit row
    itself: its resolved policy carries a risk (an admin gate, money,
    security) and shows ``"audit": "automatic"``."""
    if not route.path:
        return False
    for entry in route_snapshot(root):
        policy = entry.get("policy") or {}
        if (
            entry.get("method") == route.method
            and str(entry.get("path", "")).endswith(route.path)
            and policy.get("risk")
            and policy.get("audit") == "automatic"
        ):
            return True
    return False


def audited_or_evented(routes_: list[Route], root: Path) -> bool:
    """Every route leaves a trace: an explicit ``record_audit`` or event
    publish in the module, or the framework's automatic audit row."""
    src = text(root)
    if "record_audit(" in src or ".publish(" in src or "publish_event(" in src:
        return True
    return bool(routes_) and all(automatically_audited(r, root) for r in routes_)


TRACES = ("record_audit(", ".publish(", "publish_event(", "publish_event_versions(")


def function_sources(root: Path) -> dict[str, list[str]]:
    """Every function / method of ``root``'s sources: name -> its source(s)."""
    found: dict[str, list[str]] = {}
    for path in source_files(root):
        parsed = _parse(path)
        if parsed is None:
            continue
        src, tree = parsed
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(node.name, []).append(_seg(src, node))
    return found


def reaches(route: Route, root: Path, needles: tuple[str, ...], depth: int = 4) -> bool:
    """The route's body, or a function of ``root`` it calls (by name,
    transitively up to ``depth`` calls), contains one of ``needles``: what
    one route does, not what some other code of the module does."""
    funcs = function_sources(root)
    seen: set[str] = set()
    frontier = [route.text]
    for _ in range(depth + 1):
        if any(n in blob for blob in frontier for n in needles):
            return True
        called = {
            name
            for blob in frontier
            for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", blob)
            if name in funcs and name not in seen
        }
        if not called:
            return False
        seen |= called
        frontier = [s for name in sorted(called) for s in funcs[name]]
    return False


def route_leaves_trace(route: Route, root: Path) -> bool:
    """This route is audited or evented: the framework's automatic audit row,
    or an explicit audit / publish on its own code path (``reaches``)."""
    return automatically_audited(route, root) or reaches(route, root, TRACES)


def _parse(path: Path) -> tuple[str, ast.Module] | None:
    src = path.read_text(errors="replace")
    try:
        return src, ast.parse(src)
    except SyntaxError:
        return None


def _top_level_assigns(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    """Module-level ``NAME = value`` / ``NAME: T = value``: (name, value)."""
    out: list[tuple[str, ast.expr]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            out += [(t.id, stmt.value) for t in stmt.targets if isinstance(t, ast.Name)]
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            out.append((stmt.target.id, stmt.value))
    return out


def _file_strings(path: Path) -> dict[str, str]:
    """Module-level string constants of one file (``NAME = "..."``)."""
    parsed = _parse(path)
    if parsed is None:
        return {}
    return {
        name: value.value
        for name, value in _top_level_assigns(parsed[1])
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


def _import_target(path: Path, module: str | None, level: int) -> Path | None:
    """The source file ``from <module> import ...`` in ``path`` names: relative,
    or absolute inside the project's own package (``sandloom.*`` / ``<app>.*``)."""
    if level:
        base = path.parent
        for _ in range(level - 1):
            base = base.parent
    elif module and module.startswith(root_package() + "."):
        base = source_root()
    else:
        return None
    target = base.joinpath(*(module.split(".") if module else []))
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def string_constants(root: Path) -> dict[str, str]:
    """Every module-level string constant of ``root``'s sources: name -> value."""
    found: dict[str, str] = {}
    for path in source_files(root):
        found.update(_file_strings(path))
    return found


def _imported_string(
    tree: ast.Module, path: Path, local: str, attr: str | None
) -> str | None:
    """``local`` imported with ``from X import local`` (``attr`` None: a constant
    of X) or as a sibling module (``attr``: that module's constant)."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.ImportFrom):
            continue
        for alias in stmt.names:
            if (alias.asname or alias.name) != local:
                continue
            if attr is None:
                target = _import_target(path, stmt.module, stmt.level)
                key = alias.name
            else:
                sub = f"{stmt.module}.{alias.name}" if stmt.module else alias.name
                target = _import_target(path, sub, stmt.level)
                key = attr
            if target is not None and key in _file_strings(target):
                return _file_strings(target)[key]
    return None


def resolve_string(
    node: ast.expr, path: Path, tree: ast.Module, root: Path
) -> str | None:
    """A string literal, or ``NAME`` / ``mod.NAME`` resolved through module-level
    string constants: the same file, then the file it is imported from, then
    any file of ``root``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        local = _file_strings(path)
        if node.id in local:
            return local[node.id]
        value = _imported_string(tree, path, node.id, None)
        return value if value is not None else string_constants(root).get(node.id)
    if isinstance(node, ast.Attribute):
        value = None
        if isinstance(node.value, ast.Name):
            value = _imported_string(tree, path, node.value.id, node.attr)
        return value if value is not None else string_constants(root).get(node.attr)
    return None


def event_classes(root: Path) -> dict[str, str]:
    """``class X(Event, channel=...)`` declarations: class name -> channel.

    The channel is a string literal or a module-level constant
    (``channel=CREDITS_SPENT_V1`` with ``CREDITS_SPENT_V1 = "billing..."``),
    in the same file or imported from a sibling module."""
    found: dict[str, str] = {}
    for path in source_files(root):
        parsed = _parse(path)
        if parsed is None:
            continue
        src, tree = parsed
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any("Event" in _seg(src, b) for b in node.bases):
                continue
            for kw in node.keywords:
                if kw.arg == "channel":
                    channel = resolve_string(kw.value, path, tree, root)
                    if channel is not None:
                        found[node.name] = channel
    return found


def definitions(root: Path) -> dict[str, str]:
    """Module-level assignments of ``root``'s sources: name -> value source
    (policy constants such as ``PAID = Subscribed(...)`` or ``GATE = A & B``,
    string constants)."""
    found: dict[str, str] = {}
    for path in source_files(root):
        parsed = _parse(path)
        if parsed is None:
            continue
        src, tree = parsed
        for name, value in _top_level_assigns(tree):
            found[name] = _seg(src, value)
    return found


def expanded_head(route: Route, root: Path) -> str:
    """The route's decorator, signature and router, plus the definitions of
    every module-level constant they name, transitively: ``require(GATE)``
    with ``GATE = MEMBER & PAID`` and ``PAID = Subscribed(...)`` expands to
    text that contains ``Subscribed(``."""
    defs = definitions(root)
    seen: set[str] = set()
    frontier = route.head or route.text
    parts = [frontier]
    while True:
        names = {n for n in re.findall(r"\b[A-Za-z_]\w*\b", frontier) if n in defs}
        names -= seen
        if not names:
            return "\n".join(parts)
        seen |= names
        frontier = "\n".join(f"{n} = {defs[n]}" for n in sorted(names))
        parts.append(frontier)


def consumers(root: Path) -> list[tuple[str, str]]:
    """``@on(E)`` handlers: (function name, decorator source)."""
    found: list[tuple[str, str]] = []
    for path in source_files(root):
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Name)
                    and deco.func.id == "on"
                ):
                    found.append((node.name, _seg(src, deco)))
    return found


def contract_callers(root: Path) -> list[str]:
    """Module-level ``x = Contract(REF[, optional=True])`` declarations (source)."""
    return re.findall(r"^\w+\s*(?::[^=\n]+)?=\s*Contract\([^\n]*", text(root), re.M)


def shared_contract_stubs() -> list[Path]:
    """Generated contract stubs (written by ``sandloom test``): ``sandloom/shared/contracts``
    of the framework tree, or ``<app>/shared/contracts`` of an app."""
    stubs = source_root().joinpath(*contracts_package().split("."))
    return sorted(stubs.glob("*.py")) if stubs.exists() else []
