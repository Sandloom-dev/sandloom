"""Module-level rules that need more than one file: routes, events, contracts.

The analyzers in :mod:`sandloom.core.archcheck.analyzers` look at one file at a
time. These look at a whole business module (its ``events.py``, its
``define_module(..., consumes=[...])``), still purely statically:

* ``event-schema-channels`` (SANDLOOM-C017) — a channel passed to
  ``publish_event`` is a key of the module's ``EVENT_SCHEMAS`` (or registered
  with ``register_event_schema``);
* ``guard-order`` (SANDLOOM-C018) — an ``idempotency_guard`` or a gate that needs
  an organisation is not declared before the authentication / ``TenantScope()``
  it relies on, where the order is visible in one route declaration;
* ``consumed-contracts`` (SANDLOOM-C019) — every literal ``get_service("x.y.vN")``
  is declared in ``consumes=`` (or provided by the module itself);
* ``db-dependency-scope`` (SANDLOOM-C020) — ``Depends(get_db...)`` carries
  ``scope="function"``.

Each analyzer returns ``(file, "Line N: message")`` pairs. They only report
what they can decide: an unresolvable name (a list built elsewhere, a channel
computed at runtime) is never a violation. A deliberate exception is waived
on its line with ``# sandloom: allow <rule-id> <reason>`` (the runner honours it
for every rule).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from sandloom.core.archcheck.analyzers import (
    HTTP_METHODS,
    _const_str,
    _root_name_and_attrs,
    is_endpoint_decorator,
    parse_file,
)

Hit = tuple[Path, str]

#: Dependency callables that authenticate the caller (statically, by name).
AUTH_NAMES = frozenset(
    {
        "get_current_user_id",
        "get_current_user",
        "current_context",
        "current_user_id",
        "require_user",
        "require_operator",
    }
)
#: ``require(...)`` atoms that always resolve an organisation (path param,
#: ``X-Tenant-Id`` header, else the bound tenant).
ORG_ATOMS = frozenset({"OrgRole"})
#: Billing atoms that need an organisation unless ``of=Account.user()``.
ORG_IF_ORGANIZATION = frozenset(
    {"Subscribed", "SubscriptionActive", "Credits", "Feature", "WithinLimit"}
)
_GATE_ATOMS = ORG_ATOMS | ORG_IF_ORGANIZATION


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _names_called(node: ast.AST) -> set[str]:
    return {name for n in ast.walk(node) if (name := _call_name(n))}


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _module_constants(tree: ast.AST) -> dict[str, ast.expr]:
    """Module-level ``NAME = <expr>`` / ``NAME: T = <expr>`` assignments."""
    out: dict[str, ast.expr] = {}
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            out[stmt.target.id] = stmt.value
    return out


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _trees(files: Iterable[Path]) -> Iterator[tuple[Path, ast.AST]]:
    """``(file, tree)`` of every parsable file (a syntax error is skipped)."""
    for py in files:
        try:
            yield py, parse_file(py)
        except SyntaxError:
            continue


def _string_constants(tree: ast.AST) -> dict[str, str]:
    return {k: s for k, v in _module_constants(tree).items() if (s := _const_str(v))}


# --------------------------------------------------------------------------- #
# event-schema-channels
# --------------------------------------------------------------------------- #
def _events_file(module_dir: Path) -> Path | None:
    for candidate in (module_dir / "events.py", module_dir / "events" / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _event_constants(module_dir: Path) -> dict[str, str]:
    events = _events_file(module_dir)
    return _string_constants(parse_file(events)) if events is not None else {}


def _declared_schema_channels(module_dir: Path) -> set[str] | None:
    """Keys of ``events.EVENT_SCHEMAS`` (None: not a literal dict)."""
    events = _events_file(module_dir)
    if events is None:
        return set()
    consts = _module_constants(parse_file(events))
    constants = {k: s for k, v in consts.items() if (s := _const_str(v))}
    schemas = consts.get("EVENT_SCHEMAS")
    if schemas is None:
        return set()
    if not isinstance(schemas, ast.Dict):
        return None
    channels: set[str] = set()
    for key in schemas.keys:
        value = _const_str(key) if key is not None else None
        if value is None and isinstance(key, ast.Name):
            value = constants.get(key.id)
        if value is None:
            return None
        channels.add(value)
    return channels


def _schema_channels(module_dir: Path, files: Sequence[Path]) -> set[str] | None:
    """Channels the module registers a schema for, or None if undecidable."""
    channels = _declared_schema_channels(module_dir)
    if channels is None:
        return None
    for _py, tree in _trees(files):
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) == "register_event_schema"
            ):
                continue
            channel = _const_str(node.args[0]) if node.args else None
            if channel is None:
                return None
            channels.add(channel)
    return channels


def find_unregistered_event_channels(
    module_dir: Path, files: Sequence[Path]
) -> list[Hit]:
    """``event-schema-channels``: publish_event channels without a schema."""
    registered = _schema_channels(module_dir, files)
    if registered is None:
        return []
    event_consts = _event_constants(module_dir)
    hits: list[Hit] = []
    for py, tree in _trees(files):
        local = _string_constants(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) == "publish_event"
                and node.args
            ):
                continue
            channel = _resolve_channel(node.args[0], local, event_consts)
            if channel is None or channel in registered:
                continue
            hits.append(
                (
                    py,
                    f"Line {node.lineno}: publish_event({channel!r}) — the module "
                    "registers no schema for this channel (events.EVENT_SCHEMAS)",
                )
            )
    return hits


def _resolve_channel(
    arg: ast.expr, local: dict[str, str], event_consts: dict[str, str]
) -> str | None:
    literal = _const_str(arg)
    if literal is not None:
        return literal
    if isinstance(arg, ast.Name):
        return local.get(arg.id) or event_consts.get(arg.id)
    if (
        isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id in {"events", "_events"}
    ):
        return event_consts.get(arg.attr)
    return None


# --------------------------------------------------------------------------- #
# guard-order
# --------------------------------------------------------------------------- #
def _is_auth(node: ast.AST) -> bool:
    names = _names_called(node) | {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name)
    }
    return bool(names & AUTH_NAMES)


def _is_guard(node: ast.AST) -> bool:
    return "idempotency_guard" in _names_called(node)


def _is_tenant_scope(node: ast.AST) -> bool:
    return "TenantScope" in _names_called(node)


def _org_params_of_gate(node: ast.AST) -> set[str]:
    """Org path params an inline ``require(...)`` gate resolves from.

    ``OrgRole`` always resolves an organisation; the billing atoms unless
    their ``of=`` is ``Account.user()`` (``Account.org(param=None)`` and
    ``Account.bound_tenant()`` read only the header / bound tenant).
    """
    params: set[str] = set()
    for call in ast.walk(node):
        name = _call_name(call)
        if (
            not isinstance(call, ast.Call)
            or name not in ORG_ATOMS | ORG_IF_ORGANIZATION
        ):
            continue
        if name == "OrgRole":
            params.add(_const_str(_keyword(call, "org_param")) or "org_id")
            continue
        account = _keyword(call, "of")
        kind = _call_name(account) if account is not None else None
        if kind in (None, "user"):
            continue
        if kind == "org" and isinstance(account, ast.Call):
            param = _keyword(account, "param")
            if param is None:
                params.add("org_id")
            elif _const_str(param) is not None:
                params.add(str(_const_str(param)))
            else:
                params.add("<bound tenant>")
        else:
            params.add("<bound tenant>")
    return params


def _ordered_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Endpoint parameters (annotation + default) in signature order."""
    args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    defaults: dict[int, ast.expr] = {}
    positional = [*fn.args.posonlyargs, *fn.args.args]
    for i, default in enumerate(reversed(fn.args.defaults)):
        defaults[len(positional) - 1 - i] = default
    out: list[ast.AST] = []
    for i, arg in enumerate(args):
        parts: list[ast.AST] = []
        if arg.annotation is not None:
            parts.append(arg.annotation)
        if i < len(positional) and i in defaults:
            parts.append(defaults[i])
        elif i >= len(positional):
            kw_default = fn.args.kw_defaults[i - len(positional)]
            if kw_default is not None:
                parts.append(kw_default)
        out.append(ast.Module(body=[ast.Expr(p) for p in parts], type_ignores=[]))  # type: ignore[arg-type]
    return out


