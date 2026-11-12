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
never does. A contract joins it
only with a kit in :mod:`sandloom.driverkit.testing`. A driver states its
``contract_version``; another major version is refused at load (SANDLOOM-B022).
Guide: ``docs/infrastructure/write-a-driver.md``.
"""

from sandloom.core.descriptors import HealthResult
from sandloom.core.infra.capabilities import CapabilityRef, CapabilitySpec
from sandloom.core.infra.contracts import (
    InfraProvider,
    InvalidObjectKeyError,
    Notification,
    NotifierProvider,
    ObjectContent,
    ObjectStoreProvider,
    ObjectTooLargeError,
    UploadTooLargeError,
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

__all__ = [
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
    "InvalidObjectKeyError",
    "ObjectContent",
    "ObjectStoreProvider",
    "ObjectTooLargeError",
    "UploadTooLargeError",
    "iter_object_chunks",
    "validate_object_key",
    "register_dbapi_adapter",
]
