"""The volatile-bus shape of the ``eventbus`` capability (``memory``, ``broadcast``).

One capability, two driver shapes (spec 7). The transport shape (``streams``
and every external driver) is an ``EventTransportProvider`` the kernel builds
its durable bus over. These two are **kernel-internal**: a
:class:`VolatileBusProvider` implements no ``EventTransport``; the kernel's own
volatile bus is built from it (``memory_bus.py`` in process, ``pubsub_bus.py``
over a ``broadcast`` instance). It is not exported from ``sandloom.driverkit``
and a driver package cannot implement it.

A volatile bus declares no ``durable``, so a module that needs delivery to
survive requires it and is refused at load on one, and production refuses the
bus without the accepted risk ``volatile_event_bus`` (G-EVENT-14, G-RISK-04).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.descriptors import HealthResult
from sandloom.core.eventing.bus_base import BaseEventBus
from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.eventing.pubsub_bus import BroadcastEventBus
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.registry import DriverFactory


class VolatileBusOptions(BaseModel):
    """Config-shape schema of the ``memory`` and ``broadcast`` volatile buses.

    ``memory`` ignores every option. The ``broadcast`` bus has no client of its
    own: it runs over the ``broadcast`` instance named by ``broadcast`` (default
    ``default``), on that instance's service, TLS, credentials and channel
    namespace. A ``url`` on a ``broadcast`` bus is refused at startup;
    ``extra="forbid"`` rejects a typo'd key.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str | None = None
    #: The ``broadcast`` instance of the ``broadcast`` driver.
    broadcast: str = "default"
    socket_connect_timeout_seconds: float = Field(default=0.5, gt=0)
    health_check_interval_seconds: int = Field(default=30, ge=0)


class VolatileBusProvider(InfraProvider):
    """An ``eventbus`` driver of the volatile-bus shape (contract major 2).

    Wraps the kernel's volatile bus so it joins the registry's lifecycle:
    ``aclose`` closes the bus. The bus needs the application's schema
    registry, so the registry builds these through
    :func:`make_volatile_bus_drivers` rather than ``from_config``.
    """

    capability: ClassVar[str] = "eventbus"
    contract_version = 2
    options_schema = VolatileBusOptions

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        bus_instance: BaseEventBus,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._bus = bus_instance

    def bus(self) -> BaseEventBus:
        """The kernel's volatile bus this provider owns."""
        return self._bus

    async def health(self) -> HealthResult:
        # A failing probe raises: the registry records its type name only.
        return await self._probe() or HealthResult(status="ok")

    async def _probe(self) -> HealthResult | None:
        """One probe of the backend; ``None`` is healthy. A failure raises."""
        return None

    async def aclose(self) -> None:
        await self._bus.close()


class MemoryEventBusProvider(VolatileBusProvider):
    """``eventbus`` / ``memory``: the in-process volatile bus."""

    supported_features = frozenset({"wildcard_subscribe"})


class BroadcastEventBusProvider(VolatileBusProvider):
    """``eventbus`` / ``broadcast``: the volatile bus over a ``broadcast`` instance."""

    supported_features = frozenset({"wildcard_subscribe"})
    # No url and no import: the bus runs over a ``broadcast`` instance (option
    # ``broadcast``), whose driver brings its own client.

    _bus: BroadcastEventBus

    async def aopen(self) -> None:
        """Refuse a ``broadcast`` instance without ``pattern_subscribe``."""
        self._bus.check_broadcast()

    async def _probe(self) -> HealthResult | None:
        """The health of the ``broadcast`` instance the bus runs on."""
        probe = getattr(self._bus.broadcast_instance(), "health", None)
        if probe is None:
            return None
        result: HealthResult = await probe()
        return result


def make_volatile_bus_drivers(
    schema_registry: SchemaRegistryContract,
    *,
    volatile_allowed: Callable[[], bool],
) -> dict[str, DriverFactory]:
    """Return ``{driver_name: factory}`` for the ``memory`` and ``broadcast`` buses.

    ``volatile_allowed`` is the kernel's policy, asked each time a volatile
    bus is built: ``False`` refuses it (production without the accepted risk
    ``volatile_event_bus``). It has no default, so no caller builds the
    drivers without stating it.
    """

    def _refuse_volatile_in_prod(cfg: InstanceConfig) -> None:
        if not volatile_allowed():
            raise RuntimeError(
                f"eventbus instance {cfg.name!r}: driver {cfg.driver!r} is "
                "volatile (no persistence; a Pub/Sub PUBLISH succeeds with zero "
                "subscribers, so the outbox would mark undelivered events as "
                "delivered). fix: use driver 'streams' (EVENT_BUS_BACKEND=streams) "
                "or accept lossy delivery explicitly with "
                'SANDLOOM_ACCEPTED_RISKS=["volatile_event_bus"]'
            )

    def _memory(cfg: InstanceConfig) -> InfraProvider:
        _refuse_volatile_in_prod(cfg)
        return MemoryEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=InMemoryEventBus(schema_registry),
        )

    def _broadcast(cfg: InstanceConfig) -> InfraProvider:
        _refuse_volatile_in_prod(cfg)
        options = VolatileBusOptions.model_validate(cfg.options)
        if options.url:
            raise RuntimeError(
                f"eventbus instance {cfg.name!r}: driver 'broadcast' takes its "
                "service from a broadcast instance, not from a 'url' (the bus "
                "would otherwise run outside that service's TLS, credentials "
                "and network). fix: declare INFRA broadcast.<name> with this "
                'url and set "broadcast": "<name>" on this instance'
            )
        return BroadcastEventBusProvider(
            name=cfg.name,
            driver=cfg.driver,
            options=dict(cfg.options),
            bus_instance=BroadcastEventBus(schema_registry, broadcast=options.broadcast),
        )

    return {"memory": _memory, "broadcast": _broadcast}


__all__ = [
    "MemoryEventBusProvider",
    "BroadcastEventBusProvider",
    "VolatileBusOptions",
    "VolatileBusProvider",
    "make_volatile_bus_drivers",
]
