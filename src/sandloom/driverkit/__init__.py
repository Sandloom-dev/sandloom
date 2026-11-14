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
(``atomic_store``, ``lease``, ``broadcast``) are final and exported before
their kits. A driver states its
``contract_version``, the integer major of its capability; another major is
refused at load (SANDLOOM-B022). An operation raises only the errors of
:mod:`sandloom.core.infra.errors` (:class:`InfraError` and its subclasses,
re-exported here), mapping its backend's errors onto them. The kernel-facing
contracts a driver package serves (``atomic_store``, ``lease``, ``broadcast``)
are exported with their value types; a module never resolves them. A driver
records metrics through :data:`observe` (an :class:`Observe`: ``counter`` and
``histogram`` return a :class:`DriverCounter` / :class:`DriverHistogram`), which
registers them in the kernel's registry: a driver imports no kernel setting
and no ``prometheus_client`` registry (:mod:`sandloom.driverkit.metrics`).
Guide: ``docs/infrastructure/write-a-driver.md``.
"""

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilityRef, CapabilitySpec
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
from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
from sandloom.core.infra.ports.broadcast import (
    BroadcastMessage,
    BroadcastProvider,
    Subscription,
)
from sandloom.core.infra.ports.lease import Lease, LeaseHolder, LeaseProvider
from sandloom.core.infra.ports.notification import Notification, NotifierProvider
from sandloom.core.infra.ports.objectstore import (
    ObjectContent,
    ObjectStoreProvider,
    iter_object_chunks,
    validate_object_key,
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
    "Lease",
    "LeaseHolder",
    "LeaseProvider",
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
    "HealthResult",
    "InfraProvider",
    "Notification",
    "NotifierProvider",
    "ObjectContent",
    "ObjectStoreProvider",
    "DriverCounter",
    "DriverHistogram",
    "Observe",
    "iter_object_chunks",
    "observe",
    "validate_object_key",
    "register_dbapi_adapter",
]
