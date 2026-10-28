"""Module descriptor types — the data a module hands the framework at load.

A module's ``register(core)`` returns a :class:`ModuleDescriptor` describing its
name, version, routes, health check, migrations, and the infrastructure
:class:`CapabilityRequirement`s it needs. The loader consumes these to wire the
module and preflight its dependencies, producing a :class:`ModuleLoadSummary`.

This is part of the stable public API (``sandloom.core.descriptors``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from fastapi import APIRouter

from sandloom.core.descriptors.module_names import module_name

__all__ = [
    "HealthStatus",
    "HealthResult",
    "HealthCheck",
    "CapabilityRequirement",
    "ContractRequirement",
    "ModuleDescriptor",
    "SkippedModule",
    "FailedModule",
    "ModuleLoadSummary",
    "MigrationTargetError",
]

HealthStatus = Literal["ok", "degraded", "fail"]

_logger = logging.getLogger("sandloom.core.descriptors")


class MigrationTargetError(ValueError):
    """A module's migration target database cannot be determined unambiguously."""


@dataclass(slots=True)
class HealthResult:
    """Outcome of a module health check: a status plus optional details."""

    status: HealthStatus
    details: dict[str, object] | None = None


HealthCheck = Callable[[], Awaitable[HealthResult]]


@dataclass(frozen=True)
class CapabilityRequirement:
    """An infrastructure capability a module needs to function.

    Declared on the :class:`ModuleDescriptor` so the loader can preflight — at
    startup, before serving traffic — that the capability exists, has a
    configured instance backed by a registered driver, and that the driver
    supports every required feature. A ``required`` requirement that cannot be
    satisfied fails the module load (and aborts startup if the module itself is
    required); an optional one only logs a warning.
    """

    capability: str
    instance: str = "default"
    version: str = "v1"
    features: frozenset[str] = field(default_factory=frozenset)
    required: bool = True


@dataclass(frozen=True)
class ContractRequirement:
    """A sync service contract a module CALLS (``core.get_service(name)``).

    Declared on :attr:`ModuleDescriptor.consumes` so the loader can check at
    startup — not at the first request — that every consumed contract is
    either provided by a module loaded in this process or bound remotely in
    ``SERVICE_BINDINGS``. A ``required`` contract that is neither aborts
    startup (``ContractBindingError``); an optional one (the module degrades
    explicitly without it) only logs a warning. Access facts owned elsewhere
    are consumed through the owner's ``<owner>.facts.v1`` contract:
    ``ContractRequirement("billing.facts.v1")``.
    """

    name: str
    required: bool = True
    #: Why the module needs it (shown in diagnostics / startup errors).
    reason: str = ""
    #: The methods this module calls. Narrows the rolling-upgrade
    #: compatibility check (sandloom.core.contract_compat) to them, so a breaking
    #: change to a method the module never calls does not fail its readiness.
    #: Empty = every method of the contract's snapshot.
    methods: tuple[str, ...] = ()


@dataclass(slots=True)
class ModuleDescriptor:
    """What a module returns from ``register(core)`` to describe itself.

    Carries the module's identity (``name``/``version``), whether it is
    ``optional`` (modules are required by default: a load failure aborts
    startup unless the module says ``optional=True``), its ``routes``, an
    optional ``health_check``, its ``migrations_path`` and ``depends_on``
    ordering, and the ``capabilities`` it needs. Reference other modules'
    entities by plain UUID — never declare cross-module foreign keys.
    """

    name: str
    version: str
    optional: bool = False
    routes: APIRouter | None = None
    health_check: HealthCheck | None = None
    # Optional path to module-specific migrations directory (auto-detected Phase I)
    migrations_path: str | None = None
    # Optional dependencies (module names) for migration ordering (Phase I)
    depends_on: list[str] = field(default_factory=lambda: cast(list[str], []))
    # Infrastructure capabilities this module requires. Preflighted at load. A
    # module whose tables live in a non-default database declares it here, e.g.
    # ``CapabilityRequirement("database", instance="analytics")``.
    capabilities: list[CapabilityRequirement] = field(
        default_factory=lambda: cast(list["CapabilityRequirement"], [])
    )

    # Explicit database instance that hosts this module's tables (where its
    # migration chain is applied). ``None`` = inferred (see database_instances).
    # Declare it whenever the module USES several databases, e.g. reads
    # ``default`` but owns tables in ``analytics``.
    migrations_database: str | None = None
    # Explicit migration chain key: names the per-chain version table
    # (``alembic_version_<key>``). ``None`` = the last dotted segment of
    # ``name`` (``sandloom_billing`` -> ``billing``). Set it
    # when two loaded modules share a last segment
    # (``sandloom_billing`` vs ``myapp.modules.billing``).
    migrations_chain: str | None = None
    # Sync service contracts this module calls (``core.get_service``). The
    # loader preflights each against local providers + SERVICE_BINDINGS.
    consumes: list[ContractRequirement] = field(
        default_factory=lambda: cast(list["ContractRequirement"], [])
    )
    # This module MINTS access tokens (``create_access_token``) — identity. With
    # an asymmetric JWT_ALGORITHM the kernel requires JWT_PRIVATE_KEY in a
    # process that loads an issuer and refuses it (and JWT_SECRET) in every
    # other process: a verify-only service must not be able to mint.
    issues_tokens: bool = False

    def effective_capabilities(self) -> list[CapabilityRequirement]:
        """Capabilities to preflight at load (the declared list)."""
        return list(self.capabilities)

    def migration_chain_key(self) -> str:
        """Key of this module's migration chain (version-table suffix)."""
        raw = self.migrations_chain or module_name(self.name)
        return raw.lower().replace("-", "_").replace(".", "_")

    def database_instances(self) -> set[str]:
        """The database instance this module's tables live in (one element).

        Resolution:

        1. ``migrations_database`` when set (explicit, always wins);
        2. otherwise the instances of the REQUIRED ``database`` requirements:
           none -> ``default``; exactly one -> that one; several -> ``default``
           if it is among them (a module that reads ``default`` and also uses
           ``analytics`` keeps its tables in ``default``), else ambiguous ->
           :class:`MigrationTargetError` (declare ``migrations_database``).

        Optional (``required=False``) database requirements never host the
        chain, so an optional, unconfigured instance cannot break
        ``upgrade-all``. A module's chain is applied to exactly ONE database —
        applying it to every declared instance duplicated its tables.
        """
        if self.migrations_database:
            return {self.migrations_database}
        required = sorted(
            {
                r.instance or "default"
                for r in self.capabilities
                if r.capability == "database" and r.required
            }
        )
        if not required:
            return {"default"}
        if len(required) == 1:
            return {required[0]}
        if "default" in required:
            _logger.warning(
                "module %s declares several databases %s; migrating its tables "
                "into 'default'. Set ModuleDescriptor.migrations_database to "
                "make the target explicit.",
                self.name,
                required,
            )
            return {"default"}
        raise MigrationTargetError(
            f"module {self.name!r} declares several databases {required} and "
            "none is 'default'; set ModuleDescriptor.migrations_database to the "
            "instance that hosts its tables"
        )


