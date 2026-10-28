"""The static checks of Public API 1.0 (spec §5.10, §5.1, §4.4, §6, §5.2,
§5.3, §7.3; codes SANDLOOM-C021 … SANDLOOM-C027).

Purely static, like every ``sandloom check`` rule: an analyzer reports only what
it can decide from the source, and a deliberate exception is waived on its
line with ``# sandloom: allow <rule-id|code> <reason>``.

=====  ===========================  =======  ===================================
code   rule                         level    reports
=====  ===========================  =======  ===================================
C021   ``module-local-imports``     warning  an import inside a function (an
                                             error once WS-C clears the bundled
                                             modules' imports, spec §5.10)
C022   ``untagged-sensitive``       warning  a money command / credential or
                                             session effect without a risk tag;
                                             a write route with a hand-rolled
                                             authority check and no sensitive()
C023   ``multi-store-waivers``      info     every ``allow_multi_db_write`` (an
                                             empty reason is an error)
C024   ``unsafe-tenant-query``      error    a raw non-relational query outside
                                             ``with unscoped():``
C025   ``event-view-compat``        error /  a view incompatible with its producer
                                    warning  (or ``.publish()`` on it); a view
                                             with no producer schema found
C026   ``contract-union-hint``      info     an ambiguous union in a contract
C027   ``account-route-mismatch``   error    an organization route gating billing
                                             with ``of=Account.user()``
=====  ===========================  =======  ===================================
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

from sandloom.core.archcheck.analyzers import (
    _const_str,
    is_endpoint_decorator,
    parse_file,
    read_source,
)

Hit = tuple[Path, str]

#: Severity of each rule (``sandloom check`` fails only on errors).
SEVERITY: dict[str, str] = {
    # A warning while the bundled modules still carry their function-local
    # imports (48; WS-C removes them), then "error" (spec §5.10).
    "module-local-imports": "warning",
    "untagged-sensitive": "warning",
    "multi-store-waivers": "info",
    "unsafe-tenant-query": "error",
    "event-view-compat": "error",
    "contract-union-hint": "info",
    "account-route-mismatch": "error",
}


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _line_of(detail: str) -> int:
    return int(detail.split(":", 1)[0].split()[1])


def _marked(source_lines: Sequence[str], line: int, marker: str) -> bool:
    if 1 <= line <= len(source_lines):
        return marker in source_lines[line - 1]
    return False


# --------------------------------------------------------------------------- #
# SANDLOOM-C021 module-local-imports
# --------------------------------------------------------------------------- #

_OPTIONAL_IMPORT = "# sandloom: optional-import "


def _type_checking_blocks(tree: ast.AST) -> set[int]:
    """Ids of import nodes under ``if TYPE_CHECKING:``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = (
                test.id
                if isinstance(test, ast.Name)
                else test.attr
                if isinstance(test, ast.Attribute)
                else None
            )
            if name == "TYPE_CHECKING":
                for child in ast.walk(node):
                    if isinstance(child, ast.Import | ast.ImportFrom):
                        ids.add(id(child))
    return ids


def find_module_local_imports(file_path: Path) -> list[str]:
    """``import`` statements inside a function (SANDLOOM-C021).

    Allowed: ``if TYPE_CHECKING:`` imports and an optional dependency marked
    ``# sandloom: optional-import <reason>`` on the import line.
    """
    tree = parse_file(file_path)
    lines = read_source(file_path).splitlines()
    typing_only = _type_checking_blocks(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                not isinstance(node, ast.Import | ast.ImportFrom)
                or id(node) in typing_only
            ):
                continue
            if _marked(lines, node.lineno, _OPTIONAL_IMPORT):
                continue
            names = (
                node.module or "."
                if isinstance(node, ast.ImportFrom)
                else ", ".join(a.name for a in node.names)
            )
            found.append(
                f"Line {node.lineno}: function-local import of {names} in "
                f"{fn.name}() — import it at module level"
            )
    return sorted(set(found), key=_line_of)


