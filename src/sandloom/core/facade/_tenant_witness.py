"""The tenant authority's membership witness (ADR-0014 decision 13, K1).

``tenant_kind`` calls a tenant ``organization`` only when the membership
witness of the **assigned** tenant authority confirms the caller's
membership. The authority is the one the composition root assigned to
``tenant_ids`` (``claim_authorities``, the verifier's whole map, so a split
process knows it too); the witness is the access check its contract declares
with ``access_check(..., witnesses_membership={...})``: a boolean read whose
``True`` answer, for any value of its own parameters, proves the subject is
a member of the organization. The values the declaration lists are the ones
the kernel passes to ask about membership alone (``{"minimum": "member"}``).

The declaration is read once, when the authorities are assigned, from what
the composition's consumers were generated against (ADR-0013 decision 3: the
expectation beside a consumer's stub, then ``CONTRACT_SNAPSHOT_PATHS``, then
the provider package's snapshot). Never from import order or a table filled
at import, never on a request. A contract counts when the authority owns it
(its first segment is the authority's name) or the root assigned it to the
authority (``access_providers``). No witness, or two different ones: none
is used, the kind stays ``unknown`` and a route policy using
``Account.bound_tenant()`` is reported at startup (H-19).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

import sandloom.core.contract_compat as _compat

#: The claim key whose authority classifies tenants (ADR-0014 decision 1).
TENANT_GRANT = "tenant_ids"
#: The key of an ``access_check`` declaration naming the membership witness.
WITNESS_KEY = "witnesses_membership"

logger = logging.getLogger("sandloom.authz.tenant_witness")


@dataclass(frozen=True, slots=True)
class TenantWitness:
    """The membership check ``tenant_kind`` asks: ``contract.method`` called
    with the organization in ``organization``, the caller in ``subject`` and
    ``values`` for every other parameter, in signature order (``args``)."""

    contract: str
    method: str
    args: tuple[str, ...]
    organization: str
    subject: str
    values: Mapping[str, Any] = field(default_factory=dict)
    #: The tenant authority (short name) and where the declaration was read.
    authority: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def arguments(self, organization: UUID, subject: UUID) -> list[Any]:
        """The call's positional arguments for ``(organization, subject)``."""
        out: list[Any] = []
        for arg in self.args:
            if arg == self.organization:
                out.append(organization)
            elif arg == self.subject:
                out.append(subject)
            else:
                out.append(self.values[arg])
        return out

    def key(self) -> tuple[Any, ...]:
        """What makes two declarations the same witness."""
        return (
            self.contract,
            self.method,
            self.args,
            self.organization,
            self.subject,
            tuple(sorted(self.values.items())),
        )

    def describe(self) -> str:
        return f"{self.contract}.{self.method}"


def witness_from_describe(
    contract: str, method: str, doc: Mapping[str, Any]
) -> TenantWitness | str | None:
    """The witness one method's describe document declares; a ``str`` names
    why its declaration cannot be used; ``None``: it declares none."""
    check = doc.get("access_check")
    if not isinstance(check, Mapping) or WITNESS_KEY not in check:
        return None
    where = f"{contract}.{method}"
    values = check.get(WITNESS_KEY)
    if not isinstance(values, Mapping):
        return f"{where}: {WITNESS_KEY} is not a mapping of parameter values"
    args = tuple(
        str(p.get("name")) for p in doc.get("params") or () if isinstance(p, Mapping)
    )
    raw_bind = check.get("bind")
    bind: Mapping[str, Any] = raw_bind if isinstance(raw_bind, Mapping) else {}
    kinds: dict[str, list[str]] = {}
    for arg, source in bind.items():
        kind = source[0] if isinstance(source, list) and source else None
        kinds.setdefault(str(kind), []).append(str(arg))
    orgs, subjects = kinds.get("org", []), kinds.get("subject", [])
    if len(orgs) != 1 or len(subjects) != 1 or set(kinds) - {"org", "subject"}:
        return (
            f"{where}: a membership witness binds exactly one organization "
            "(From.org) and one subject (From.subject_id), nothing else"
        )
    own = [a for a in args if a not in bind]
    if set(values) != set(own):
        return (
            f"{where}: {WITNESS_KEY} must give a value for each of its own "
            f"parameters ({', '.join(own) or 'none'}), not "
            f"{', '.join(sorted(map(str, values))) or 'none'}"
        )
    raw_allowed = check.get("values")
    allowed: Mapping[str, Any] = raw_allowed if isinstance(raw_allowed, Mapping) else {}
    for arg, value in values.items():
        if arg in allowed and value not in allowed[arg]:
            return f"{where}: {WITNESS_KEY}[{arg!r}] = {value!r} is not declared"
    if orgs[0] not in args or subjects[0] not in args:
        return f"{where}: the witness binds a parameter the method does not take"
    return TenantWitness(contract, method, args, orgs[0], subjects[0], dict(values))


def _first_segment(name: str) -> str:
    return name.split(".", 1)[0]


def _known_contracts(core: Any) -> set[str]:
    """Every contract this composition names: consumed, registered, bound
    remotely or assigned to a provider (no I/O)."""
    names: set[str] = set()
    for descriptor in getattr(core, "module_descriptors", None) or ():
        for use in getattr(descriptor, "consumes", None) or ():
            names.add(str(getattr(use, "name", use)))
    registry = getattr(core, "service_registry", None)
    if registry is not None:
        names.update(str(n) for n in registry.list())
    settings_obj = getattr(core, "settings", None)
    names.update(str(n) for n in getattr(settings_obj, "service_bindings", None) or {})
    names.update(getattr(core, "_access_assignment", None) or {})
    return names