#: Dependencies known NOT to authenticate (framework declaration markers and
#: guards). Anything else declared before a guard might authenticate, so the
#: static rule then stays silent and leaves the decision to create_app.
NON_AUTH_MARKERS = frozenset(
    {
        "public",
        "no_idempotency",
        "replay_safe",
        "sensitive",
        "operation_intent",
        "writes",
        "allow_multi_db_write",
        "at_commit",
        "rate_limiter",
        "idempotency_guard",
        "TenantScope",
    }
)


def _expand_items(
    value: ast.expr | None, consts: dict[str, ast.expr], depth: int = 0
) -> list[ast.AST] | None:
    """The items of a ``dependencies=`` value, or None when unresolvable."""
    if value is None:
        return []
    if depth > 4:
        return None
    if isinstance(value, ast.Name):
        return (
            _expand_items(consts.get(value.id), consts, depth + 1)
            if (value.id in consts)
            else None
        )
    if not isinstance(value, ast.List | ast.Tuple):
        return None
    items: list[ast.AST] = []
    for elt in value.elts:
        if isinstance(elt, ast.Starred):
            inner = _expand_items(elt.value, consts, depth + 1)
            if inner is None:
                return None
            items.extend(inner)
        else:
            items.append(elt)
    return items


def _router_items(
    tree: ast.AST, router: str | None, consts: dict[str, ast.expr]
) -> list[ast.AST] | None:
    if router is None:
        return None
    for node in getattr(tree, "body", []):
        if (
            isinstance(node, ast.Assign)
            and _call_name(node.value) == "APIRouter"
            and any(isinstance(t, ast.Name) and t.id == router for t in node.targets)
        ):
            return _expand_items(_keyword(node.value, "dependencies"), consts)  # type: ignore[arg-type]
    return None


