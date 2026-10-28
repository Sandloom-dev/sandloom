"""Sagas: a small orchestrator over the existing effect primitives (ADR-0008).

A module declares linear, versioned definitions (``sandloom.core.module_api``:
``Saga``, ``step``, ``local_step``) and registers them in ``setup`` with
``core.register_saga(DEFINITION)``. This package is the runtime:

* :func:`install_sagas` — wired by the composition root after the modules
  loaded: checks every definition against its contracts
  (:mod:`.registry`), installs one advance consumer per saga-owning module
  (``<module>:sagas@advance`` on ``<module>.saga_advance.v1``,
  ``durable=False`` — nudges are versioned, the row lock is the guard —
  ``dead_letter=False``), and the lease-guarded timer sweep (:mod:`.sweep`);
* :func:`start_saga` / :func:`saga_status` (re-exported by ``sandloom.core.di``) —
  create-or-get inside the caller's Unit of Work;
* the step protocol and recovery rules (:mod:`.runtime`), the outcome table
  (:mod:`.outcomes`), operator commands (:mod:`.ops`, ``sandloom sagas``).

Layer (ADR-0007): contracts & RPC, a sibling of ``module_api`` —
``module_api`` never imports this package (``register_saga`` only stores the
definition on the facade).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

import sandloom.core.runtime as _runtime
from sandloom.core.consumers.callbacks import EventConsumerDescriptor
from sandloom.core.descriptors import Saga
from sandloom.core.sagas import ops as _ops
from sandloom.core.sagas.channel import SystemChannel, system_channel_problem
from sandloom.core.sagas.outcomes import Outcome, StepOutcome, classify_exception
from sandloom.core.sagas.registry import (
    ADVANCE_CONSUMER,
    NUDGE_SCHEMA,
    RegisteredSaga,
    SagaRegistry,
    build_registry,
    nudge_channel,
)
from sandloom.core.sagas.runtime import (
    SagaHandle,
    SagaInputConflict,
    SagaNotInstalled,
    SagaRuntime,
)
from sandloom.core.sagas.sweep import SagaSweeper

logger = logging.getLogger("sandloom.sagas")


def install_sagas(
    core: Any, *, system_channel: SystemChannel | None = None
) -> SagaRuntime:
    """Check, bind and wire every saga registered on ``core`` (startup).

    ``system_channel`` is the kernel's system channel (``create_app`` passes
    ``sandloom.core.drivers.system_db``'s): cross-tenant work on a ``tenant_rls``
    database goes through it. Raises
    :class:`~sandloom.core.descriptors.SagaDefinitionError` (startup aborts) when a
    definition does not match its contracts. Idempotent per facade.
    """
    existing = getattr(core, "saga_runtime", None)
    if isinstance(existing, SagaRuntime):
        return existing
    registry = build_registry(core)
    runtime = SagaRuntime(core, registry, system_channel=system_channel)
    sweeper = SagaSweeper(runtime)
    core.saga_runtime = runtime
    core.saga_sweeper = sweeper
    if not len(registry):
        return runtime
    _install_consumers(core, runtime)

    async def saga_system_channel_check() -> None:
        problems = [
            p
            for p in (
                system_channel_problem(core, db, system_channel)
                for db in registry.databases()
            )
            if p is not None
        ]
        if problems:
            raise RuntimeError("; ".join(problems))

    async def saga_unknown_definitions_report() -> None:
        await _report_unknown_definitions(runtime)

    core.add_startup_task(saga_system_channel_check, critical=True)
    core.add_startup_task(saga_unknown_definitions_report)
    core.add_startup_task(sweeper.start)
    core.add_shutdown_task(sweeper.stop)
    return runtime


def _install_consumers(core: Any, runtime: SagaRuntime) -> None:
    """One advance consumer (+ nudge schema) per saga-owning module."""
    for module, (short, database) in sorted(runtime.registry.modules().items()):
        channel = nudge_channel(short)
        if core.get_event_schema(channel) is None:
            core.register_event_schema(channel, channel, NUDGE_SCHEMA)
        core.register_event_consumer(
            EventConsumerDescriptor(
                name=ADVANCE_CONSUMER,
                channel=channel,
                handler=runtime.handle_nudge,
                durable=False,
                database=database,
                required=True,
                module=module,
                dead_letter=False,
            )
        )


async def _report_unknown_definitions(runtime: SagaRuntime) -> None:
    """Startup report: active instances whose (type, version) is not here."""
    for database in runtime.registry.databases():
        try:
            found = await _ops.attention(runtime, database, actor="kernel:sagas")
        except Exception:
            logger.warning(
                "saga startup report on database/%s failed", database, exc_info=True
            )
            continue
        if found.unknown:
            logger.warning(
                "active saga instances of unregistered versions on database/%s "
                "(they park as unknown_definition): %s — register those versions "
                "alongside the new ones",
                database,
                ", ".join(found.unknown),
            )


def runtime_of(core: Any = None) -> SagaRuntime:
    """The saga runtime of ``core`` (default: the active core)."""
    target = core if core is not None else _runtime.get_active_core()
    found = getattr(target, "saga_runtime", None)
    if not isinstance(found, SagaRuntime):
        raise SagaNotInstalled(
            "no saga runtime on the active core: sagas are installed by the "
            "composition root (create_app / load_composition_core) after the "
            "modules registered them"
        )
    return found


async def start_saga(
    definition: Saga[Any],
    *,
    saga_id: object,
    input: BaseModel | Mapping[str, Any],
) -> SagaHandle:
    """Start ``definition`` for business id ``saga_id`` in the caller's UoW.

    Inserts the instance row and publishes its first nudge through the outbox
    in the SAME transaction as the business change. Starting the same
    ``saga_id`` again is a create-or-get: equal input returns the existing
    instance (``created=False``), other input raises
    :class:`SagaInputConflict` (409). Outside a Unit of Work it raises
    ``UnitOfWorkRequiredError``, like ``publish_event``.
    """
    return await runtime_of().start(definition, saga_id=saga_id, input=input)


async def saga_status(definition: Saga[Any], saga_id: object) -> SagaHandle | None:
    """The instance of ``definition`` for ``saga_id`` (``None`` if never started)."""
    return await runtime_of().status(definition, saga_id)


__all__ = [
    "ADVANCE_CONSUMER",
    "NUDGE_SCHEMA",
    "Outcome",
    "RegisteredSaga",
    "SagaHandle",
    "SagaInputConflict",
    "SagaNotInstalled",
    "SagaRegistry",
    "SagaRuntime",
    "SagaSweeper",
    "StepOutcome",
    "SystemChannel",
    "classify_exception",
    "install_sagas",
    "nudge_channel",
    "runtime_of",
    "saga_status",
    "start_saga",
]
