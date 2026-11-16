"""The API a driver package implements. Most code should use ``sandloom.sdk.infra``.

A SQL dialect renderer implements :class:`SqlDialect` and names itself in the
``sandloom.sql_dialects`` entry-point group; a database with row security
answers ``row_security()`` with a :class:`RowSecurity` (the tenant it binds
is the setting :data:`TENANT_SETTING`); a database whose writers are serial
marks a transaction holding the write lock with :data:`WRITE_LOCK_HELD`. A
DBAPI that needs the driver's fast paths subclasses :class:`DbapiAdapter` and
registers it with
:func:`register_dbapi_adapter` (any other DBAPI takes the plain path). A
business module may not import this package (SANDLOOM-C004), and the kernel
never does. A module-facing contract joins it
only with a kit in :mod:`sandloom.driverkit.testing`; the kernel-facing ones
(``atomic_store``, ``lease``, ``broadcast``, the transport shape of ``eventbus``)
are final and exported before their kits. A driver states its
``contract_version``, the integer major of its capability; another major is
refused at load (SANDLOOM-B022). An operation raises only the errors of
:mod:`sandloom.core.infra.errors` (:class:`InfraError` and its subclasses,
re-exported here), mapping its backend's errors onto them. The kernel-facing
contracts a driver package serves (``atomic_store``, ``lease``, ``broadcast``,
and ``eventbus`` as an :class:`EventTransportProvider`: the typed durable
transport the kernel builds its bus over, with :class:`Delivery`,
:class:`GroupOwner`, :class:`Retention` and the other value types of its
operations, :class:`PartialPublish` and :class:`GroupOwnedElsewhere`)
are exported with their value types; a module never resolves them. A driver
records metrics through :data:`observe` (an :class:`Observe`: ``counter`` and
``histogram`` return a :class:`DriverCounter` / :class:`DriverHistogram`), which
registers them in the kernel's registry: a driver imports no kernel setting
and no ``prometheus_client`` registry (:mod:`sandloom.driverkit.metrics`).
Guide: ``docs/infrastructure/write-a-driver.md``.

The names below load on first use (PEP 562), not when the package is imported:
a submodule such as :mod:`sandloom.driverkit.testing_family` is imported by the
Redis harness family before the settings snapshot, and importing the package
must not pull in the settings, SQLAlchemy and the infra and eventing layers
(about 250 ms) for it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sandloom.core.descriptors import HealthResult
    from sandloom.core.eventing.envelope import EventEnvelope
    from sandloom.core.infra.capabilities import CapabilityRef, CapabilitySpec
    from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
    from sandloom.core.infra.errors import (
        AlreadyExists,
        InfraError,
        InvalidArgument,
        NotFound,
        PermissionDenied,
        Timeout,
        TooLarge,
        Unavailable,
        Unsupported,
    )
    from sandloom.core.infra.fenced_lease import FencedLease
    from sandloom.core.infra.ports._base import InfraProvider, health_failure_details
    from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
    from sandloom.core.infra.ports.broadcast import (
        BroadcastMessage,
        BroadcastProvider,
        Subscription,
    )
    from sandloom.core.infra.ports.cache import CacheProvider
    from sandloom.core.infra.ports.database import UnitSession
    from sandloom.core.infra.ports.eventbus import (
        ConsumerInfo,
        DeadLetter,
        Delivery,
        EventTransport,
        EventTransportProvider,
        GroupInfo,
        GroupOwnedElsewhere,
        GroupOwner,
        PartialPublish,
        Retention,
        StreamInfo,
    )
    from sandloom.core.infra.ports.lease import (
        Lease,
        LeaseHolder,
        LeaseProvider,
        LeaseStore,
    )
    from sandloom.core.infra.ports.notification import Notification, NotifierProvider
    from sandloom.core.infra.ports.objectstore import (
        ObjectContent,
        ObjectStoreProvider,
        iter_object_chunks,
        validate_object_key,
    )
    from sandloom.core.infra.ports.taskqueue import (
        PeriodicSpec,
        TaskQueueProvider,
        TaskSpec,
        TaskTable,
        WorkerBoot,
        WorkerHost,
    )
    from sandloom.core.sql_alchemy.dbapi import (
        DBAPI_CAPABILITIES,
        DbapiAdapter,
        register_dbapi_adapter,
    )
    from sandloom.core.sql_alchemy.portable import (
        TENANT_SETTING,
        WRITE_LOCK_HELD,
        ConflictInsert,
        LockStatement,
        RowSecurity,
        SqlDialect,
    )
    from sandloom.driverkit.metrics import (
        DriverCounter,
        DriverHistogram,
        Observe,
        observe,
    )

#: Where each exported name lives: ``module -> names``. Every name of
#: ``__all__`` is in exactly one entry (``test_driverkit_lazy_exports``), and
#: every entry is imported under ``TYPE_CHECKING`` above for the type checkers.
_HOMES: dict[str, tuple[str, ...]] = {
    "sandloom.core.descriptors": ("HealthResult",),
    "sandloom.core.eventing.envelope": ("EventEnvelope",),
    "sandloom.core.infra.capabilities": ("CapabilityRef", "CapabilitySpec"),
    "sandloom.core.infra.config": ("DEFAULT_INSTANCE", "InstanceConfig"),
    "sandloom.core.infra.errors": (
        "AlreadyExists",
        "InfraError",
        "InvalidArgument",
        "NotFound",
        "PermissionDenied",
        "Timeout",
        "TooLarge",
        "Unavailable",
        "Unsupported",
    ),
    "sandloom.core.infra.fenced_lease": ("FencedLease",),
    "sandloom.core.infra.ports._base": ("InfraProvider", "health_failure_details"),
    "sandloom.core.infra.ports.atomic_store": ("AtomicStoreProvider",),
    "sandloom.core.infra.ports.broadcast": (
        "BroadcastMessage",
        "BroadcastProvider",
        "Subscription",
    ),
    "sandloom.core.infra.ports.cache": ("CacheProvider",),
    "sandloom.core.infra.ports.database": ("UnitSession",),
    "sandloom.core.infra.ports.eventbus": (
        "ConsumerInfo",
        "DeadLetter",
        "Delivery",
        "EventTransport",
        "EventTransportProvider",
        "GroupInfo",
        "GroupOwnedElsewhere",
        "GroupOwner",
        "PartialPublish",
        "Retention",
        "StreamInfo",
    ),
    "sandloom.core.infra.ports.lease": (
        "Lease",
        "LeaseHolder",
        "LeaseProvider",
        "LeaseStore",
    ),
    "sandloom.core.infra.ports.notification": ("Notification", "NotifierProvider"),
    "sandloom.core.infra.ports.objectstore": (
        "ObjectContent",
        "ObjectStoreProvider",
        "iter_object_chunks",
        "validate_object_key",
    ),
    "sandloom.core.infra.ports.taskqueue": (
        "PeriodicSpec",
        "TaskQueueProvider",
        "TaskSpec",
        "TaskTable",
        "WorkerBoot",
        "WorkerHost",
    ),
    "sandloom.core.sql_alchemy.dbapi": (
        "DBAPI_CAPABILITIES",
        "DbapiAdapter",
        "register_dbapi_adapter",
    ),
    "sandloom.core.sql_alchemy.portable": (
        "TENANT_SETTING",
        "WRITE_LOCK_HELD",
        "ConflictInsert",
        "LockStatement",
        "RowSecurity",
        "SqlDialect",
    ),
    "sandloom.driverkit.metrics": (
        "DriverCounter",
        "DriverHistogram",
        "Observe",
        "observe",
    ),
}
_HOME: dict[str, str] = {
    name: module for module, names in _HOMES.items() for name in names
}


def __getattr__(name: str) -> object:
    module = _HOME.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # resolved once: later lookups skip this hook
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_HOME})


__all__ = [
    "AlreadyExists",
    "InfraError",
    "InvalidArgument",
    "NotFound",
    "PermissionDenied",
    "Timeout",
    "TooLarge",
    "Unavailable",
    "Unsupported",
    "AtomicStoreProvider",
    "BroadcastMessage",
    "BroadcastProvider",
    "CacheProvider",
    "ConsumerInfo",
    "DeadLetter",
    "Delivery",
    "EventEnvelope",
    "EventTransport",
    "EventTransportProvider",
    "GroupInfo",
    "GroupOwnedElsewhere",
    "GroupOwner",
    "PartialPublish",
    "Retention",
    "StreamInfo",
    "FencedLease",
    "Lease",
    "LeaseHolder",
    "LeaseProvider",
    "LeaseStore",
    "Subscription",
    "CapabilityRef",
    "ConflictInsert",
    "DBAPI_CAPABILITIES",
    "DbapiAdapter",
    "LockStatement",
    "RowSecurity",
    "SqlDialect",
    "TENANT_SETTING",
    "WRITE_LOCK_HELD",
    "CapabilitySpec",
    "DEFAULT_INSTANCE",
    "HealthResult",
    "InfraProvider",
    "InstanceConfig",
    "Notification",
    "NotifierProvider",
    "ObjectContent",
    "ObjectStoreProvider",
    "PeriodicSpec",
    "TaskQueueProvider",
    "TaskSpec",
    "TaskTable",
    "UnitSession",
    "WorkerBoot",
    "WorkerHost",
    "DriverCounter",
    "DriverHistogram",
    "Observe",
    "health_failure_details",
    "iter_object_chunks",
    "observe",
    "validate_object_key",
    "register_dbapi_adapter",
]
