"""Boot one module alone (I-19): static import rules miss boot-time coupling.

A module that never imports a sibling can still need one to START: a
``register(core)`` that resolves another module's contract eagerly, a startup
task that calls a contract without degrading, a route that only validates
alongside a sibling's router. The deploy model promises any module subset
boots (``SANDLOOM_MODULES``); :func:`boot_module_alone` proves it for one module:
``create_app`` with ONLY that module in ``SANDLOOM_MODULES`` (required, no
``SERVICE_BINDINGS``, no entry-point plugins), then the full lifespan
(startup + shutdown) against the test infrastructure.

A startup task that fails without being ``critical`` only logs; here every
ERROR logged under ``sandloom`` while the lifespan runs fails the boot
(:class:`ModuleBootError`) — a module that cannot start alone must refuse
explicitly, not degrade into a half-started process.

The only acceptable refusal is the documented explicit one:
:class:`~sandloom.core.loader.plugin_loader.ContractBindingError` for REQUIRED
consumed contracts (``ModuleDescriptor.consumes``) that nothing provides —
returned as :attr:`BootOutcome.missing_contracts` so a test pins it. Any other
exception propagates (that is the coupling bug).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from sandloom.core.app_factory import create_app
from sandloom.core.config.settings import settings
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import (
    ContractBindingError,
    load_module_topology,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry


class ModuleBootError(AssertionError):
    """The module started alone, but logged errors while doing so."""


class _Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = f"{record.name}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            text += f" ({record.exc_info[1]!r})"
        self.messages.append(text)


@dataclass(frozen=True)
class BootOutcome:
    """What booting one module alone did."""

    module: str
    started: bool
    #: Required consumed contracts nothing provides (``ContractBindingError``).
    missing_contracts: frozenset[str] = field(default_factory=frozenset)


@contextmanager
def _only_module(module: str) -> Iterator[None]:
    patched: dict[str, Any] = {
        "sandloom_modules": [module],
        "module_required": [module],
        "module_blacklist": [],
        "module_load_entrypoints": False,
        "service_bindings": {},
    }
    saved = {name: getattr(settings, name) for name in patched}
    try:
        for name, value in patched.items():
            setattr(settings, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


async def boot_module_alone(module: str, *, lifespan: bool = True) -> BootOutcome:
    """``create_app`` with only ``module`` loaded, then run its lifespan.

    Returns ``started=True`` when the app built and (with ``lifespan``) started
    and shut down cleanly; ``started=False`` with the missing contracts when
    the loader refused with ``ContractBindingError``. Anything else raises
    (an error logged during the lifespan: :class:`ModuleBootError`).
    Needs the test database / cache the harness configures (the ``db_engine``
    fixture builds the schema).
    """
    with _only_module(module):
        try:
            app = create_app()
        except ContractBindingError as exc:
            missing = frozenset(contract for _m, contract, _r in exc.missing)
            return BootOutcome(module=module, started=False, missing_contracts=missing)
        if lifespan:
            errors = _Errors()
            sandloom_logger = logging.getLogger("sandloom")
            sandloom_logger.addHandler(errors)
            try:
                async with app.router.lifespan_context(app):
                    pass
            finally:
                sandloom_logger.removeHandler(errors)
            if errors.messages:
                raise ModuleBootError(
                    f"{module} logged errors booting alone: "
                    + "; ".join(errors.messages)
                )
    return BootOutcome(module=module, started=True)


@dataclass(frozen=True)
class ModuleTopology:
    """What one module, loaded alone, provides and requires."""

    module: str
    provides: frozenset[str]
    #: Required consumed contracts the module does not provide itself.
    requires: frozenset[str]


def load_topology(modules: Sequence[str], *, exact: bool = False) -> tuple[Any, Any]:
    """``(facade, load summary)`` of ``modules`` loaded into a fresh facade
    (registration only, no I/O; a consumed contract left unbound is recorded,
    never raised). ``exact``: load nothing else (no entry-point plugins, no
    filesystem scan), as a deployed process listing ``SANDLOOM_MODULES`` does."""
    patched = {"module_load_entrypoints": False}
    saved = {name: getattr(settings, name) for name in patched}
    try:
        if exact:
            for name, value in patched.items():
                setattr(settings, name, value)
        core = CoreFacade(app=FastAPI())
        core.register_schema_registry(InMemorySchemaRegistry())
        summary = load_module_topology(core, list(modules))
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
    return core, summary


def module_topology(module: str) -> ModuleTopology:
    """Load ``module`` alone (registration only, no I/O) and read its
    provided contracts and required consumed ones."""
    core, summary = load_topology([module])
    requires = {
        entry["contract"]
        for entries in summary.consumes.values()
        for entry in entries
        if entry["required"] and entry["resolution"] == "missing"
    }
    return ModuleTopology(
        module, frozenset(core.service_registry.list()), frozenset(requires)
    )


def with_required_providers(
    modules: Sequence[str], candidates: Sequence[str]
) -> list[str]:
    """``modules`` plus, transitively, the ``candidates`` that provide the
    contracts they REQUIRE — the smallest set that loads strictly.

    Tests that boot a module subset derive it with this instead of pinning a
    list, so a module that gains a required cross-module contract (bundled or
    an app's) needs no framework-test edit. A requirement no candidate
    provides is left unmet: the strict load reports it (SANDLOOM-B005).
    """
    topology = {m: module_topology(m) for m in dict.fromkeys([*modules, *candidates])}
    result = list(dict.fromkeys(modules))
    pending = [c for m in result for c in sorted(topology[m].requires)]
    while pending:
        contract = pending.pop(0)
        if any(contract in topology[m].provides for m in result):
            continue
        provider = next(
            (m for m in candidates if contract in topology[m].provides), None
        )
        if provider is None:
            continue
        result.append(provider)
        pending += sorted(topology[provider].requires)
    return result


def provided_contracts(modules: Sequence[str]) -> set[str]:
    """Every contract the ``modules`` provide (each loaded alone)."""
    return {c for m in modules for c in module_topology(m).provides}


__all__ = [
    "BootOutcome",
    "ModuleBootError",
    "ModuleTopology",
    "boot_module_alone",
    "load_topology",
    "module_topology",
    "provided_contracts",
    "with_required_providers",
]
