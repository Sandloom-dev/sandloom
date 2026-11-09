"""t9_member_export — admin-only export behind a live subscription gate.

Framework mode (a kernel-contributor task): the bundled ``tenancy``'s member
list. App mode: the seeded app's own ``invites`` module (``seeds/teamhub``)
exports its invitations — the same mechanism: an org-admin gate and a LIVE
subscription gate on the organization's account (billing battery loaded),
the billing contract declared or derived."""

import ast
import json
import re
from pathlib import Path

from _dx import (
    Route,
    app_mode,
    changed_under,
    definitions,
    expanded_head,
    module_dir,
    module_prefix,
    pick,
    routes,
    string_constants,
    text,
)

NAME = pick(framework="tenancy", app="invites")
MODULE = module_dir(NAME)  # the exporting module
ADMIN_GATES = ("require_org_role", "ROLE_ADMIN", 'OrgRole("admin"', "OrgRole('admin'")
#: Billing's live, organization-scoped access fact (``billing/access.py``): it
#: takes the org from the ``org_id`` path param, checks membership and is
#: live-only, so ``Entitled(<it>, owner="billing")`` is equivalent to
#: ``SubscriptionActive(of=Account.org(param="org_id"))``.
ORG_FACT = "organization.subscription.active"
#: The module that owns ``ORG_FACT`` (a fact is identified by owner and name).
FACT_OWNER = "billing"
#: Contracts that make billing's subscription state reachable from the module.
BILLING_CONTRACTS = ("billing.facts.v1", "billing.entitlements.v1")


def _export() -> list[Route]:
    return [r for r in routes(MODULE) if "export" in (r.path + r.function).lower()]


def _gate_text(route: Route, root: Path = MODULE) -> str:
    """The route plus the module-level policy constants it names (resolved)."""
    return route.text + "\n" + expanded_head(route, root)


def _spellings(values: tuple[str, ...]) -> str:
    """A regex alternation: the literals ``values`` and the module-level
    constants that hold one of them."""
    consts = [n for n, v in string_constants(MODULE).items() if v in values]
    return "|".join(
        [rf"[\"']{re.escape(v)}[\"']" for v in values]
        + [rf"\b{re.escape(n)}\b" for n in consts]
    )


def _org_fact_gate(blob: str) -> bool:
    arg = _spellings((ORG_FACT,))
    return re.search(rf"Entitled\(\s*(?:fact_name\s*=\s*)?(?:{arg})", blob) is not None


def test_fn_export_route() -> None:
    assert _export()


def test_fn_tests_changed() -> None:
    assert changed_under(module_prefix(NAME, "tests"))


def test_dev_live_subscription_gate() -> None:
    """'Check at request time' = the live SubscriptionActive() or the live org
    fact Entitled("organization.subscription.active", owner="billing"), not
    claim-only
    Subscribed()."""
    assert any(
        "SubscriptionActive(" in (blob := _gate_text(r)) or _org_fact_gate(blob)
        for r in _export()
    )


#: The organization roles that make ``Account.bound_tenant()`` an organization
#: account: the role check fails on a personal tenant. ``require_org_role``
#: is not one: it reads the path's ``org_id`` without comparing it with the
#: bound tenant.
_ORG_ADMIN_ROLES = {"admin", "owner"}


