"""Who answers an operator command, confirmed before its first side effect.

An operator command (``sandloom security ...``, ``sandloom admin ...``) acts
on what a module owns. The kernel names no such module (ADR-0012 decision 1):
:func:`operator_owner` reads the CANDIDATE owner of a capability from the
composition root's claim-authority map, and the command asks that module's
reserved ``<owner>.security.v1`` (ADR-0013 decision 4: only module ``owner``
registers it, SANDLOOM-B034; no kernel shape, every contract rule applies).

* sessions, accounts, credentials and the email-to-user lookup: the map's
  ``issuer``;
* platform-administrator roles (grant, list, first-admin check): the
  authority of ``roles``;
* tenant member enumeration: the authority of ``tenant_ids``.

The map is read whole (:attr:`~sandloom.core.claims.ClaimAuthorities.verified`,
``declared_issuer``, ``names``), so a process of a split that does not load
the owner resolves the same owner as the one that does. The owner's public
name comes from the declaration (``Authority(name, module=path)``), an
installed entry point or a module loaded here, never from the last import
segment.

A claim authority is a candidate, not proof: :func:`confirm_operator_capability`
confirms each method by describe (the local schema in one process, the bound
provider's live describe in a split), checks that the binding names the
owner's own service and that the provider is the module the map names, and
refuses otherwise (SANDLOOM-B039 / SANDLOOM-B040, exit status 1) before
anything is done, a dry run included. Operator paths only: nothing here runs
at import or on a request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import sandloom.core.rpc.proxy as _proxy
import sandloom.core.rpc_identity as _rpc_identity
from sandloom.core.claims import ISSUER_ENTRY, security_contract_name
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc_transport import RpcResponseBindingError

#: The refusal of a capability that cannot be confirmed.
CODE = "SANDLOOM-B039"
#: The refusal of a binding or provider that is not the named owner.
SUBSTITUTION_CODE = "SANDLOOM-B040"

#: Operator capability -> the claim-map entry whose authority is its
#: candidate owner.
OPERATOR_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "sessions": ISSUER_ENTRY,
        "accounts": ISSUER_ENTRY,
        "credentials": ISSUER_ENTRY,
        "user_lookup": ISSUER_ENTRY,
        "platform_admin": "roles",
        "tenant_members": "tenant_ids",
    }
)


class OperatorCapabilityUnavailable(SystemExit):
    """SANDLOOM-B039: the owner of an operator capability cannot be confirmed
    to provide it (no owner, unknown name, contract neither local nor bound,
    a missing method, a missing ``RPC_ALLOWED_CALLS`` grant). A
    :class:`SystemExit` (status 1): the command stops before acting."""

    sandloom_code = CODE

    def __init__(self, message: str) -> None:
        super().__init__(f"error: {CODE}: {message} (`sandloom explain {CODE}`)")


class OperatorOwnerSubstitution(SystemExit):
    """SANDLOOM-B040: the service that would answer ``<owner>.security.v1``
    is not the owner the claim map names. A :class:`SystemExit` (status 1)."""

    sandloom_code = SUBSTITUTION_CODE

    def __init__(self, message: str) -> None:
        super().__init__(
            f"error: {SUBSTITUTION_CODE}: {message} "
            f"(`sandloom explain {SUBSTITUTION_CODE}`)"
        )


@dataclass(frozen=True)
class OperatorOwner:
    """The candidate owner of one operator capability.

    ``command``: the operator command (refusals name it); ``key``: the
    claim-map entry it was read from; ``module``: the owner's public name;
    ``path``: its import path.
    """

    command: str
    capability: str
    key: str
    module: str
    path: str

    @property
    def contract(self) -> str:
        """The owner's operator contract, ``<module>.security.v1``."""
        return security_contract_name(self.module)

    def label(self) -> str:
        """``"owner 'issuer' = acme_idp (oidcapp.modules.acme_idp)"``."""
        return f"owner {self.key!r} = {self.module} ({self.path})"


def _loaded_paths(core: Any) -> set[str]:
    return {
        str(name)
        for name in (getattr(d, "name", None) for d in core.module_descriptors)
        if name
    }


def _assignment_fix(key: str) -> str:
    return (
        f"assign it in the composition root: create_app(claim_authorities="
        f"{{{key!r}: Authority('<name>', module='<import path>'), ...}})"
    )


