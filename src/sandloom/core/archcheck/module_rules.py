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
  ``scope="function"``;
* ``database-feature-forms`` (SANDLOOM-C030) — a ``sandloom.sdk.data`` form
  that needs a database feature (``Array``, ``fulltext_index``, ``matches``,
  ``json_path``, ``json_contains``, ``transaction_lock``) has its feature in
  a ``CapabilityRequirement("database", features=...)`` of the module;
* ``undeclared-capability`` (SANDLOOM-C033) — a ``tasks.py`` declares the
  ``taskqueue`` capability, and its beat hook the feature ``periodic``.

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
from sandloom.core.descriptors.database_forms import DATABASE_FORM_FEATURES
from sandloom.core.descriptors.derived_capabilities import (
    PERIODIC_FEATURE,
    PERIODIC_HOOK,
    TASKQUEUE,
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
# database-feature-forms
# --------------------------------------------------------------------------- #
_DATA_MODULE = "sandloom.sdk.data"
#: Forms whose undeclared use only warns for one minor release (they were in
#: use before the rule): the message says it becomes an error.
_WARN_ONLY_FORMS = frozenset({"transaction_lock"})

#: ``(file, "Line N: message", severity)``.
SeverityHit = tuple[Path, str, str]


def _data_aliases(tree: ast.AST) -> tuple[dict[str, str], set[str], list[int]]:
    """``local name -> form`` imported from ``sandloom.sdk.data``, the local
    names of the module itself (``import sandloom.sdk.data as d``, ``from
    sandloom.sdk import data``), and the lines of a ``*`` import from it."""
    forms: dict[str, str] = {}
    modules: set[str] = set()
    stars: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _DATA_MODULE:
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node.lineno)
                elif alias.name in DATABASE_FORM_FEATURES:
                    forms[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "sandloom.sdk":
            modules.update(a.asname or a.name for a in node.names if a.name == "data")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _DATA_MODULE:
                    modules.add(alias.asname or alias.name)
    return forms, modules, stars


def _form_uses(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """``(line, form)`` for every reference to a feature form: called or not
    (``f = d.transaction_lock``)."""
    forms, modules, _ = _data_aliases(tree)
    if not forms and not modules:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in forms:
                yield node.lineno, forms[node.id]
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in DATABASE_FORM_FEATURES
            and isinstance(node.ctx, ast.Load)
        ):
            root, attrs = _root_name_and_attrs(node.value)
            dotted = ".".join([root, *attrs]) if root is not None else None
            if dotted in modules:
                yield node.lineno, node.attr


def _literal_strings(node: ast.expr | None) -> set[str] | None:
    """The strings of a set / list / tuple literal, or of ``frozenset(...)``
    / ``set(...)`` over one; ``None`` when not statically known."""
    if isinstance(node, ast.Call) and _call_name(node) in {"frozenset", "set"}:
        if not node.args:
            return set()
        node = node.args[0]
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return None
    values = {_const_str(item) for item in node.elts}
    if None in values:
        return None
    return {v for v in values if v is not None}


_UNREADABLE = (
    "cannot be read statically, so sandloom check cannot tell which database "
    "features the module declares; write it inline"
)


def _requirement_features(
    call: ast.Call, py: Path, problems: list[SeverityHit]
) -> set[str]:
    """The features of a ``CapabilityRequirement("database", ...)`` call."""
    capability = _const_str(call.args[0]) if call.args else None
    if capability is None:
        capability = _const_str(_keyword(call, "capability"))
    if capability != "database":
        return set()
    features = _keyword(call, "features")
    if features is None and len(call.args) > 3:
        features = call.args[3]
    if features is None:
        return set()
    found = _literal_strings(features)
    if found is None:
        problems.append(
            (
                py,
                f"Line {call.lineno}: features= of CapabilityRequirement"
                f'("database") {_UNREADABLE}: features=frozenset({{"..."}})',
                "error",
            )
        )
        return set()
    return found


def _entry_features(
    item: ast.expr,
    constants: dict[str, ast.expr],
    py: Path,
    call: ast.Call,
    problems: list[SeverityHit],
) -> set[str]:
    """The database features one entry of ``capabilities=`` declares."""
    if isinstance(item, ast.Name):
        item = constants.get(item.id, item)
    if _const_str(item) is not None:
        return set()  # "cache": no features
    if isinstance(item, ast.Call) and _call_name(item) == "CapabilityRequirement":
        return _requirement_features(item, py, problems)
    problems.append(
        (
            py,
            f"Line {getattr(item, 'lineno', call.lineno)}: an entry of "
            f"capabilities= {_UNREADABLE} as "
            'CapabilityRequirement("database", features=...)',
            "error",
        )
    )
    return set()


def _declared_database_features(
    trees: dict[Path, ast.AST],
) -> tuple[set[str], list[SeverityHit]]:
    """The database features the module declares in ``capabilities=`` of its
    ``define_module(...)`` (or ``ModuleDescriptor(...)``), and a finding for
    each declaration that cannot be read statically."""
    declared: set[str] = set()
    problems: list[SeverityHit] = []
    for py, tree in trees.items():
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) in {"define_module", "ModuleDescriptor"}
            ):
                continue
            capabilities = _keyword(node, "capabilities")
            if isinstance(capabilities, ast.Name):
                capabilities = constants.get(capabilities.id, capabilities)
            if capabilities is None:
                continue
            if not isinstance(capabilities, (ast.List, ast.Tuple)):
                problems.append(
                    (
                        py,
                        f"Line {node.lineno}: capabilities= {_UNREADABLE} as a list",
                        "error",
                    )
                )
                continue
            for item in capabilities.elts:
                declared |= _entry_features(item, constants, py, node, problems)
    return declared, problems