def authority_contracts(core: Any, authority: str, path: str) -> list[str]:
    """The contracts the tenant authority answers for: those it owns (first
    segment) and those the root assigned to it (``access_providers``)."""
    assigned: Mapping[str, str] = getattr(core, "_access_assignment", None) or {}
    return sorted(
        name
        for name in _known_contracts(core)
        if _first_segment(name) == authority or assigned.get(name) == path
    )


def _declared(
    plan: Any, names: Iterable[str], owner: str
) -> tuple[dict[tuple[Any, ...], TenantWitness], list[str]]:
    """The distinct witnesses the expectations of ``names`` declare (only on
    a method the consumer uses), and why a declaration was unusable."""
    found: dict[tuple[Any, ...], TenantWitness] = {}
    problems: list[str] = []
    for name in names:
        for expectation in plan.expectations.get(name, ()):
            methods = (expectation.describe or {}).get("methods") or {}
            used = expectation.methods
            for method, doc in sorted(methods.items()):
                if (used is not None and method not in used) or not isinstance(
                    doc, Mapping
                ):
                    continue
                witness = witness_from_describe(name, str(method), doc)
                if isinstance(witness, str):
                    problems.append(witness)
                elif witness is not None:
                    found.setdefault(
                        witness.key(),
                        replace(witness, authority=owner, source=expectation.source),
                    )
    return found, problems


def resolve_tenant_witness(core: Any) -> tuple[TenantWitness | None, str]:
    """``(witness, why)``: the assigned tenant authority's membership witness,
    else ``None`` and why. Startup only: reads the composition's contract
    expectations (files), never on a request."""
    authorities = getattr(core, "claim_authorities", None)
    path = (getattr(authorities, "verified", None) or {}).get(TENANT_GRANT)
    owner = (getattr(authorities, "verified_owners", None) or {}).get(TENANT_GRANT)
    if not path or not owner:
        return None, "no module is assigned the tenant authority (tenant_ids)"
    names = authority_contracts(core, str(owner), str(path))
    if not names:
        return None, f"the tenant authority {owner!r} provides no contract here"
    plan = _compat.resolve_expectations(core, names)
    found, problems = _declared(plan, names, str(owner))
    if len(found) == 1:
        return next(iter(found.values())), ""
    if len(found) > 1:
        listed = ", ".join(sorted(w.describe() for w in found.values()))
        return None, (
            f"the tenant authority {owner!r} declares more than one membership "
            f"witness ({listed}): none is used"
        )
    why = f"the tenant authority {owner!r} declares no membership witness"
    if problems:
        why += " usable here (" + "; ".join(problems) + ")"
    else:
        why += f" on {', '.join(names)} (access_check(..., {WITNESS_KEY}={{...}}))"
    return None, why


def _uses_bound_tenant(policy: Any, seen: set[int] | None = None) -> bool:
    """Whether a policy tree states ``Account.bound_tenant()`` anywhere."""
    seen = set() if seen is None else seen
    if id(policy) in seen:
        return False
    seen.add(id(policy))
    account = getattr(policy, "account", None)
    if getattr(account, "kind", None) == "bound_tenant":
        return True
    parts: Iterable[Any] = getattr(policy, "parts", None) or ()
    inner = getattr(policy, "inner", None)
    children = [*parts, *([inner] if inner is not None else [])]
    return any(_uses_bound_tenant(child, seen) for child in children)


def bound_tenant_policies(app: Any) -> list[tuple[str, str]]:
    """``(route, policy)`` of every route whose policy uses
    ``Account.bound_tenant()`` (startup only)."""
    out: list[tuple[str, str]] = []
    seen: set[int] = set()

    def visit(dependant: Any, route: str) -> None:
        for sub in getattr(dependant, "dependencies", None) or ():
            policy = getattr(getattr(sub, "call", None), "__sandloom_policy__", None)
            if policy is not None and id(policy) not in seen:
                seen.add(id(policy))
                if _uses_bound_tenant(policy):
                    describe = getattr(policy, "describe", None)
                    label = describe() if callable(describe) else repr(policy)
                    out.append((route, str(label)))
            visit(sub, route)

    for route in getattr(app, "routes", None) or ():
        methods = ",".join(sorted(getattr(route, "methods", None) or ()))
        visit(getattr(route, "dependant", None), f"{methods} {route.path}".strip())
    return out


def report(core: Any, witness: TenantWitness | None, why: str) -> None:
    """The startup report (H-19): the witness in use, or every route policy
    whose ``Account.bound_tenant()`` cannot resolve an organization."""
    policies = bound_tenant_policies(getattr(core, "app", None))
    if witness is not None:
        logger.info(
            "tenant kind: organization membership is asked of %s (tenant authority "
            "%r, declared in %s)",
            witness.describe(),
            witness.authority,
            witness.source,
        )
        return
    for route, policy in policies:
        logger.warning(
            "tenant kind: %s requires %s with Account.bound_tenant(), but %s: an "
            "organization tenant's kind is 'unknown' and the policy is "
            "indeterminate (403 tenant_kind_unresolved). Assign the tenant "
            "authority in claim_authorities, consume its contract and run "
            "`sandloom generate`, or state of=Account.org(...) / Account.user()",
            route,
            policy,
            why,
        )


__all__ = [
    "TENANT_GRANT",
    "WITNESS_KEY",
    "TenantWitness",
    "authority_contracts",
    "bound_tenant_policies",
    "report",
    "resolve_tenant_witness",
    "witness_from_describe",
]
