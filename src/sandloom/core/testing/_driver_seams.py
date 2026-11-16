"""The test harness's reach into the built-in SQL driver (framework-internal).

The harness builds a caller-owned engine, resets a schema and returns the
default pools without an app (a fixture runs before one boots), so it needs
three things only the driver holds. They are named here, in the one harness
module that imports ``sandloom.core.drivers``, instead of in the request
dependency ``sandloom.core._internal.db_provider``, which stays driver-free.
"""

from sandloom.core.drivers.schema_reset import schema_reset_for
from sandloom.core.drivers.sqlalchemy_database import (
    build_disposable_engine,
    release_default_pools,
)

__all__ = [
    "build_disposable_engine",
    "release_default_pools",
    "schema_reset_for",
]