def operator_owner(core: Any, capability: str, *, command: str) -> OperatorOwner:
    """The candidate owner of ``capability`` (:data:`OPERATOR_CAPABILITIES`)
    in ``core``'s composition, else :class:`OperatorCapabilityUnavailable`.

    Reads the root's whole claim-authority declaration; no I/O. The owner is
    not yet confirmed: call :func:`confirm_operator_capability` before the
    first side effect.
    """
    key = OPERATOR_CAPABILITIES.get(capability)
    if key is None:
        raise ValueError(
            f"unknown operator capability {capability!r}: one of "
            f"{', '.join(sorted(OPERATOR_CAPABILITIES))}"
        )
    authorities = core.claim_authorities
    if not authorities.assigned:
        raise OperatorCapabilityUnavailable(
            f"{command}: the composition root passes no claim_authorities map, "
            f"so nothing owns {capability} (the {key!r} entry); "
            f"{_assignment_fix(key)}"
        )
    path = (
        authorities.declared_issuer
        if key == ISSUER_ENTRY
        else (authorities.verified or {}).get(key)
    )
    if not path:
        raise OperatorCapabilityUnavailable(
            f"{command}: no owner is assigned for {key!r} in the composition "
            f"root's claim_authorities, so nothing here owns {capability}; "
            f"{_assignment_fix(key)}"
        )
    name = authorities.names.get(path)
    if name is None and path in _loaded_paths(core):
        # Loaded here by that path: its name is the loader's.
        name = module_name(path)
    if name is None:
        raise OperatorCapabilityUnavailable(
            f"{command}: owner {key!r} = {path!r} is neither installed nor "
            "loaded in this process, and the composition root declares no "
            "public name for it (its last import segment is not its name): "
            f"write Authority('<name>', module={path!r}) in claim_authorities"
        )
    return OperatorOwner(
        command=command, capability=capability, key=key, module=name, path=path
    )


def _missing_methods(owner: OperatorOwner, missing: Sequence[str]) -> str:
    listed = ", ".join(missing)
    return (
        f"{owner.command}: {owner.label()}; {owner.contract} has no {listed}. "
        "Do it at the external provider, or add the method to "
        f"{owner.module}."
    )


def _grant_fix(core: Any, owner: OperatorOwner, methods: Sequence[str]) -> str:
    caller = _rpc_identity.service_id(core.settings) or "<this service>"
    listed = ", ".join(repr(m) for m in methods)
    return (
        f"grant it in RPC_ALLOWED_CALLS: {{{caller!r}: {{{owner.contract!r}: "
        f"[{listed}, ...]}}}}"
    )


def forbidden_refusal(
    core: Any, owner: OperatorOwner, method: str
) -> OperatorCapabilityUnavailable:
    """The refusal of a 403 answer to ``method``: a missing
    ``RPC_ALLOWED_CALLS`` grant (for a command that catches it on a call)."""
    return OperatorCapabilityUnavailable(
        f"{owner.command}: {owner.label()}; the provider of {owner.contract} "
        f"refuses {method} to this service (403): "
        f"{_grant_fix(core, owner, [method])}"
    )


def _confirm_local(core: Any, owner: OperatorOwner, methods: Sequence[str]) -> Any:
    registrant = core.contract_owner(owner.contract)
    if registrant != owner.path:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}, but {owner.contract} is "
            f"registered here by {registrant or 'the kernel'!r}: only the module "
            "the claim map names answers its operator contract"
        )
    service = core.local_contract(owner.contract)
    have = set(service.contract_schema.methods)
    missing = [m for m in methods if m not in have]
    if missing:
        raise OperatorCapabilityUnavailable(_missing_methods(owner, missing))
    return service


def _check_binding(core: Any, owner: OperatorOwner) -> str:
    """The pinned ``service_id`` of ``owner``'s bound operator contract."""
    bindings: Mapping[str, Mapping[str, Any]] = core.settings.service_bindings
    binding = bindings.get(owner.contract)
    if binding is None:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} is neither "
            "registered in this process nor bound: run the command where "
            f"{owner.module} is loaded, or bind {owner.contract} in "
            "SERVICE_BINDINGS (with an RPC_ALLOWED_CALLS grant)"
        )
    pinned = binding.get("service_id")
    if not pinned:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the binding of {owner.contract} "
            "is pinned to no service_id: pin it to the owner's service"
        )
    prefix = f"{owner.module}."
    others = sorted(
        (name, str(other.get("service_id")))
        for name, other in bindings.items()
        if name.startswith(prefix)
        and name != owner.contract
        and other.get("service_id") != pinned
    )
    if others:
        listed = ", ".join(f"{name} -> {sid!r}" for name, sid in others)
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the binding of {owner.contract} "
            f"names service {pinned!r}, but the owner's other bindings name "
            f"another ({listed}): bind {owner.contract} to the owner's own "
            "service"
        )
    return str(pinned)


