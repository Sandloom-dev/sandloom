"""Contract-session bootstrap: the provider's UoW factory (dedicated contract
pool), the isolated UoW scope and the active-core lookup."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import sandloom.core.runtime as _runtime
import sandloom.core.unit_of_work.scope as uow_scope


@contextmanager
def _isolated_uow_scope(session: Any, name: str) -> Iterator[None]:
    """Bind ``session`` as the ONLY active UoW (the caller's is not visible).

    A contract never writes into its caller's transaction: publish_event /
    record_audit inside the provider resolve to the provider's own session.
    """
    session.info[uow_scope._DB_NAME_KEY] = name
    cur = uow_scope._current_uow_session.set(session)
    stack = uow_scope._active_uow_sessions.set((session,))
    try:
        yield
    finally:
        uow_scope._active_uow_sessions.reset(stack)
        uow_scope._current_uow_session.reset(cur)


def _contract_uow_factory(core: Any, database: str) -> Callable[[], Any]:
    """The provider's UoW factory for a contract call (dedicated contract pool).

    ``CoreFacade.get_contract_uow_factory`` serves contract UoWs from a pool
    separate from the request pool, so a request holding its connection never
    starves the nested contract call it makes (monolith pool deadlock).
    """
    getter = getattr(core, "get_contract_uow_factory", None)
    if callable(getter):
        return cast(Callable[[], Any], getter(database))
    return cast(Callable[[], Any], core.get_uow_factory(database))


def _require_core(core: Any, qualname: str) -> Any:
    if core is None:
        core = _runtime.get_active_core()
    if core is None:
        raise RuntimeError(
            f"contract {qualname!r} has no CoreFacade to open its Unit of Work; "
            "resolve it through core.get_service(...)"
        )
    return core