def find_database_feature_problems(files: Sequence[Path]) -> list[SeverityHit]:
    """``database-feature-forms``: a feature form whose database feature the
    module's ``define_module(capabilities=[...])`` does not declare; a ``*``
    import from ``sandloom.sdk.data`` (it hides the forms used); a
    declaration that cannot be read statically. An undeclared
    ``transaction_lock`` is a warning for one minor release."""
    trees: dict[Path, ast.AST] = {}
    for py in files:
        try:
            trees[py] = parse_file(py)
        except SyntaxError:
            continue
    declared, hits = _declared_database_features(trees)
    for py, tree in trees.items():
        for line in _data_aliases(tree)[2]:
            hits.append(
                (
                    py,
                    f"Line {line}: from sandloom.sdk.data import * hides which "
                    "feature forms the module uses; import the names",
                    "error",
                )
            )
        for line, form in _form_uses(tree):
            feature = DATABASE_FORM_FEATURES[form]
            if feature in declared:
                continue
            message = (
                f"Line {line}: {form} needs the database feature "
                f"{feature!r}; declare it in define_module: capabilities="
                '[CapabilityRequirement("database", '
                f'features=frozenset({{"{feature}"}}))]'
            )
            severity = "error"
            if form in _WARN_ONLY_FORMS:
                severity = "warning"
                message += (
                    " (a warning in this release; an error from the next minor release)"
                )
            hits.append((py, message, severity))
    return hits


# --------------------------------------------------------------------------- #
# undeclared-capability
# --------------------------------------------------------------------------- #
#: ``capability -> features`` a module declares; ``None`` when a declaration
#: cannot be read statically (C030 reports it), so nothing is decided.
_Declared = dict[str, set[str]] | None


def _declared_entry(
    item: ast.expr, constants: dict[str, ast.expr]
) -> tuple[str, set[str]] | None:
    """``(capability, features)`` of one ``capabilities=`` entry, or ``None``
    when it cannot be read statically."""
    if isinstance(item, ast.Name):
        item = constants.get(item.id, item)
    name = _const_str(item)
    if name is not None:
        return name, set()
    if not (isinstance(item, ast.Call) and _call_name(item) == "CapabilityRequirement"):
        return None
    capability = _const_str(item.args[0]) if item.args else None
    if capability is None:
        capability = _const_str(_keyword(item, "capability"))
    if capability is None:
        return None
    features = _keyword(item, "features")
    if features is None and len(item.args) > 3:
        features = item.args[3]
    if features is None:
        return capability, set()
    found = _literal_strings(features)
    return None if found is None else (capability, found)


