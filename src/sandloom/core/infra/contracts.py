"""Abstract infrastructure provider contracts (technology-agnostic).

The kernel and business modules interact with infrastructure ONLY through
these abstractions. A concrete backend (Postgres, Redis, S3, Celery, …) is a
*driver* that subclasses one of these capability base classes and is selected
purely by configuration — modules never import a concrete driver.

Capabilities are intentionally small and orthogonal:

* ``database``    — transactional persistence (exposes a UnitOfWork factory)
* ``cache``       — ephemeral key/value store
* ``objectstore`` — blob/file storage
* ``taskqueue``   — background job broker
* ``eventbus``    — pub/sub domain-event transport

Storage is split *by paradigm*, not by product. ``database`` is the
**relational paradigm** contract — the rich, transactional system-of-record
that backs the Unit of Work, the transactional outbox and the audit log; its
guarantees (atomic UoW, ``transactional_outbox``, ``audit``) are advertised as
capability *features* a driver must support. Other storage paradigms have their
OWN contracts with their own (often weaker) guarantees: ``document_store`` and
``graph_store`` (see :mod:`sandloom.core.infra.extra_capabilities`). A module picks
the paradigm whose contract fits, rather than forcing one universal "database"
abstraction to span them all — that is why the relational contract is allowed
to be SQL-shaped while a document/graph contract is not.

Every capability supports *named instances*: multiple databases (or caches,
or queues) can coexist in one process, each addressed by a logical name the
module knows nothing concrete about (``"default"``, ``"analytics"``, …).
"""

from __future__ import annotations

from sandloom.core.infra.ports._base import InfraProvider
from sandloom.core.infra.ports.cache import Cache, CacheProvider
from sandloom.core.infra.ports.database import (
    AuditStore,
    BrokerUnavailableError,
    CommandClaim,
    CommandLogBusy,
    CommandLogStore,
    DatabaseProvider,
    DeliveryStatus,
    OutboxHandle,
    OutboxStore,
    PublishMany,
    RelayBatchResult,
    RequestLogBusy,
    RequestLogStore,
    UnitSession,
)
from sandloom.core.infra.ports.eventbus import EventTransportProvider
from sandloom.core.infra.ports.notification import (
    Notification,
    NotificationChannel,
    Notifier,
    NotifierProvider,
)
from sandloom.core.infra.ports.objectstore import (
    ObjectContent,
    ObjectReader,
    ObjectStore,
    ObjectStoreProvider,
    iter_object_chunks,
    validate_object_key,
)
from sandloom.core.infra.ports.taskqueue import (
    PeriodicSpec,
    TaskQueue,
    TaskQueueProvider,
    TaskSpec,
    TaskTable,
)

#: Readability alias: the ``database`` capability IS the relational paradigm.
#: Use it where calling the contract out as "relational" aids clarity; the
#: canonical capability key stays ``"database"`` (there is no second name).
RelationalProvider = DatabaseProvider


__all__ = [
    "Cache",
    "InfraProvider",
    "Notifier",
    "ObjectStore",
    "TaskQueue",
    "DatabaseProvider",
    "UnitSession",
    "RelationalProvider",
    "CacheProvider",
    "ObjectContent",
    "ObjectReader",
    "ObjectStoreProvider",
    "iter_object_chunks",
    "validate_object_key",
    "TaskQueueProvider",
    "TaskSpec",
    "TaskTable",
    "PeriodicSpec",
    "EventTransportProvider",
    "NotifierProvider",
    "Notification",
    "NotificationChannel",
    "BrokerUnavailableError",
    "DeliveryStatus",
    "OutboxHandle",
    "OutboxStore",
    "PublishMany",
    "RelayBatchResult",
    "AuditStore",
    "CommandClaim",
    "CommandLogBusy",
    "CommandLogStore",
    "RequestLogBusy",
    "RequestLogStore",
]