# --------------------------------------------------------------------------- #
# SANDLOOM-C022 untagged-sensitive (warning)
# --------------------------------------------------------------------------- #

#: Command names that move money (``@command`` without ``risk="money"``).
MONEY_VERBS = re.compile(
    r"^(spend|charge|refund|grant|credit|debit|pay|payout|transfer|withdraw|"
    r"deposit|capture|top_?up|reserve_credits)(_|$)"
)
#: Calls changing credentials or sessions; their owner chooses admin/security
#: risk according to the operation, rather than the revocation mechanism.
#: Verifying a password changes nothing, so the hashing helpers are not here.
SECURITY_CALLS = frozenset(
    {
        "revoke_user_tokens",
        "revoke_user_tokens_on_commit",
        "block_user",
        "unblock_user",
    }
)
#: A framework admin gate: it carries the admin risk statically (G-SENS-01),
#: so a route behind it is tagged.
_ADMIN_GATE = re.compile(
    r"\brequire_platform_admin\b"
    r"|\bRole\(\s*['\"]admin['\"]"
    r"|\bOrgRole\(\s*['\"](admin|owner)['\"]"
    r"|\brequire_org_role\(\s*(ROLE_ADMIN|ROLE_OWNER|['\"](admin|owner)['\"])"
)
#: A member-level organization gate (no admin risk).
_MEMBER_GATE = re.compile(
    r"\brequire_org_role\(\s*(ROLE_MEMBER|['\"]member['\"])"
    r"|\bOrgRole\(\s*['\"]member['\"]"
)
#: A call deciding an authority by hand (``user_has_admin_role``,
#: ``holds_admin_in_tx``, ``is_owner``, ``check_role`` ...).
_AUTHORITY_CALL = re.compile(
    r"^(is|has|holds|user_has|check|ensure|assert|verify)_\w*(admin|owner|role)"
)
_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})


def _decorator_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    return [d for d in fn.decorator_list if isinstance(d, ast.Call)]


def _admin_routers(tree: ast.AST) -> set[str]:
    """Module-level routers whose own ``dependencies`` carry an admin gate."""
    names: set[str] = set()
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        deps = _keyword(node.value, "dependencies")
        if deps is not None and _ADMIN_GATE.search(ast.unparse(deps)):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _endpoint(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.Call, str, str] | None:
    """``(decorator, method, router name)`` of an endpoint, else ``None``."""
    for dec in _decorator_calls(fn):
        if not is_endpoint_decorator(dec) or not isinstance(dec.func, ast.Attribute):
            continue
        router = dec.func.value
        name = router.id if isinstance(router, ast.Name) else ast.unparse(router)
        return dec, dec.func.attr, name
    return None


def _route_text(fn: ast.FunctionDef | ast.AsyncFunctionDef, dec: ast.Call) -> str:
    """The route's declarations: its decorator and its parameters' defaults
    and annotations (where gates and markers live)."""
    args = fn.args
    parts = [ast.unparse(dec)]
    parts += [
        ast.unparse(arg.annotation)
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if arg.annotation is not None
    ]
    parts += [ast.unparse(d) for d in (*args.defaults, *args.kw_defaults) if d]
    return "\n".join(parts)


def _hand_rolled_authority(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, dec: ast.Call, declared: str
) -> str | None:
    """Why a write route decides an authority by hand, else ``None``."""
    for stmt in fn.body:
        for node in ast.walk(stmt):
            name = _call_name(node)
            if name and _AUTHORITY_CALL.match(name):
                return f"decides an authority in its body ({name}())"
            if (
                isinstance(node, ast.Attribute) and node.attr == "HTTP_403_FORBIDDEN"
            ) or (
                isinstance(node, ast.keyword)
                and node.arg == "status_code"
                and isinstance(node.value, ast.Constant)
                and node.value.value == 403
            ):
                return "refuses with 403 in its body"
    if re.search(r"\bat_commit\(\s*\)", declared):
        return "declares at_commit() for an authority it checks itself"
    path = _const_str(dec.args[0]) if dec.args else None
    if path and "/members" in path and _MEMBER_GATE.search(declared):
        return "changes a membership behind a member-level gate"
    return None


