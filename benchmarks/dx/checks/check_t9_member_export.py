"""t9_member_export — admin-only export behind a live subscription gate.

Framework mode (a kernel-contributor task): the bundled ``tenancy``'s member
list. App mode: the seeded app's own ``invites`` module (``seeds/teamhub``)
exports its invitations — the same mechanism: an org-admin gate and a LIVE
subscription gate on the organization's account (billing battery loaded),
the billing contract declared or derived."""

import json
import re

from _dx import (
    Route,
    app_mode,
    changed_under,
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
#: live-only, so ``Entitled(<it>)`` is equivalent to
#: ``SubscriptionActive(of=Account.org(param="org_id"))``.
ORG_FACT = "organization.subscription.active"
#: Contracts that make billing's subscription state reachable from the module.
BILLING_CONTRACTS = ("billing.facts.v1", "billing.entitlements.v1")


def _export() -> list[Route]:
    return [r for r in routes(MODULE) if "export" in (r.path + r.function).lower()]


def _gate_text(route: Route) -> str:
    """The route plus the module-level policy constants it names (resolved)."""
    return route.text + "\n" + expanded_head(route, MODULE)


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
    fact Entitled("organization.subscription.active"), not claim-only
    Subscribed()."""
    assert any(
        "SubscriptionActive(" in (blob := _gate_text(r)) or _org_fact_gate(blob)
        for r in _export()
    )


def test_dev_gate_checks_the_organization() -> None:
    """The organization's subscription, not the caller's: of=Account.org(...)
    or the org fact (Account.user(reason=...) passes SANDLOOM-C027 but checks the
    wrong account)."""
    assert any(
        "Account.org(" in (blob := _gate_text(r)) or _org_fact_gate(blob)
        for r in _export()
    )


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
