"""Serve a contract from a test double on the application itself.

``Contract(REF)`` in module code resolves through the SDK's resolver, which
``fake_contract`` overrides. Kernel code resolves contracts through
``core.get_service`` instead: the authorization atoms (``SubscriptionActive``,
``Subscribed(of=Account.org(...))``, ``Credits``, ``OrgRole``), access facts
and the claims pipeline. :func:`override_service` covers that path for a
block by shadowing ``get_service`` on the facade instance, so the request path
of a real application is untouched (no lookup is added to ``get_service``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sandloom.core.runtime as _runtime


@contextmanager
def override_service(core: Any, name: str, impl: object) -> Iterator[None]:
    """``core.get_service(name)`` answers ``impl`` inside the block.

    Overrides nest; the previous resolver is restored afterwards.
    """
    facade = getattr(core, "_facade", core)  # a runtime view wraps the facade
    had_own = "get_service" in vars(facade)
    previous = facade.get_service

    def get_service(requested: str) -> Any:
        if requested != name:
            return previous(requested)
        return impl

    facade.get_service = get_service
    try:
        yield
    finally:
        if had_own:
            facade.get_service = previous
        else:
            del facade.get_service


@contextmanager
def override_active_service(name: str, impl: object) -> Iterator[None]:
    """:func:`override_service` on the active application, if there is one."""
    core = _runtime.get_active_core()
    if core is None:
        yield
        return
    with override_service(core, name, impl):
        yield


__all__ = ["override_active_service", "override_service"]
