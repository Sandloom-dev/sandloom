"""Sync service contracts and access facts on the :class:`CoreFacade`.

Registration of contract providers (wire-portability checked), contract
ownership and the database a contract runs on, the local-first
:meth:`CoreFacade.get_service` resolution (local provider, else the injected
remote resolver, else :class:`ServiceUnavailableInDeployment`), and the
access-fact registry with its ``<owner>.facts.v1`` exposure.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sandloom.core.contract_wire as _contract_wire
import sandloom.core.module_api as _module_api
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.facade._state import (
    _FacadeState,
)
from sandloom.core.module_api._service_registry import (
    ServiceUnavailableInDeployment,
    is_valid_service_name,
)

#: Every claims-contributing module exposes ``<module>.claims.v1``.
CLAIMS_CONTRACT_SUFFIX = ".claims.v1"


def claims_contract_name(owner: str) -> str:
    """``"tenancy"`` -> ``"tenancy.claims.v1"``."""
    return f"{owner}{CLAIMS_CONTRACT_SUFFIX}"


#: Every access-fact-owning module exposes ``<module>.facts.v1``.
FACTS_CONTRACT_SUFFIX = ".facts.v1"


def facts_contract_name(owner: str) -> str:
    """``"billing"`` -> ``"billing.facts.v1"``."""
    return f"{owner}{FACTS_CONTRACT_SUFFIX}"


def _short_module_name(name: str | None) -> str | None:
    """``sandloom_billing`` / ``myapp.modules.billing`` -> ``billing``."""
    if not name:
        return None
    return _module_name(name) or None


class _ServicesMixin(_FacadeState):
    """CoreFacade methods: see :mod:`sandloom.core.facade._services`."""

    def register_service(
        self, name: str, impl: Any, *, database: str | None = None
    ) -> None:
        """Register a sync service contract.

        The registering module (if any) is recorded as the contract's owner so
        the contract runtime opens the provider's Unit of Work on the database
        that module declares; ``database`` overrides it explicitly.
        """
        problems = self.contract_dto_problems(name, impl)
        if problems:
            raise _contract_wire.ContractTypeError("; ".join(problems))
        self.service_registry.register(name, impl)
        if self._registering_module is not None:
            self._service_owners[name] = self._registering_module
        if database is not None:
            self._service_databases[name] = database

    def contract_owner(self, name: str) -> str | None:
        """The module that registered contract ``name`` (``None`` = kernel)."""
        return self._service_owners.get(name)

    def contract_dto_problems(self, name: str, impl: Any) -> list[str]:
        """DTOs of a module-registered contract no caller could hold a copy of.

        See :func:`sandloom.core.module_api.contract_dto_problems`: a contract a
        module registers names DTOs of its own package (``sandloom generate``
        copies them into the shared DTO package) or of a shared package, never
        another module's class or a function-local one (SANDLOOM-C012).
        """

        return _module_api.contract_dto_problems(
            name,
            impl,
            provider_module=self._registering_module,
            module_packages=[
                *self._known_module_packages,
                *[
                    str(d.name)
                    for d in self.module_descriptors
                    if getattr(d, "name", None)
                ],
            ],
            shared_packages=getattr(self.settings, "contract_dto_packages", ()),
        )

    def contract_database(self, name: str) -> str:
        """The database the provider of contract ``name`` runs its UoW on.

        Explicit ``register_service(..., database=)`` wins; otherwise the
        owning module's declared database
        (``ModuleDescriptor.database_instances()``); else ``"default"``.
        """
        explicit = self._service_databases.get(name)
        if explicit is not None:
            return explicit
        owner = self._service_owners.get(name)
        if owner is not None:
            return self._module_database(owner)
        return "default"

    def _claims_owner_database(self, owner: str | None) -> str:
        """Database a claims owner's contributors run on (contract resolution).

        Same resolution as the contract runtime: the ``<owner>.claims.v1``
        contract's database when registered, else the declared database of the
        module that registered the contributor (or whose short name is
        ``owner``); ``"default"`` only when no owning module is known.
        """
        if owner is None:
            return "default"
        contract = claims_contract_name(owner)
        if contract in self._service_databases or contract in self._service_owners:
            return self.contract_database(contract)
        module = self._claims_owner_modules.get(owner)
        if module is not None:
            return self._module_database(module)
        for desc in self.module_descriptors:
            name = getattr(desc, "name", None)
            if _short_module_name(name) == owner and name is not None:
                return self._module_database(name)
        return "default"

    def _module_database(self, module_name: str) -> str:
        cached = self._module_database_cache.get(module_name)
        if cached is not None:
            return cached
        desc = self._module_descriptor_index.get(module_name)
        if desc is not None:
            instances = desc.database_instances()
            resolved = str(next(iter(sorted(instances)))) if instances else "default"
            self._module_database_cache[module_name] = resolved
            return resolved
        return "default"

    def local_contract(self, name: str) -> Any | None:
        """The locally provided contract ``name`` bound to the contract runtime.

        Every call on the returned object runs in the provider's own Unit of
        Work on its own database — the SAME code path the internal RPC server
        uses for a remote caller. ``None`` when not provided in this process.
        """
        impl = self.service_registry.try_get(name)
        if impl is None:
            return None
        database = self.contract_database(name)
        cached = self._bound_contracts.get(name)
        if cached is not None and cached[0] is impl and cached[1] == database:
            return cached[2]

        # One provider per (impl, database): building one per call allocated a
        # provider + method closures (a reference cycle) on every call.
        bound = _module_api.bind_contract(impl, core=self, database=database, name=name)
        self._bound_contracts[name] = (impl, database, bound)
        return bound

    def set_service_resolver(self, resolver: Callable[..., Any] | None) -> None:
        """Inject the remote-service-proxy factory (called at composition).

        ``sandloom.core.rpc.install_service_rpc_routes`` passes ``remote_service_proxy``
        here so ``get_service`` can build a remote fallback for a bound contract
        without the facade importing the RPC mechanism.
        """
        self._remote_service_resolver = resolver

    def get_service(self, name: str) -> Any:
        """Resolve a sync service contract (local provider first, then RPC).

        A local provider is returned bound to the contract runtime
        (:meth:`local_contract`), so local and remote calls share semantics:
        the provider runs in its own Unit of Work on its own database and
        never in the caller's transaction.
        """
        local = self.local_contract(name)
        if local is not None:
            return local
        # Local-first: when the provider module is not co-deployed, fall back to
        # a generated remote proxy IFF a binding is configured AND the RPC
        # resolver was wired in. The facade knows neither how to build the proxy
        # (injected) nor the concrete RPC module.
        binding = self.settings.service_bindings.get(name)
        resolver = self._remote_service_resolver
        if binding is not None and resolver is not None:
            timeout = self.settings.service_rpc_timeout_seconds
            remote = self._remote_proxies.get(name)
            if (
                remote is not None
                and remote[0] is resolver
                and remote[1] == binding
                and remote[2] == timeout
            ):
                return remote[3]
            proxy = resolver(
                name, binding, default_timeout_seconds=timeout, settings=self.settings
            )
            self._remote_proxies[name] = (resolver, dict(binding), timeout, proxy)
            return proxy
        # Neither local provider nor a wired remote binding: degrade explicitly
        # with a 501 (not a bare 500) so a consumer that does not pre-check with
        # try_get still surfaces a clean "not implemented in this deployment".
        raise ServiceUnavailableInDeployment(name)

    # -----------------
    # Access facts (authorization subsystem)
    # -----------------
    def register_access_fact(self, fact: Any) -> None:
        """Register an :class:`~sandloom.shared.authz.facts.AccessFact`.

        Binds a fact name (e.g. ``"subscription.active"``) to its claim /
        projection / live resolution paths so ``Entitled(name)`` policies can
        resolve it by configuration. Raises on a duplicate name.
        """
        name = fact.name
        if name in self._access_facts:
            raise ValueError(f"Access fact {name!r} is already registered")
        self._access_facts[name] = fact
        # Facts are part of the owner's PUBLIC contract: expose
        # ``<owner>.facts.v1`` (describe / evaluate) so a process without the
        # owner resolves the same fact over RPC. Built through a duck-typed
        # hook on the fact's class — the kernel never imports the authz
        # subsystem.
        owner = getattr(fact, "owner_module", None)
        build = getattr(type(fact), "build_facts_contract", None)
        if not isinstance(owner, str) or not owner or not callable(build):
            return
        contract_name = facts_contract_name(owner)
        if self.service_registry.contains(contract_name):
            return
        if not self._exposable(contract_name, "access fact owner", owner):
            return
        # Any other failure (the contract cannot be built / registered) aborts
        # the registering module's load: a silently local-only owner would
        # break every process that binds <owner>.facts.v1 remotely.
        self.register_service(contract_name, build(self, owner))

    def _exposable(self, contract_name: str, what: str, owner: str) -> bool:
        """``False`` (logged) when ``owner`` cannot name a contract.

        Only an unnameable owner (``contract_name`` fails the registry's
        naming convention, e.g. a dashed module name) stays local-only by
        design; every other auto-contract failure propagates.
        """
        if is_valid_service_name(contract_name):
            return True
        self.logger.warning(
            "%s %r cannot be exposed as %r (not a valid contract name); it "
            "stays local-only",
            what,
            owner,
            contract_name,
        )
        return False

    def get_access_fact(self, name: str) -> Any:
        """Return the LOCALLY registered access fact, or None.

        A fact owned by a module deployed elsewhere is resolved by the
        authorization subsystem through the owner's bound ``<owner>.facts.v1``
        (:meth:`remote_fact_owners`, cached in
        :attr:`remote_access_fact_cache`).
        """
        return self._access_facts.get(name)

    def list_access_facts(self) -> dict[str, Any]:
        return dict(self._access_facts)

    def remote_fact_owners(self) -> list[str]:
        """Owners whose ``<owner>.facts.v1`` is bound remotely (not local)."""
        owners: list[str] = []
        for name in self.settings.service_bindings:
            if not name.endswith(FACTS_CONTRACT_SUFFIX):
                continue
            owner = name[: -len(FACTS_CONTRACT_SUFFIX)]
            if owner and not self.service_registry.contains(name):
                owners.append(owner)
        return sorted(owners)

    @property
    def remote_access_fact_cache(self) -> dict[Any, Any]:
        """Per-app cache of remote owners' fact descriptors (authz-managed)."""
        return self._remote_access_fact_cache