def _declared_capabilities(trees: dict[Path, ast.AST]) -> _Declared:
    """What ``capabilities=`` of the module's ``define_module(...)`` (or
    ``ModuleDescriptor(...)``) declares. ``None`` when there is no such call
    or a declaration cannot be read: the check then decides nothing."""
    declared: dict[str, set[str]] = {}
    found = False
    for tree in trees.values():
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) in {"define_module", "ModuleDescriptor"}
            ):
                continue
            found = True
            capabilities = _keyword(node, "capabilities")
            if isinstance(capabilities, ast.Name):
                capabilities = constants.get(capabilities.id, capabilities)
            if capabilities is None:
                continue
            if not isinstance(capabilities, (ast.List, ast.Tuple)):
                return None
            for item in capabilities.elts:
                entry = _declared_entry(item, constants)
                if entry is None:
                    return None
                declared.setdefault(entry[0], set()).update(entry[1])
    return declared if found else None


def _first_definition_line(tree: ast.AST) -> int:
    """The line of the first top-level ``def`` / ``class`` (decorators
    included), where a waiver comment can go; 1 without one."""
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return min([node.lineno, *(d.lineno for d in node.decorator_list)])
    return 1


def find_undeclared_capabilities(module_dir: Path, files: Sequence[Path]) -> list[Hit]:
    """``undeclared-capability``: a capability the module's code shows it
    needs (the rules of :mod:`sandloom.core.descriptors.derived_capabilities`)
    that its ``define_module(capabilities=[...])`` does not declare:

    * a ``tasks.py`` (or ``tasks/`` package) without ``taskqueue``;
    * a beat hook (``setup_periodic_tasks``) without the ``taskqueue``
      feature ``periodic``.

    The load preflight derives both anyway; the declaration is what readers
    see. Only what is decidable is reported: an unreadable declaration (a
    SANDLOOM-C030 finding) or a module without ``define_module`` /
    ``ModuleDescriptor`` decides nothing, and the rule does not claim to find
    every capability a module uses.
    """
    tasks = next(
        (
            p
            for p in (module_dir / "tasks.py", module_dir / "tasks" / "__init__.py")
            if p.is_file()
        ),
        None,
    )
    if tasks is None:
        return []
    try:
        tasks_tree = parse_file(tasks)
    except SyntaxError:
        return []
    declared = _declared_capabilities(dict(_trees(files)))
    if declared is None:
        return []
    taskqueue = declared.get(TASKQUEUE)
    hook = next(
        (
            node
            for node in getattr(tasks_tree, "body", [])
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == PERIODIC_HOOK
        ),
        None,
    )
    shown = tasks.relative_to(module_dir).as_posix()
    if hook is not None and (taskqueue is None or PERIODIC_FEATURE not in taskqueue):
        return [
            (
                tasks,
                f"Line {hook.lineno}: {PERIODIC_HOOK} schedules periodic jobs, "
                f"which need the {TASKQUEUE} feature {PERIODIC_FEATURE!r}; declare "
                "it in define_module: capabilities=[CapabilityRequirement("
                f'"{TASKQUEUE}", features=frozenset({{"{PERIODIC_FEATURE}"}}))] '
                "(required=False when the module works without its schedule)",
            )
        ]
    if taskqueue is None:
        return [
            (
                tasks,
                f"Line {_first_definition_line(tasks_tree)}: the module has "
                f"tasks ({shown}), which need the {TASKQUEUE} capability; "
                f'declare it in define_module: capabilities=["{TASKQUEUE}"]',
            )
        ]
    return []


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
    "find_database_feature_problems",
    "find_undeclared_capabilities",
    "find_undeclared_contracts",
    "find_unregistered_event_channels",
    "find_unscoped_db_dependencies",
]
