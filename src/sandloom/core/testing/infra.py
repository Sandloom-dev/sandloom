"""Test seam over the infrastructure registry.

:func:`override_instance` points one named infrastructure instance (the
``default`` cache, the ``default`` database, ...) at a test backend for the
duration of a block, restoring the previous configuration AND the previously
built provider afterwards (``InfraRegistry.snapshot_instance`` /
``restore_instance``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def override_instance(
    core: Any,
    capability: str,
    name: str = "default",
    *,
    driver: str,
    **options: Any,
) -> AsyncIterator[Any]:
    """Configure ``capability``/``name`` with ``driver`` + ``options`` in the block.

    Yields the core's infrastructure registry. On exit the provider built for
    the override (if any) is closed and the previous instance configuration and
    provider are put back — so a lazily built, process-wide provider (e.g. the
    ``default`` engine) survives the test untouched::

        async with override_instance(core, "cache", driver="redis",
                                     url=os.environ["CACHE_URL"]):
            ...
    """
    from sandloom.core.infra.config import InstanceConfig  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    registry = core.infra
    snapshot = registry.snapshot_instance(capability, name)
    registry.configure_instance(
        InstanceConfig(capability=capability, name=name, driver=driver, options=options)
    )
    try:
        yield registry
    finally:
        displaced = registry.restore_instance(snapshot)
        if displaced is not None:
            await displaced.aclose()


__all__ = ["override_instance"]