def _untagged_money_commands(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, tagged: bool
) -> list[str]:
    found = []
    for dec in _decorator_calls(fn):
        if _call_name(dec) == "command" and MONEY_VERBS.match(fn.name):
            risk = _const_str(_keyword(dec, "risk"))
            if risk != "money" and not tagged:
                found.append(
                    f"Line {fn.lineno}: command {fn.name}() looks like a money "
                    'effect but declares no risk — @command(..., risk="money")'
                )
    return found


def _route_check(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, admin_routers: set[str]
) -> tuple[bool, str | None]:
    """``(tagged, hit)`` for an endpoint: whether a gate or marker carries its
    risk, and the hand-rolled authority report of an untagged write route."""
    endpoint = _endpoint(fn)
    if endpoint is None:
        return False, None
    dec, method, router = endpoint
    declared = _route_text(fn, dec)
    if (
        router in admin_routers
        or _ADMIN_GATE.search(declared)
        or re.search(r"\b(sensitive|public)\(", declared)
    ):
        return True, None
    why = (
        _hand_rolled_authority(fn, dec, declared) if method in _WRITE_METHODS else None
    )
    if why is None:
        return False, None
    path = _const_str(dec.args[0]) if dec.args else "?"
    return False, (
        f"Line {fn.lineno}: route {method.upper()} {path} ({fn.name}) {why} and "
        'declares no risk — add sensitive("admin") (or "security") to '
        "its dependencies"
    )