def _inline_constants(node: ast.AST, consts: dict[str, ast.expr]) -> ast.AST:
    """``node`` plus the module constants its names refer to (one level)."""
    referenced = [
        consts[n.id]
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and n.id in consts
    ]
    if not referenced:
        return node
    return ast.Module(
        body=[ast.Expr(x) for x in (node, *referenced)],  # type: ignore[arg-type]
        type_ignores=[],
    )


def _maybe_auth(node: ast.AST) -> bool:
    """A dependency that is not a known non-authenticating marker."""
    called = _names_called(node)
    if not called:
        return isinstance(node, ast.Name | ast.Attribute)
    if "Depends" in called:
        deps = called - {"Depends"}
        if deps:
            return not deps <= NON_AUTH_MARKERS
        # Depends(name): the dependency is a plain reference.
        return not any(
            isinstance(n, ast.Name) and n.id in NON_AUTH_MARKERS for n in ast.walk(node)
        )
    return not called <= NON_AUTH_MARKERS


def find_guard_order_problems(files: Sequence[Path]) -> list[Hit]:
    """``guard-order``: statically visible guard / gate order mistakes."""
    hits: list[Hit] = []
    for py, tree in _trees(files):
        consts = _module_constants(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in fn.decorator_list:
                if not (isinstance(dec, ast.Call) and is_endpoint_decorator(dec)):
                    continue
                root, attrs = _root_name_and_attrs(dec.func)
                router = attrs[-2] if len(attrs) > 1 else root
                hits.extend(
                    (py, f"Line {dec.lineno}: {problem} ({fn.name})")
                    for problem in _route_order_problems(
                        dec, fn, _router_items(tree, router, consts), consts
                    )
                )
    return hits


def _route_order_problems(
    dec: ast.Call,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    router_items: list[ast.AST] | None,
    consts: dict[str, ast.expr],
) -> list[str]:
    decorator_items = _expand_items(_keyword(dec, "dependencies"), consts)
    if decorator_items is None or router_items is None:
        return []
    sequence = [*router_items, *decorator_items, *_ordered_params(fn)]
    path = _const_str(dec.args[0]) if dec.args else None
    path_params: set[str] = set()
    if path:
        path_params = {
            seg[1:-1].split(":")[0]
            for seg in path.split("/")
            if seg.startswith("{") and seg.endswith("}")
        }
    problems: list[str] = []
    auth_at = next((i for i, n in enumerate(sequence) if _is_auth(n)), None)
    guard_at = next((i for i, n in enumerate(sequence) if _is_guard(n)), None)
    if (
        guard_at is not None
        and auth_at is not None
        and guard_at < auth_at
        and not any(_maybe_auth(n) for n in sequence[:guard_at])
    ):
        problems.append(
            "idempotency_guard is declared before the authentication it keys on"
        )
    tenant_at = next((i for i, n in enumerate(sequence) if _is_tenant_scope(n)), None)
    if tenant_at is not None:
        for item in sequence[:tenant_at]:
            node = _inline_constants(item, consts)
            if "require" not in _names_called(node):
                continue
            needed = _org_params_of_gate(node)
            if needed and not needed & path_params:
                problems.append(
                    "a require(...) gate that needs an organisation is declared "
                    "before TenantScope() (it runs with no tenant bound)"
                )
                break
    return problems


# --------------------------------------------------------------------------- #
# consumed-contracts
# --------------------------------------------------------------------------- #
def _string_args(
    files: Sequence[Path], call_names: set[str]
) -> list[tuple[Path, int, str]]:
    out: list[tuple[Path, int, str]] = []
    for py in files:
        try:
            tree = parse_file(py)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in call_names:
                value = _const_str(node.args[0]) if node.args else None
                if value is None:
                    value = _const_str(_keyword(node, "name"))
                if value is not None:
                    out.append((py, node.lineno, value))
    return out


def find_undeclared_contracts(files: Sequence[Path]) -> list[Hit]:
    """``consumed-contracts``: get_service literals missing from consumes=."""
    declared = {v for _, _, v in _string_args(files, {"ContractRequirement"})}
    provided = {v for _, _, v in _string_args(files, {"register_service"})}
    hits: list[Hit] = []
    for py, line, name in _string_args(files, {"get_service"}):
        if name in declared or name in provided:
            continue
        hits.append(
            (
                py,
                f"Line {line}: get_service({name!r}) is not declared in the "
                "module's consumes=[ContractRequirement(...)]",
            )
        )
    return hits


# --------------------------------------------------------------------------- #
# db-dependency-scope
# --------------------------------------------------------------------------- #
def _db_dependency_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    name = (
        target.id
        if isinstance(target, ast.Name)
        else target.attr
        if isinstance(target, ast.Attribute)
        else None
    )
    if name is None:
        return None
    if name.startswith("get_db") or name in {"get_read_only_db", "make_db_dependency"}:
        return name
    return None


def find_unscoped_db_dependencies(file_path: Path) -> list[str]:
    """``db-dependency-scope``: ``Depends(get_db...)`` without scope="function"."""
    hits: list[str] = []
    for node in ast.walk(parse_file(file_path)):
        if not (isinstance(node, ast.Call) and _call_name(node) == "Depends"):
            continue
        if not node.args:
            continue
        name = _db_dependency_name(node.args[0])
        if name is None:
            continue
        if _const_str(_keyword(node, "scope")) == "function":
            continue
        hits.append(
            f'Line {node.lineno}: Depends({name}) without scope="function" — the '
            "Unit of Work would commit after the response is sent"
        )
    return hits


__all__ = [
    "AUTH_NAMES",
    "HTTP_METHODS",
    "ORG_ATOMS",
    "find_guard_order_problems",
    "find_undeclared_contracts",
    "find_unregistered_event_channels",
    "find_unscoped_db_dependencies",
]