async def _describe(core: Any, owner: OperatorOwner, pinned: str) -> Any:
    try:
        service = core.get_service(owner.contract)
    except ServiceUnavailableInDeployment as exc:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} is bound but "
            "this process cannot call it (no RPC client is wired)"
        ) from exc
    if not isinstance(service, _proxy.RemoteServiceProxy):
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} resolves to no "
            "bound provider here"
        )
    try:
        description = await _proxy.describe_provider(service)
    except RpcResponseBindingError as exc:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the describe answer of "
            f"{owner.contract} is not bound to the pinned service {pinned!r}"
        ) from exc
    except Exception as exc:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} at service "
            f"{pinned!r} cannot be described ({type(exc).__name__}): nothing "
            "was done; retry when it answers"
        ) from exc
    return service, description


async def confirm_operator_capability(
    core: Any, owner: OperatorOwner, methods: Sequence[str]
) -> Any:
    """``owner``'s operator contract, confirmed to provide ``methods``.

    In one process: the contract registered here by the module from
    ``owner.path`` (another registrant: :class:`OperatorOwnerSubstitution`),
    its schema listing every method. Else its ``SERVICE_BINDINGS`` entry:
    pinned to the same ``service_id`` as the owner's other bindings, every
    method granted to this service in ``RPC_ALLOWED_CALLS``, and the
    provider's live describe (bound to that service) stating ``owner.path``
    and listing every method. Any gap refuses (SANDLOOM-B039 / B040, exit 1)
    before the caller acts. Returns the contract to call (the local bound
    provider or the remote proxy).
    """
    if core.service_registry.try_get(owner.contract) is not None:
        return _confirm_local(core, owner, methods)
    if owner.path in _loaded_paths(core):
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()} is loaded here and registers no "
            f"{owner.contract}. Do it at the external provider, or add "
            f"{owner.contract} to {owner.module}."
        )
    pinned = _check_binding(core, owner)
    caller = _rpc_identity.service_id(core.settings) or ""
    ungranted = [
        m
        for m in methods
        if not _rpc_identity.authorize(core.settings, caller, owner.contract, m)
    ]
    if ungranted:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; this service "
            f"({caller or 'no RPC_SERVICE_ID'}) may not call "
            f"{owner.contract}.{', '.join(ungranted)}: "
            f"{_grant_fix(core, owner, ungranted)}"
        )
    service, description = await _describe(core, owner, pinned)
    if description.status == 403:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; service {pinned!r} refuses "
            f"{owner.contract} to this service (403): "
            f"{_grant_fix(core, owner, methods)}"
        )
    if description.status != 200:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; service {pinned!r} does not "
            f"serve {owner.contract} (HTTP {description.status}"
            f"{' ' + description.code if description.code else ''}): register "
            f"it in {owner.module}, or bind it to the service that runs "
            f"{owner.module}"
        )
    if description.provider != owner.path:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}, but service {pinned!r} answers "
            f"{owner.contract} for {description.provider or 'an unstated module'!r}"
            ": bind it to the service that runs the module the claim map names"
        )
    missing = [m for m in methods if m not in description.methods]
    if missing:
        raise OperatorCapabilityUnavailable(_missing_methods(owner, missing))
    return service


async def operator_contract(
    core: Any, capability: str, methods: Sequence[str], *, command: str
) -> tuple[OperatorOwner, Any]:
    """:func:`operator_owner` then :func:`confirm_operator_capability`:
    ``(owner, contract)``, or a refusal before anything is done."""
    owner = operator_owner(core, capability, command=command)
    return owner, await confirm_operator_capability(core, owner, methods)


__all__ = [
    "CODE",
    "OPERATOR_CAPABILITIES",
    "SUBSTITUTION_CODE",
    "OperatorCapabilityUnavailable",
    "OperatorOwner",
    "OperatorOwnerSubstitution",
    "confirm_operator_capability",
    "forbidden_refusal",
    "operator_contract",
    "operator_owner",
]