def find_untagged_sensitive(file_path: Path) -> list[str]:
    """Effects and routes that look sensitive and carry no risk tag (SANDLOOM-C022,
    warning; the ADR-0009 residuals).

    - a ``@command`` whose name moves money, without ``risk="money"``;
    - a function calling a credential / session changer
      (:data:`SECURITY_CALLS`) without ``@sensitive_effect``;
    - a write route that decides an authority by hand (an authority call or a
      403 in its body, ``at_commit()``, a membership change behind a
      member-level gate) without ``sensitive(risk)``.

    A function or route behind a framework admin gate carries the admin risk
    already and is not reported; neither is a ``public()`` route.
    """
    tree = parse_file(file_path)
    admin_routers = _admin_routers(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        tagged = any(
            _call_name(d) in {"sensitive_effect", "sensitive"}
            for d in _decorator_calls(fn)
        )
        found += _untagged_money_commands(fn, tagged)
        route_tagged, hit = _route_check(fn, admin_routers)
        if hit is not None and not tagged:
            found.append(hit)
            continue
        if tagged or route_tagged:
            continue
        for node in ast.walk(fn):
            name = _call_name(node)
            if isinstance(node, ast.Call) and name in SECURITY_CALLS:
                found.append(
                    f"Line {node.lineno}: {fn.name}() calls {name}() without a "
                    "risk tag — declare the owning operation "
                    '@sensitive_effect("admin") or @sensitive_effect("security")'
                )
                break
    return sorted(found, key=_line_of)


# --------------------------------------------------------------------------- #
# SANDLOOM-C023 multi-store-waivers (info listing)
# --------------------------------------------------------------------------- #


def find_multi_store_waivers(file_path: Path) -> list[tuple[str, str]]:
    """``(severity, "Line N: ...")`` for every ``allow_multi_db_write`` (C023)."""
    tree = parse_file(file_path)
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "allow_multi_db_write":
            continue
        reason = _const_str(_keyword(node, "reason"))
        secondaries = _keyword(node, "secondaries")
        listed = ast.unparse(secondaries) if secondaries is not None else "?"
        if not reason or not reason.strip():
            out.append(
                (
                    "error",
                    f"Line {node.lineno}: allow_multi_db_write(secondaries={listed}) "
                    "has no reason — state why best-effort 1PC is acceptable",
                )
            )
        else:
            out.append(
                (
                    "info",
                    f"Line {node.lineno}: multi-store waiver secondaries={listed}: "
                    f"{reason}",
                )
            )
    return sorted(out, key=lambda hit: _line_of(hit[1]))


# --------------------------------------------------------------------------- #
# SANDLOOM-C024 unsafe-tenant-query
# --------------------------------------------------------------------------- #

#: The raw (tenant-unscoped) query methods of non-relational stores.
RAW_QUERY_METHODS = frozenset(
    {"raw_query", "query_raw", "find_raw", "scan_all", "aggregate_raw"}
)


def _unscoped_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        if any(_call_name(item.context_expr) == "unscoped" for item in node.items):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            ranges.append((node.lineno, end))
    return ranges


def find_unsafe_tenant_queries(file_path: Path) -> list[str]:
    """Raw non-relational queries outside ``with unscoped():`` (SANDLOOM-C024)."""
    tree = parse_file(file_path)
    ranges = _unscoped_ranges(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        name = _call_name(node)
        if name not in RAW_QUERY_METHODS or not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if any(start <= node.lineno <= end for start, end in ranges):
            continue
        found.append(
            f"Line {node.lineno}: raw store query .{name}() reads every tenant — "
            "use the store's tenant-scoped operations, or `with unscoped():` "
            "for deliberate cross-tenant work"
        )
    return sorted(found, key=_line_of)


# --------------------------------------------------------------------------- #
# SANDLOOM-C026 contract-union-hint (info)
# --------------------------------------------------------------------------- #

_SCALARS = frozenset(
    {"str", "int", "float", "bool", "UUID", "datetime", "date", "Decimal"}
)


def _union_members(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _union_members(node.left)
        right = _union_members(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Constant) and node.value is None:
        return ["None"]
    return None


def _ambiguous(annotation: ast.AST | None) -> str | None:
    if annotation is None:
        return None
    for node in ast.walk(annotation):
        members = _union_members(node)
        if members is None or len(members) < 2:
            continue
        scalars = [m for m in members if m in _SCALARS]
        if len(scalars) >= 2:
            return ast.unparse(node)
    return None


def _is_contract_member(fn: ast.AST, provider: bool) -> bool:
    if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    if fn.name.startswith("_"):
        return False
    if provider:
        return True
    return any(
        (isinstance(d, ast.Name) and d.id == "contract_method")
        or _call_name(d) == "contract_method"
        for d in fn.decorator_list
    )


def find_contract_union_hints(file_path: Path) -> list[str]:
    """Ambiguous unions (codec 1) in contract signatures (SANDLOOM-C026, info)."""
    tree = parse_file(file_path)
    found: list[str] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        provider = any(_call_name(d) == "contract" for d in cls.decorator_list)
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _is_contract_member(fn, provider):
                continue
            annotations = [a.annotation for a in fn.args.args] + [fn.returns]
            for annotation in annotations:
                union = _ambiguous(annotation)
                if union is not None:
                    found.append(
                        f"Line {fn.lineno}: {cls.name}.{fn.name} uses the ambiguous "
                        f"union {union} (codec 1, tagged); a discriminated type "
                        "lets the fully typed codec 2 apply"
                    )
                    break
    return found


# --------------------------------------------------------------------------- #
# SANDLOOM-C027 account-route-mismatch
# --------------------------------------------------------------------------- #

#: Billing atoms that take ``of=`` (spec §7.2).
BILLING_ATOMS = frozenset(
    {"Subscribed", "SubscriptionActive", "Credits", "Feature", "WithinLimit"}
)
_ORG_PARAMS = frozenset({"org_id", "organization_id"})
_PATH_PARAM = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?}")


def _account_call(node: ast.AST) -> tuple[str, ast.Call] | None:
    """``("user"|"org"|"bound_tenant", call)`` for ``Account.<x>(...)``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Account"
    ):
        return node.func.attr, node
    return None


def _org_param_names(tree: ast.AST) -> set[str]:
    names = set(_ORG_PARAMS)
    for node in ast.walk(tree):
        found = _account_call(node)
        if found is not None and found[0] == "org":
            param = _const_str(_keyword(found[1], "param"))
            if param:
                names.add(param)
    return names


def _route_parts(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[tuple[str, list[ast.AST]]]:
    """``(path, [nodes that declare the route's dependencies])`` per route."""
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and is_endpoint_decorator(dec)):
            continue
        path = (
            _const_str(dec.args[0]) if dec.args else _const_str(_keyword(dec, "path"))
        )
        nodes: list[ast.AST] = [dec, *fn.args.defaults]
        nodes += [d for d in fn.args.kw_defaults if d is not None]
        nodes += [a.annotation for a in fn.args.args if a.annotation is not None]
        yield path or "", nodes


def _query_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]}


def find_account_route_mismatches(file_path: Path) -> list[str]:
    """Organization routes gating billing with ``of=Account.user()`` (C027).

    Also refuses ``Account.org(param=X)`` on a route without ``X`` in its path
    or parameters (today's rule for ``org_param``).
    """
    tree = parse_file(file_path)
    org_params = _org_param_names(tree)
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for path, nodes in _route_parts(fn):
            in_path = set(_PATH_PARAM.findall(path))
            org_route = bool(in_path & org_params)
            for root in nodes:
                found += _route_account_problems(
                    root, path, in_path | _query_params(fn), org_route
                )
    return sorted(set(found), key=_line_of)


def _route_account_problems(
    root: ast.AST, path: str, available: set[str], org_route: bool
) -> list[str]:
    out: list[str] = []
    for node in ast.walk(root):
        if _call_name(node) not in BILLING_ATOMS or not isinstance(node, ast.Call):
            continue
        of = _keyword(node, "of")
        account = _account_call(of) if of is not None else None
        if account is None:
            continue
        kind, call = account
        if kind == "user" and org_route and _keyword(call, "reason") is None:
            out.append(
                f"Line {node.lineno}: route {path!r} carries an organization "
                f"parameter but {_call_name(node)}(of=Account.user()) checks the "
                "caller's personal account — use of=Account.org(param=...) or "
                'state Account.user(reason="...")'
            )
        if kind == "org":
            param_node = _keyword(call, "param")
            param = "org_id" if param_node is None else _const_str(param_node)
            if param and param not in available:
                out.append(
                    f"Line {node.lineno}: Account.org(param={param!r}) on route "
                    f"{path!r}, which has no {param!r} path or query parameter"
                )
    return out


# --------------------------------------------------------------------------- #
# SANDLOOM-C025 event-view-compat
# --------------------------------------------------------------------------- #

_CHANNEL = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+\.v[1-9][0-9]*$")
#: Annotation -> JSON schema ``type`` of the exported producer schema.
_JSON_TYPES = {
    "str": "string",
    "UUID": "string",
    "datetime": "string",
    "date": "string",
    "Decimal": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


class EventClass:
    """An ``Event`` subclass found statically.

    Its fields include those inherited from base classes defined in the same
    file (an abstract ``Event`` base sharing ``tenant_id`` and the like),
    bases first, as pydantic orders them.
    """

    def __init__(
        self,
        file: Path,
        node: ast.ClassDef,
        channel: str,
        role: str,
        classes: dict[str, ast.ClassDef] | None = None,
    ) -> None:
        self.file = file
        self.node = node
        self.channel = channel
        self.role = role
        self.fields: dict[str, str] = {}
        self.optional: set[str] = set()
        for cls in _lineage(node, classes or {}):
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and "ClassVar" not in ast.unparse(stmt.annotation)
                ):
                    name = stmt.target.id
                    self.fields[name] = ast.unparse(stmt.annotation)
                    if stmt.value is not None:
                        self.optional.add(name)
                    else:
                        self.optional.discard(name)


def _lineage(
    node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> list[ast.ClassDef]:
    """``node`` after its same-file bases (depth first, bases in order)."""
    out: list[ast.ClassDef] = []
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in classes and base.id not in seen:
            for cls in _lineage(classes[base.id], classes, seen | {node.name}):
                if cls not in out:
                    out.append(cls)
    out.append(node)
    return out


def event_classes(file_path: Path) -> list[EventClass]:
    """The ``class X(Event, channel="...")`` declarations of a file."""
    tree = parse_file(file_path)
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    out: list[EventClass] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        channel = _const_str(
            next((k.value for k in node.keywords if k.arg == "channel"), None)
        )
        if not channel or not _CHANNEL.match(channel):
            continue
        role = _const_str(
            next((k.value for k in node.keywords if k.arg == "role"), None)
        )
        out.append(EventClass(file_path, node, channel, role or "auto", classes))
    return out


def _is_view(event: EventClass, module: str) -> bool:
    if event.role != "auto":
        return event.role == "view"
    return event.channel.split(".", 1)[0] != module


def _view_publish_calls(file_path: Path, views: set[str]) -> list[str]:
    tree = parse_file(file_path)
    return [
        f"Line {node.lineno}: {node.func.value.func.id} is a view of another "
        "module's channel: only the producing module publishes it"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id in views
    ]


def _compare_with_class(view: EventClass, producer: EventClass) -> list[str]:
    problems = []
    for name, annotation in view.fields.items():
        if name not in producer.fields:
            problems.append(f"field {name!r} is not in the producer")
        elif producer.fields[name] != annotation:
            problems.append(
                f"field {name!r} is {annotation} here but {producer.fields[name]} "
                "in the producer"
            )
        elif name in producer.optional and name not in view.optional:
            problems.append(
                f"field {name!r} is optional in the producer: give it a default"
            )
    return problems


def _compare_with_schema(view: EventClass, schema: dict[str, object]) -> list[str]:
    properties = schema.get("properties")
    props = properties if isinstance(properties, dict) else {}
    raw_required = schema.get("required")
    required = set(raw_required) if isinstance(raw_required, list) else set()
    problems = []
    for name, annotation in view.fields.items():
        prop = props.get(name)
        if not isinstance(prop, dict):
            problems.append(f"field {name!r} is not in the exported producer schema")
            continue
        expected = _JSON_TYPES.get(annotation)
        if expected is not None and prop.get("type") not in (None, expected):
            problems.append(
                f"field {name!r} is {annotation} here but {prop.get('type')} in the "
                "producer schema"
            )
        if name not in required and name not in view.optional:
            problems.append(
                f"field {name!r} is optional in the producer: give it a default"
            )
    return problems


def find_event_view_problems(
    modules: dict[str, list[Path]], exported: Path | None
) -> list[tuple[str, Path, str]]:
    """``(severity, file, detail)`` for every view of every module (C025).

    The producer schema is found, in order, in the producer class of the same
    tree, the exported schema file ``<exported>/<channel>.json``, or nowhere
    (warning: unverified).
    """
    classes = {
        module: [e for f in files for e in event_classes(f)]
        for module, files in modules.items()
    }
    producers = {
        e.channel: e
        for module, events in classes.items()
        for e in events
        if not _is_view(e, module)
    }
    out: list[tuple[str, Path, str]] = []
    for module, events in classes.items():
        views = [e for e in events if _is_view(e, module)]
        for file in modules[module]:
            names = _view_names_in(file, views)
            if names:
                out += [("error", file, d) for d in _view_publish_calls(file, names)]
        for view in views:
            out += _view_problems(view, producers, exported)
    return out


def _view_names_in(file: Path, views: list[EventClass]) -> set[str]:
    """The names that denote a view in ``file``: views it defines, and views
    it imports from the module file that defines them."""
    names = {v.node.name for v in views if v.file == file}
    by_stem: dict[str, set[str]] = {}
    for view in views:
        by_stem.setdefault(view.file.stem, set()).add(view.node.name)
    for node in ast.walk(parse_file(file)):
        if isinstance(node, ast.ImportFrom) and node.module:
            defined = by_stem.get(node.module.rsplit(".", 1)[-1], set())
            names |= {a.asname or a.name for a in node.names if a.name in defined}
    return names


def _view_problems(
    view: EventClass, producers: dict[str, EventClass], exported: Path | None
) -> list[tuple[str, Path, str]]:
    line = view.node.lineno
    producer = producers.get(view.channel)
    if producer is not None:
        problems = _compare_with_class(view, producer)
    else:
        schema_file = exported / f"{view.channel}.json" if exported else None
        if schema_file is None or not schema_file.is_file():
            return [
                (
                    "warning",
                    view.file,
                    f"Line {line}: view {view.node.name} of {view.channel!r} is "
                    "unverified: no producer in this tree and no exported schema "
                    "— run the producer's `sandloom test` to export it",
                )
            ]
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        problems = _compare_with_schema(view, schema)
    return [
        (
            "error",
            view.file,
            f"Line {line}: view {view.node.name} of {view.channel!r}: {p}",
        )
        for p in problems
    ]


def per_file_rules() -> dict[str, Callable[[Path], list[str]]]:
    """Rule id -> per-file analyzer (``(path) -> ["Line N: ..."]``)."""
    return {
        "module-local-imports": find_module_local_imports,
        "untagged-sensitive": find_untagged_sensitive,
        "unsafe-tenant-query": find_unsafe_tenant_queries,
        "contract-union-hint": find_contract_union_hints,
        "account-route-mismatch": find_account_route_mismatches,
    }


def iter_python(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.suffix == ".py":
            yield path


__all__ = [
    "BILLING_ATOMS",
    "beginner_names",
    "find_beginner_tier_problems",
    "MONEY_VERBS",
    "RAW_QUERY_METHODS",
    "SECURITY_CALLS",
    "SEVERITY",
    "EventClass",
    "event_classes",
    "find_account_route_mismatches",
    "find_contract_union_hints",
    "find_event_view_problems",
    "find_module_local_imports",
    "find_multi_store_waivers",
    "find_unsafe_tenant_queries",
    "find_untagged_sensitive",
    "iter_python",
    "per_file_rules",
]


# --------------------------------------------------------------------------- #
# SANDLOOM-T009 beginner-tier (generated code and the examples)
# --------------------------------------------------------------------------- #

_ADVANCED = "# sandloom: advanced "


def _literal_names(node: ast.expr | None, names: dict[str, list[str]]) -> list[str]:
    if isinstance(node, ast.List | ast.Tuple):
        return [
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tuple"
        and node.args
        and isinstance(node.args[0], ast.Name)
    ):
        return names.get(node.args[0].id, [])
    return []


def beginner_names(sdk_dir: Path) -> dict[str, set[str]]:
    """``{sandloom.sdk module: its __beginner__ names}``, read statically."""
    out: dict[str, set[str]] = {}
    for py in sorted(sdk_dir.rglob("*.py")):
        rel = py.relative_to(sdk_dir.parent.parent).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        if any(p.startswith("_") for p in parts):
            continue
        assigned: dict[str, list[str]] = {}
        value: ast.expr | None
        for node in ast.parse(py.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            else:
                continue
            if target in ("__all__", "__beginner__"):
                assigned[target] = _literal_names(value, assigned)
        out[".".join(parts)] = set(assigned.get("__beginner__", []))
    return out


def find_beginner_tier_problems(
    file_path: Path, beginner: dict[str, set[str]]
) -> list[str]:
    """``sandloom.sdk`` names outside the beginner tier without a marker (T009)."""
    lines = read_source(file_path).splitlines()
    found: list[str] = []
    for node in ast.walk(parse_file(file_path)):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        module = ""
        advanced: list[str] = []
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "sandloom.sdk"
        ):
            module = node.module or ""
            allowed = beginner.get(module, set())
            advanced = [a.name for a in node.names if a.name not in allowed]
        elif isinstance(node, ast.Import):
            module = "sandloom.sdk"
            advanced = [
                a.name for a in node.names if a.name.startswith("sandloom.sdk.")
            ]
        if not advanced:
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if any(_ADVANCED in lines[i - 1] for i in range(node.lineno, end + 1)):
            continue
        found.append(
            f"Line {node.lineno}: {', '.join(advanced)} from {module} is not in "
            "the beginner tier — use a beginner name, or mark the line "
            "`# sandloom: advanced <reason>`"
        )
    return found