def _callee(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _resolved(node: ast.expr, defs: dict[str, str], seen: frozenset[str]) -> ast.expr:
    """``node`` with each module-level constant it names replaced by its
    definition, transitively."""

    class Expand(ast.NodeTransformer):
        def visit_Name(self, name: ast.Name) -> ast.AST:
            if name.id in defs and name.id not in seen:
                try:
                    # parenthesised: a multiline ``A\n & B`` constant loses
                    # its own parentheses in the extracted source segment
                    value = ast.parse(f"({defs[name.id]})", mode="eval").body
                except SyntaxError:
                    return name
                return _resolved(value, defs, seen | {name.id})
            return name

    return Expand().visit(node)


def _conjuncts(expr: ast.expr) -> list[ast.expr]:
    """The terms every request must pass: ``A & B`` splits, ``A | B`` does not."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitAnd):
        return _conjuncts(expr.left) + _conjuncts(expr.right)
    return [expr]


def _require_terms(part: ast.expr, defs: dict[str, str]) -> list[ast.expr]:
    terms: list[ast.expr] = []
    for call in ast.walk(_resolved(part, defs, frozenset())):
        if isinstance(call, ast.Call) and _callee(call) == "require" and call.args:
            terms += _conjuncts(call.args[0])
    return terms


def gate_terms(route: Route, root: Path) -> list[ast.expr]:
    """The conjuncts of every ``require(...)`` on the route (its decorator,
    its parameters' defaults and its router's own definition, constants
    resolved), parsed, never matched as text. The route's function is the
    one defined at ``route.line``: a same-named method elsewhere in the file
    contributes nothing."""
    parsed = ast.parse(route.file.read_text(errors="replace"))
    defs = definitions(root)
    terms: list[ast.expr] = []
    if route.router:
        try:
            terms += _require_terms(ast.parse(route.router, mode="eval").body, defs)
        except SyntaxError:
            pass
    for node in ast.walk(parsed):
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == route.function
            and (not route.line or node.lineno == route.line)
        ):
            continue
        args = node.args
        for part in [*node.decorator_list, *args.defaults, *args.kw_defaults]:
            if part is not None:
                terms += _require_terms(part, defs)
    return terms


def _org_admin_role(term: ast.expr) -> bool:
    if _callee(term) != "OrgRole":
        return False
    assert isinstance(term, ast.Call)
    values = [*term.args[:1], *(k.value for k in term.keywords if k.arg == "role")]
    return any(
        isinstance(v, ast.Constant) and v.value in _ORG_ADMIN_ROLES for v in values
    )


def _subscription_of(term: ast.expr, account: str) -> bool:
    """A live ``SubscriptionActive(of=Account.<account>(...))`` atom: the
    account of another billing atom (``Credits(..., of=...)``) says nothing
    about whose subscription is checked."""
    if _callee(term) != "SubscriptionActive":
        return False
    assert isinstance(term, ast.Call)
    return any(k.arg == "of" and _callee(k.value) == account for k in term.keywords)


def _of_bound_tenant(term: ast.expr) -> bool:
    return _subscription_of(term, "bound_tenant")


def bound_tenant_under_org_admin(terms: list[ast.expr]) -> bool:
    """A billing atom ``of=Account.bound_tenant()`` AND an ``OrgRole("admin")``
    / ``OrgRole("owner")`` term of the same route's gates (an ``|`` keeps
    either from being a term)."""
    return any(map(_of_bound_tenant, terms)) and any(map(_org_admin_role, terms))


def _of_org(term: ast.expr) -> bool:
    """``SubscriptionActive(of=Account.org(...))``."""
    return _subscription_of(term, "org")


def _org_fact(term: ast.expr) -> bool:
    """``Entitled("organization.subscription.active", owner="billing")``
    (constants resolved): a fact is billing's only when the policy names
    billing as its owner."""
    if _callee(term) != "Entitled":
        return False
    assert isinstance(term, ast.Call)
    values = [*term.args[:1], *(k.value for k in term.keywords if k.arg == "fact_name")]
    owners = [k.value for k in term.keywords if k.arg == "owner"]
    return any(
        isinstance(v, ast.Constant) and v.value == ORG_FACT for v in values
    ) and any(isinstance(o, ast.Constant) and o.value == FACT_OWNER for o in owners)


def _org_account(route: Route, root: Path) -> bool:
    """A gate term every request passes: SubscriptionActive(of=Account.org(...))
    (``param="org_id"`` or ``param=None``), the org fact, or
    Account.bound_tenant() required together with an organization admin
    role (:func:`bound_tenant_under_org_admin`). Terms are parsed from the
    route's and its router's ``require(...)``: a docstring, a comment or an
    ``|`` alternative naming them is not a gate."""
    terms = gate_terms(route, root)
    if any(_of_org(t) or _org_fact(t) for t in terms):
        return True
    return bound_tenant_under_org_admin(terms)


def test_dev_gate_checks_the_organization() -> None:
    """The organization's subscription, not the caller's: of=Account.org(...),
    the org fact, or Account.bound_tenant() AND OrgRole("admin"/"owner")
    (Account.user(reason=...) passes SANDLOOM-C027 but checks the wrong
    account)."""
    assert any(_org_account(r, MODULE) for r in _export())


def _derived_consumes() -> set[str]:
    """App mode: the contracts the module's resolved snapshot says it consumes
    (``module.snapshot.json``, written by ``sandloom test``; the gates check it is
    current). Public API 1.0 derives ``consumes`` from the gates a route
    names (a live billing gate -> billing's contract)."""
    try:
        data = json.loads((MODULE / "module.snapshot.json").read_text())
    except (OSError, ValueError):
        return set()
    return {
        str(c.get("name"))
        for c in data.get("consumes", [])
        if isinstance(c, dict) and c.get("name")
    }


def test_dev_facts_contract_declared() -> None:
    """Calling billing's facts / entitlements from the module declares the
    consumed contract: ``ContractRequirement(...)``, a ``consumes=[...]``
    entry, a module-level ``Contract(...)`` (Public API 1.0 derives
    ``consumes`` from it) or, in app mode, the derived ``consumes`` of the
    module's snapshot."""
    if app_mode() and _derived_consumes() & set(BILLING_CONTRACTS):
        return
    src = text(MODULE)
    name = _spellings(BILLING_CONTRACTS)
    declared = (
        re.search(rf"ContractRequirement\(\s*(?:name\s*=\s*)?(?:{name})", src)
        or re.search(rf"consumes\s*=\s*[\[(][^\])]*(?:{name})", src)
        or re.search(
            rf"Contract\(\s*(?:BILLING_FACTS_V1\b|BILLING_ENTITLEMENTS_V1\b|{name})",
            src,
        )
    )
    assert declared


def test_dev_admin_only() -> None:
    for r in _export():
        assert any(g in _gate_text(r) for g in ADMIN_GATES), r.function
