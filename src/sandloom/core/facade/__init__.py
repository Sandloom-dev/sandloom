"""The CoreFacade — a module's stable, narrow window onto the framework.

``create_app`` builds one :class:`CoreFacade` and passes it to each module's
``register(core)``. It exposes only the safe, stable operations a module may use
(routing, lifecycle hooks, health checks, event-schema registration, service and
capability resolution, projections) while hiding concrete infrastructure choices
behind capability contracts. Modules depend on this surface, never on core
internals.

This is part of the stable public API (``sandloom.core.facade``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from prometheus_client import Counter

import sandloom.core.eventing.event_bus as _event_bus
import sandloom.core.facade._claims as _claims
import sandloom.core.facade._registries as _registries
import sandloom.core.facade._runtime as _runtime
import sandloom.core.facade._services as _services
import sandloom.core.facade._state as _state
import sandloom.core.observability.metrics as _metrics
import sandloom.core.observability.tracing as _tracing
from sandloom.core.descriptors import HealthCheck
from sandloom.core.module_api import ModuleAPI, ModuleRegistrationDraft
from sandloom.core.module_api._registration import _RegistrationControlFlow
from sandloom.core.observability.tracing import init_tracing

# The public facade API (__all__) and the CoreFacade bases, defined in the
# submodules.
ShutdownTask = _state.ShutdownTask
StartupTask = _state.StartupTask
ClaimsContributorContract = _claims.ClaimsContributorContract
ClaimsUnavailable = _claims.ClaimsUnavailable
_ClaimsMixin = _claims._ClaimsMixin
CLAIMS_CONTRACT_SUFFIX = _services.CLAIMS_CONTRACT_SUFFIX
FACTS_CONTRACT_SUFFIX = _services.FACTS_CONTRACT_SUFFIX
claims_contract_name = _services.claims_contract_name
facts_contract_name = _services.facts_contract_name
_RegistriesMixin = _registries._RegistriesMixin
CoreRuntime = _runtime.CoreRuntime


__all__ = [
    "CLAIMS_CONTRACT_SUFFIX",
    "FACTS_CONTRACT_SUFFIX",
    "ClaimsContributorContract",
    "ClaimsUnavailable",
    "CoreFacade",
    "CoreRuntime",
    "StartupTask",
    "ShutdownTask",
    "claims_contract_name",
    "facts_contract_name",
]

_FACADE_COUNTER: Any = None
_FACADE_COUNTER_LOCK = threading.Lock()
_FACADE_METRIC_NAME_LIMIT = 1024
_FACADE_COUNTER_NAMES: set[str] = set()


def _facade_counter() -> Any:
    """The process-wide Prometheus counter behind ``increment_metric`` (lazy)."""
    global _FACADE_COUNTER
    if _FACADE_COUNTER is None:
        with _FACADE_COUNTER_LOCK:
            if _FACADE_COUNTER is None:
                _FACADE_COUNTER = Counter(
                    "sandloom_facade_counter",
                    "Monotonic counters incremented through CoreFacade",
                    ["name"],
                )
    return _FACADE_COUNTER


#: Ordered startup phases (see CoreFacade.add_startup_task).
STARTUP_PHASES = ("infra", "modules", "post")


class CoreFacade(_ClaimsMixin, _RegistriesMixin):
    """A module's strictly bounded access point to the framework's infrastructure.

    Provides only methods that are safe and stable for modules to depend on;
    concrete infrastructure details (cache backend, tracing implementation,
    transport) stay hidden behind this API. One instance is created by the
    composition root and handed to every module's ``register(core)``.
    """

    @property
    def event_bus(self) -> Any:
        """Return the event bus owned by this facade.

        The instance is bound via set_event_bus() during app wiring so each
        application owns (and shuts down) exactly its own bus. Falls back to the
        bare-facade in-memory bus for facades that never bind one (unit tests).
        """
        if self._event_bus is not None:
            return self._event_bus

        return _event_bus.get_event_bus()

    def set_event_bus(self, bus: Any) -> None:
        """Bind the event bus instance this application owns."""
        self._event_bus = bus

    def add_startup_task(
        self,
        task: StartupTask,
        *,
        critical: bool = False,
        phase: str | None = None,
    ) -> None:
        """Register a startup task.

        critical=True means the application MUST NOT start if the task fails:
        run_startup re-raises instead of degrading silently.

        ``phase`` is ``"infra"`` (runs first), ``"modules"`` (the default) or
        ``"post"`` (runs last); a task's name never selects its phase.
        """
        if phase is None:
            phase = "modules"
        if phase not in STARTUP_PHASES:
            raise ValueError(
                f"unknown startup phase {phase!r}; expected one of {STARTUP_PHASES}"
            )
        self._startup_tasks.append(task)
        self._startup_critical.append(critical)
        self._startup_phase.append(phase)

    def is_critical_startup_task(self, task: Any) -> bool:
        # True if this callable was registered critical in ANY of its
        # registrations (kept for the public API / tests; run_startup uses the
        # per-registration specs to avoid identity ambiguity).
        return any(
            t is task and crit
            for t, crit in zip(self._startup_tasks, self._startup_critical, strict=True)
        )

    @property
    def startup_task_specs(self) -> list[tuple[StartupTask, bool]]:
        """``(task, critical)`` pairs in registration order."""
        return list(zip(self._startup_tasks, self._startup_critical, strict=True))

    @property
    def startup_task_phased_specs(self) -> list[tuple[StartupTask, bool, str]]:
        """``(task, critical, phase)`` triples in registration order."""
        return list(
            zip(
                self._startup_tasks,
                self._startup_critical,
                self._startup_phase,
                strict=True,
            )
        )

    def add_shutdown_task(self, task: ShutdownTask) -> None:
        self._shutdown_tasks.append(task)

    def register_health_check(self, name: str, fn: HealthCheck) -> None:
        self._health_checks[name] = fn

    @property
    def health_checks(self) -> dict[str, HealthCheck]:
        return dict(self._health_checks)

    @property
    def startup_tasks(self) -> list[StartupTask]:
        return list(self._startup_tasks)

    @property
    def shutdown_tasks(self) -> list[ShutdownTask]:
        return list(self._shutdown_tasks)

    # -----------------
    # Module registration transaction (draft/commit)
    # -----------------
    @contextmanager
    def module_registration(self, module_name: str) -> Iterator[ModuleAPI]:
        """Scope a module registration so a failure leaves no partial state.

        Yields a draft-backed :class:`ModuleAPI`: everything the module
        registers (routers, services, lifecycle hooks, health checks, event
        schemas) is buffered in a :class:`ModuleRegistrationDraft` and applied
        atomically by ``commit`` only when ``register()`` returns normally. A
        failure propagates without ever committing, so NOTHING is applied —
        including event schemas, which the old snapshot/restore path left behind
        as orphans in the contract inventory.

        This replaces snapshotting private kernel state (``service_registry._map``,
        ``app.router.routes`` slicing): the kernel is never mutated until commit.
        """
        draft = ModuleRegistrationDraft()
        api = ModuleAPI(self, draft=draft)
        try:
            yield api
        except Exception as exc:
            if not isinstance(exc, _RegistrationControlFlow):
                self.logger.warning(
                    "Discarding partial registration of module %s", module_name
                )
            raise
        # Reached only on success: apply the whole draft atomically. The
        # registering module's name is visible while applying so contributions
        # (claims contributors) are attributed to their owner.
        previous = self._registering_module
        self._registering_module = module_name
        try:
            draft.commit(self)
        finally:
            self._registering_module = previous

    # -----------------
    # Infra accessors
    # -----------------
    def ensure_tracer(self) -> None:
        """Initialize tracing once (idempotent)."""
        if not self._tracer_initialized:
            init_tracing()
            self._tracer_initialized = True

    # -----------------
    # Tracing helper
    # -----------------
    def span(self, name: str) -> Any:  # pragma: no cover - thin wrapper
        """Return a span context manager (no-op if tracing disabled).

        Usage:
            with core.span("my.operation"):
                ...
        """
        try:
            pass
        except Exception:  # noqa: BLE001 - tracing is optional
            return nullcontext()
        self.ensure_tracer()
        return _tracing.start_span(name)

    # -----------------
    # Metrics (skeleton)
    # -----------------
    def register_metric(self, name: str, value: Any | None = None) -> None:
        with self._metrics_lock:
            if (
                name not in self._metrics
                and len(self._metrics) >= _FACADE_METRIC_NAME_LIMIT
            ):
                raise ValueError("facade metric name budget exhausted")
            self._metrics[name] = value
        # Export numeric values immediately to Prometheus gauge (Phase G)
        if isinstance(value, (int, float)):
            try:  # local import to avoid circular dependency at import time
                _metrics.set_facade_metric(name, float(value))
            except Exception:  # pragma: no cover  # noqa: BLE001 - metric export is best effort
                self.logger.debug("Failed to export facade metric '%s'", name)

    def increment_metric(self, name: str, amount: int | float = 1) -> None:
        """Atomically add ``amount`` to the counter ``name``.

        A real counter (monotonic, thread-safe): the in-process value
        (``get_metric``) is updated under a lock and the increment is exported
        to the Prometheus counter ``sandloom_facade_counter_total{name=...}``. Use it
        instead of a ``get_metric``/``register_metric`` read-modify-write.
        """
        if amount < 0:
            raise ValueError("counters only increase; amount must be >= 0")
        with self._metrics_lock:
            if (
                name not in self._metrics
                and len(self._metrics) >= _FACADE_METRIC_NAME_LIMIT
            ):
                raise ValueError("facade metric name budget exhausted")
            current = self._metrics.get(name)
            base = current if isinstance(current, (int, float)) else 0
            self._metrics[name] = base + amount
        try:
            counter = _facade_counter()
            with _FACADE_COUNTER_LOCK:
                if name not in _FACADE_COUNTER_NAMES:
                    if len(_FACADE_COUNTER_NAMES) >= _FACADE_METRIC_NAME_LIMIT:
                        return
                    _FACADE_COUNTER_NAMES.add(name)
                counter.labels(name=name).inc(amount)
        except Exception:  # pragma: no cover  # noqa: BLE001 - metric export is best effort
            self.logger.debug("Failed to export facade counter '%s'", name)

    def get_metric(self, name: str) -> Any:
        return self._metrics.get(name)

    def list_metrics(self) -> dict[str, Any]:
        return dict(self._metrics)

    # -----------------
    # Logging helpers
    # -----------------
    def logger_factory(self, name: str) -> logging.Logger:
        """Return namespaced module logger (sandloom.module.<name>)."""
        return logging.getLogger(f"sandloom.module.{name}")

    # -----------------
    # Contracts / Schemas (Phase J)
    # -----------------
    # -----------------
    # Transactional module registration (rollback support)
    # -----------------
