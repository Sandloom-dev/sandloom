"""Evaluation context for the authorization algebra.

An :class:`AuthzContext` carries everything an atom may need to decide:

- ``subject`` — the authenticated :class:`UserContext` (always present, from JWT
  claims; zero I/O).
- the request resource (tier 2) — loaded **lazily** so claim atoms that already
  decide the result never trigger a fetch.
- ``request`` / ``db`` — only used by live atoms (tier 3) that resolve a sync
  service (e.g. billing entitlements).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sandloom.shared.subject import UserContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession


_UNSET = object()


class AuthzContext:
    """Inputs available to a :class:`Requirement` during evaluation."""

    def __init__(
        self,
        subject: UserContext,
        *,
        request: Request | None = None,
        db: AsyncSession | None = None,
        resource: Any = _UNSET,
        loader: Callable[[AuthzContext], Awaitable[Any]] | None = None,
        source: str = "http",
        core: Any = None,
    ) -> None:
        self.subject = subject
        self.request = request
        self.db = db
        self._resource = resource
        self._loader = loader
        #: Where the evaluation originates: ``http`` | ``task`` | ``internal``
        #: | ``system``. Lets atoms / audit distinguish a request from a worker.
        self.source = source
        #: Optional explicit CoreFacade (non-HTTP paths). When None, atoms fall
        #: back to ``request.app.state.core`` or the process-active core.
        self._core = core
        #: Request-local cache of resolved access facts ((name, forced mode) ->
        #: bool), so a policy referencing the same fact twice resolves it once,
        #: while a forced mode is never served another mode's answer.
        self.fact_cache: dict[tuple[str, str | None], bool] = {}

    async def get_resource(self) -> Any:
        """Return the request resource, loading it once on first access.

        Loading is lazy so that a policy like ``Perm("x") & Owner(...)`` never
        fetches the resource when the cheap claim atom already fails.
        """
        if self._resource is _UNSET:
            if self._loader is None:
                raise RuntimeError(
                    "Resource-scoped atom evaluated without a resource or loader"
                )
            self._resource = await self._loader(self)
        return self._resource

    @property
    def has_resource(self) -> bool:
        return self._resource is not _UNSET or self._loader is not None

    def core(self) -> Any:
        """Resolve the bound :class:`CoreFacade`, or None.

        Order: an explicitly-passed core (non-HTTP paths pass ``core=`` to
        ``enforce``) → the app the request was served by. There is deliberately
        **no** process-global fallback — the subsystem never reaches a global
        singleton (that would break multi-app / test isolation). When no core is
        bound, atoms that need one (live / fact / ``OrgRole``) degrade
        explicitly (indeterminate 501 / ``FactUnavailable``).
        """
        if self._core is not None:
            return self._core
        app = getattr(self.request, "app", None)
        return getattr(getattr(app, "state", None), "core", None)
