"""Who may provide an access-bearing contract in-process (ADR-0014 decision 6).

A contract the authorization layer asks to decide access is a trust decision
(I-22): a module that registers it answers every check that asks it. So an
**access-bearing** contract is registered in-process only by the module its
first segment names, or by the module the composition root assigns:
``create_app(access_providers={"tenancy.membership.v1": "acme_orgs"})``. A
value is a module import path or a ``sandloom.modules`` entry-point name.
Remotely, ``SERVICE_BINDINGS`` is the assignment: the binding is pinned to the
provider's ``service_id`` and every call is authenticated with the pair MAC
(ADR-0004). Any other contract name keeps the first-registrant rule (H-17).

Which names are access-bearing is read from the consumer's side of this
composition, never from a list of known contracts in the kernel:

* (a) the keys of ``access_providers``;
* (b) the contract of each generated access check a consumer's generated
  copy carries and the process imported
  (:func:`sandloom.shared.authz.contract_check.generated_access_contracts`):
  the literal names it, never a list in the kernel, so a third party's
  ``acme.seats.v1`` is access-bearing with no kernel edit;
* (c) a contract whose registered implementation, or the declared
  ``@contract`` class it derives from, carries an ``@access_check`` method
  (:func:`sandloom.shared.authz.contract_check.declares_access_checks`),
  at any registration attempt in this process: a provider-only process (no
  consumer imported the generated atoms) classifies it too, so a module
  other than its provider cannot first-register it.

A provider cannot declassify its own contract by omitting a declaration. The
provider is checked by its real identity: its loaded import path, not only
its short name (:func:`sandloom.core.facade._services.access_provider_problem`).

:func:`install_access_providers` binds the assignment before any module loads
(a registration by anyone else raises ``ForeignOwnerRegistration``, which
refuses that module's load) and :func:`enforce_access_providers` sweeps every
registration again after the load, since a later module may import a
generated copy naming a contract registered earlier, and once more as a
critical startup task before the app serves (a copy imported after
``create_app``).
All refuse with SANDLOOM-B034, B034's family widened; all run at boot only,
never on a request.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import sandloom.shared.authz.contract_check as _contract_check
from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
from sandloom.core.descriptors.module_names import (
    AmbiguousModuleError,
    installed_modules,
    resolve_module_names,
)
from sandloom.core.facade._services import (
    ForeignOwnerRegistration,
    reserved_contract_owner,
)
from sandloom.core.module_api._service_registry import is_valid_service_name

#: The refusal code of every problem here (B034's family, widened).
CODE = "SANDLOOM-B034"


class AccessProviderError(ValueError):
    """An ``access_providers`` entry the composition cannot take."""


def _resolve(entry: str) -> str:
    """The import path ``entry`` names: an entry-point name's path, else
    ``entry`` itself (as ``claim_authorities`` resolves a value)."""
    if any(m.name == entry for m in installed_modules()):
        return resolve_module_names([entry])[0]
    return entry


def build_access_providers(mapping: Mapping[str, str] | None) -> dict[str, str]:
    """Resolve a composition root's ``access_providers`` (contract -> path).

    Refuses (:class:`AccessProviderError`) an entry that is not
    ``{contract name: module}``, a key outside the contract naming convention,
    a reserved ``<m>.claims.v1`` / ``<m>.facts.v1`` name (always its own
    module's, never assignable) and an ambiguous entry-point name.
    """
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise AccessProviderError(
            f"access_providers must be a mapping of contract name to module, "
            f"got {type(mapping).__name__}"
        )
    resolved: dict[str, str] = {}
    for contract, module in mapping.items():
        if (
            not isinstance(contract, str)
            or not isinstance(module, str)
            or not module
            or not is_valid_service_name(contract)
        ):
            raise AccessProviderError(
                f"access_providers[{contract!r}] = {module!r}: each key is a "
                "contract name (<module>.<name>.v<N>) and each value a module "
                "import path or entry-point name"
            )
        if reserved_contract_owner(contract) is not None:
            raise AccessProviderError(
                f"access_providers[{contract!r}]: {contract!r} is a reserved "
                "claims/facts contract, provided only by the module it names; "
                "drop the entry"
            )
        try:
            resolved[contract] = _resolve(module)
        except AmbiguousModuleError as exc:
            raise AccessProviderError(
                f"access_providers[{contract!r}] = {module!r} is ambiguous: {exc}"
            ) from exc
    return resolved


def _refusal(problems: list[str], log: logging.Logger) -> ConfigurationError:
    for problem in problems:
        log.error("access providers: %s", problem)
    return ConfigurationError(
        INVALID_CONFIGURATION
        + f"{CODE}: "
        + "; ".join(problems)
        + f" (`sandloom explain {CODE}`)"
    )


def install_access_providers(
    core_facade: Any,
    *,
    access_providers: Mapping[str, str] | None,
    log: logging.Logger,
) -> dict[str, str]:
    """Bind the root's assignment to the facade before the modules load.

    Raises :class:`ConfigurationError` (SANDLOOM-B034) on a malformed map.
    Returns the resolved assignment.
    """
    try:
        assigned = build_access_providers(access_providers)
    except AccessProviderError as exc:
        raise _refusal([str(exc)], log) from exc
    keys = frozenset(assigned)

    def access_bearing() -> frozenset[str]:
        return keys | _contract_check.generated_access_contracts()

    core_facade._assign_access_providers(
        assigned, access_bearing, declares=_contract_check.declares_access_checks
    )
    return assigned


def enforce_access_providers(core_facade: Any, *, log: logging.Logger) -> None:
    """Refuse the boot when a registered access-bearing contract's registrant
    is not its provider (SANDLOOM-B034), after every module has loaded.

    The same sweep runs again as a critical startup task, before the app
    serves: a generated copy imported after ``create_app`` returned
    classifies its contract too.
    """
    problems = core_facade.access_provider_problems()
    if problems:
        raise _refusal(problems, log)

    async def recheck_access_providers() -> None:
        late = core_facade.access_provider_problems()
        if late:
            for problem in late:
                log.error("access providers: %s", problem)
            raise ForeignOwnerRegistration(
                f"{CODE}: " + "; ".join(late) + f" (`sandloom explain {CODE}`)"
            )

    core_facade.add_startup_task(recheck_access_providers, critical=True, phase="infra")


__all__ = [
    "CODE",
    "AccessProviderError",
    "build_access_providers",
    "enforce_access_providers",
    "install_access_providers",
]
