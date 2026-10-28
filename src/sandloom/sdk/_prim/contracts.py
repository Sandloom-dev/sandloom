"""L2 contract primitives of ``sandloom.sdk.contracts`` (spec §2.3, §3).

``service(name)`` is the L2 form of ``Contract(REF)``: the caller-side proxy
of a sync contract, resolved when it is called (local provider first, then
the ``SERVICE_BINDINGS`` RPC proxy). ``service(name, optional=True)``
returns ``None`` only when the provider is neither local nor bound in this
deployment (decided at startup); an outage of a deployed provider still
raises (``503``). ``contract_method`` is the L2 form of ``@contract`` /
``@command`` (L1 builds provider methods with it).

Resolution order: a fake installed by ``sandloom.sdk.testing.fake_contract``, then
the application's contract resolver. The resolver is bound to the kernel's
active-core accessor (``sandloom.core.di.get_service``: the core ``create_app``
bound, or a ``use_active_core`` scope), so ``Contract(REF)`` works in any
running application; the ``sandloom.sdk.testing`` plugin binds the test
application's ``core.get_service`` for each test and restores it after.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sandloom.core.descriptors.contract_refs import ContractRef
from sandloom.core.di import ServiceNotFound, get_service
from sandloom.core.module_api import ContractMethodSpec, contract_method, contract_spec

#: ``fake_contract`` overrides: contract name -> implementation.
_fakes: dict[str, list[object]] = {}
#: The application's resolver: contract name -> proxy (raises ServiceNotFound).
#: Bound to the kernel's active core at import, the composition root of every
#: entry point (``create_app`` activates its core).
_resolver: Callable[[str], Any] | None = get_service


def _bind_resolver(
    resolver: Callable[[str], Any] | None,
) -> Callable[[str], Any] | None:
    """Install the application's contract resolver; returns the previous one.

    Called by the composition roots (the ``sandloom.sdk.testing`` plugin binds the
    test application's ``core.get_service``). Not a module-author API.
    """
    global _resolver
    previous = _resolver
    _resolver = resolver
    return previous


@contextmanager
def _override(name: str, impl: object) -> Iterator[None]:
    """Serve contract ``name`` from ``impl`` for the block (``fake_contract``)."""
    stack = _fakes.setdefault(name, [])
    stack.append(impl)
    try:
        yield
    finally:
        stack.pop()
        if not stack:
            _fakes.pop(name, None)


def service(name: str, *, optional: bool = False) -> Any:
    """The proxy of contract ``name`` (spec §2.3, §5.3).

    ``optional=True`` returns ``None`` only when the provider is neither local
    nor bound in this deployment; a deployed provider that is down still
    raises when called.
    """
    fakes = _fakes.get(name)
    if fakes:
        return fakes[-1]
    resolver = _resolver
    if resolver is None:
        raise RuntimeError(
            f"sandloom.sdk: no contract resolver is bound, so contract {name!r} "
            "cannot be resolved. In tests, activate the sandloom.sdk.testing plugin "
            "(addopts = ['-p', 'sandloom.sdk.testing.plugin']) or use fake_contract(); "
            "in an application create_app binds the active core."
        )
    try:
        return resolver(name)
    except ServiceNotFound:
        if optional:
            return None
        raise


__all__ = [
    "ContractRef",
    "ContractMethodSpec",
    "contract_method",
    "contract_spec",
    "service",
]
