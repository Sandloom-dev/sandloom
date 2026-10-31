"""The read-only :class:`CoreRuntime` view handed to module code."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sandloom.core.config.settings import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade


class CoreRuntime:
    """Read-only runtime view of the :class:`CoreFacade` for module code.

    Request-time module code (``Depends(get_core)``) gets THIS, not the full
    facade: it can resolve contracts and capabilities, read settings, log,
    trace, count, and collect token claims — but it cannot reach the event bus
    (publish through ``publish_event``), the infrastructure registry, the raw
    FastAPI app, the service registry, or any ``register_*`` mutator.
    Registration happens only through the
    :class:`~sandloom.core.module_api.ModuleAPI` handed to ``register(core)``.

    This is an API-discipline boundary for trusted application code, not an
    immutable object graph or a sandbox. Shared Settings must be treated as
    read-only by modules; the remote-fact cache is maintained by shared authz
    machinery. Do not mutate either from a business module.
    """

    __slots__ = ("_facade",)

    def __init__(self, facade: CoreFacade) -> None:
        self._facade = facade

    # -- contracts ------------------------------------------------------
    def get_service(self, name: str) -> Any:
        return self._facade.get_service(name)

    async def collect_claims(
        self,
        user_id: Any,
        issuer_session: Any = None,
        *,
        issuer: str | None = None,
        precomputed: Mapping[Any, dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        return await self._facade.collect_claims(
            user_id, issuer_session, issuer=issuer, precomputed=precomputed
        )

    def get_access_fact(self, name: str) -> Any:
        return self._facade.get_access_fact(name)

    def remote_fact_owners(self) -> list[str]:
        return self._facade.remote_fact_owners()

    @property
    def remote_access_fact_cache(self) -> dict[Any, Any]:
        return self._facade.remote_access_fact_cache

    # -- capabilities ---------------------------------------------------
    def capability(
        self, capability: str, name: str = "default", contract: type | None = None
    ) -> Any:
        return self._facade.capability(capability, name, contract)

    def require_capability(
        self,
        capability: str,
        name: str = "default",
        *,
        features: set[str] | None = None,
        contract: type | None = None,
        version: str | None = None,
    ) -> Any:
        return self._facade.require_capability(
            capability, name, features=features, contract=contract, version=version
        )

    def database(self, name: str = "default") -> Any:
        return self._facade.database(name)

    def cache(self, name: str = "default") -> Any:
        return self._facade.cache(name)

    def objectstore(self, name: str = "default") -> Any:
        return self._facade.objectstore(name)

    def taskqueue(self, name: str = "default") -> Any:
        return self._facade.taskqueue(name)

    def notifier(self, name: str = "default") -> Any:
        return self._facade.notifier(name)

    # -- config / observability -----------------------------------------
    @property
    def settings(self) -> Settings:
        return self._facade.settings

    @property
    def logger(self) -> logging.Logger:
        return self._facade.logger

    def logger_factory(self, name: str) -> logging.Logger:
        return self._facade.logger_factory(name)

    def span(self, name: str) -> Any:
        return self._facade.span(name)

    def increment_metric(self, name: str, amount: int | float = 1) -> None:
        self._facade.increment_metric(name, amount)

    def get_metric(self, name: str) -> Any:
        return self._facade.get_metric(name)

    def __repr__(self) -> str:
        return "<CoreRuntime (read-only view)>"
