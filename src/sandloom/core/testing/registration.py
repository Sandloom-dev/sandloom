"""Pin what a module's ``register(core)`` registers — its registration surface.

:func:`registration_surface` runs ``register`` against a recording stand-in
for ``ModuleAPI`` and records every registration call (routers with their
routes, services, event schemas, claims contributors, access facts, startup /
shutdown tasks, consumers, projections, health checks) as comparable tuples.
A module test pins that list, so refactoring ``register`` (e.g. to
:func:`~sandloom.core.module_api.define_module`) provably changes nothing::

    surface = registration_surface(sandloom_billing.register)
    assert surface.descriptor.consumes == [...]
    assert sorted(surface.calls) == sorted(EXPECTED)
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter

from sandloom.core.config.settings import settings
from sandloom.core.module_api import ModuleAPI

#: ModuleAPI methods that only READ (never part of the surface).
_READS = frozenset({"get_service", "get_schema_registry", "logger_factory"})

Call = tuple[str, ...]

_LOGGER = logging.getLogger("sandloom.core.testing.registration")


@dataclass
class RegistrationSurface:
    """What one ``register(core)`` call registered, in call order."""

    descriptor: Any
    calls: list[Call] = field(default_factory=list)

    def of(self, method: str) -> list[Call]:
        """The recorded calls of one ``ModuleAPI`` method, in order."""
        return [call for call in self.calls if call[0] == method]


def _describe(value: Any) -> str:
    if isinstance(value, APIRouter):
        routes = ", ".join(
            f"{','.join(sorted(getattr(r, 'methods', None) or ()))} "
            f"{getattr(r, 'path', '')}"
            for r in value.routes
        )
        return f"router[{routes}]"
    if value is None or isinstance(value, bool | int | float | str):
        return repr(value)
    if isinstance(value, dict):
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        return f"mapping:{digest}"
    fact_name = getattr(value, "name", None)
    if isinstance(fact_name, str) and not callable(value):
        return f"{type(value).__name__}({fact_name!r})"
    qualname = getattr(value, "__qualname__", None)
    if qualname is not None:
        # A closure is named by itself, not by the function that defined it
        # (``register.<locals>.f`` and ``_setup.<locals>.f`` are the same task).
        qualname = qualname.rsplit("<locals>.", 1)[-1]
        return f"{getattr(value, '__module__', '?')}.{qualname}"
    return type(value).__name__


class _Recorder:
    """Stands in for ``ModuleAPI``: records every registration call.

    With a real ``api`` the calls are also forwarded to it; without one the
    recorder is a pure stub (reads return ``None``), so nothing registers
    anywhere and ``register`` can run any number of times.
    """

    def __init__(self, api: Any | None, calls: list[Call]) -> None:
        self._api = api
        self._calls = calls

    @property
    def logger(self) -> logging.Logger:
        return self._api.logger if self._api is not None else _LOGGER

    @property
    def settings(self) -> Any:
        if self._api is not None:
            return self._api.settings

        return settings

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or not callable(getattr(ModuleAPI, name, None)):
            raise AttributeError(name)
        target = getattr(self._api, name) if self._api is not None else None
        if name in _READS:
            return target or (lambda *args, **kwargs: None)

        def record(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(
                (
                    name,
                    *(_describe(a) for a in args),
                    *(f"{k}={_describe(v)}" for k, v in sorted(kwargs.items())),
                )
            )
            return target(*args, **kwargs) if target is not None else None

        return record


def registration_surface(
    register: Callable[[Any], Any], *, api: Any | None = None
) -> RegistrationSurface:
    """Run ``register`` against a recording ``ModuleAPI`` stand-in.

    By default nothing is registered anywhere (a pure stub: read methods such
    as ``get_service`` return ``None``). Pass ``api=ModuleAPI(core)`` to also
    forward every call to a real core. Side effects of ``register`` itself
    (e.g. installing a process-wide client) still happen — restore them in the
    test if they matter.
    """
    calls: list[Call] = []
    descriptor = register(_Recorder(api, calls))
    return RegistrationSurface(descriptor=descriptor, calls=calls)


__all__ = ["Call", "RegistrationSurface", "registration_surface"]