@dataclass(slots=True)
class SkippedModule:
    """A module the loader skipped, with the reason why."""

    name: str
    reason: str  # 'list' | 'duplicate'


@dataclass(slots=True)
class FailedModule:
    """A module that failed to load, with its error and whether it was required."""

    name: str
    error: str
    required: bool


@dataclass(slots=True)
class ModuleLoadSummary:
    """Aggregate result of a load pass: loaded, skipped, failed, and duplicates."""

    # Use lambdas to keep precise element types for static analysis
    loaded: list[ModuleDescriptor] = field(default_factory=lambda: [])
    skipped: list[SkippedModule] = field(default_factory=lambda: [])
    failed: list[FailedModule] = field(default_factory=lambda: [])
    duplicates: list[str] = field(default_factory=lambda: [])
    explicit: list[str] = field(default_factory=lambda: [])
    #: Consumed-contract preflight: module -> [{contract, required, resolution}]
    #: where resolution is "local" | "remote" | "missing".
    consumes: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {})
    #: Module name -> its ``sandloom.core.loader.manifest.ModuleManifest`` (spec
    #: §5.7); ``AppManifest.from_summary`` assembles them in load order.
    manifests: dict[str, Any] = field(default_factory=lambda: {})

    def success(self) -> bool:
        return all(not f.required for f in self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded": [d.name for d in self.loaded],
            "skipped": [{"name": s.name, "reason": s.reason} for s in self.skipped],
            "failed": [
                {"name": f.name, "error": f.error, "required": f.required}
                for f in self.failed
            ],
            "duplicates": list(self.duplicates),
            "explicit": list(self.explicit),
            "consumes": {k: [dict(e) for e in v] for k, v in self.consumes.items()},
            "success": self.success(),
        }


# --------------------------------------------------------------------------- #
# Saga definitions (ADR-0008) — the DSL lives in ``.sagas``
# --------------------------------------------------------------------------- #
from sandloom.core.descriptors.sagas import (  # noqa: E402 - the DSL builds on the types above
    BACKWARD,
    FORWARD,
    CommandStep,
    Direction,
    LocalStep,
    Saga,
    SagaContext,
    SagaDefinitionError,
    SagaStep,
    StepKind,
    local_step,
    operation_key,
    saga_step_key,
    step,
    synthetic_instance,
)


def saga_contract_requirements(saga: Saga[Any]) -> list[ContractRequirement]:
    """One ``ContractRequirement(name, methods=...)`` per contract it calls.

    ``register_saga`` adds these to the module's ``consumes``, so a missing
    provider is a ``ContractBindingError`` at startup like any consumed
    contract, and the rolling-upgrade check covers exactly these methods.
    """
    methods: dict[str, set[str]] = {}
    for item in saga.steps:
        if isinstance(item, CommandStep):
            methods.setdefault(item.contract, set()).add(item.method)
            if item.compensate_contract and item.compensate_method:
                methods.setdefault(item.compensate_contract, set()).add(
                    item.compensate_method
                )
    return [
        ContractRequirement(
            name, reason=f"saga {saga.name!r} steps", methods=tuple(sorted(found))
        )
        for name, found in sorted(methods.items())
    ]


__all__ += [
    "BACKWARD",
    "FORWARD",
    "CommandStep",
    "Direction",
    "LocalStep",
    "Saga",
    "SagaContext",
    "SagaDefinitionError",
    "SagaStep",
    "StepKind",
    "local_step",
    "operation_key",
    "saga_contract_requirements",
    "saga_step_key",
    "step",
    "synthetic_instance",
]
