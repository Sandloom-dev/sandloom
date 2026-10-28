"""Declarative module registration: :func:`define_module`."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from fastapi import APIRouter

import sandloom.core.descriptors as _descriptors
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.module_api._registration import ModuleAPI

# --------------------------------------------------------------------------- #
# Declarative module registration
# --------------------------------------------------------------------------- #

#: ``router=`` / ``events=`` sentinel: discover by convention.
AUTO: Literal["auto"] = "auto"


def _optional_submodule(package: str, name: str) -> Any | None:
    """Import ``<package>.<name>`` if it exists (import errors propagate)."""
    dotted = f"{package}.{name}"
    try:
        spec = importlib.util.find_spec(dotted)
    except ModuleNotFoundError:  # the package itself is not importable
        spec = None
    if spec is None:
        return None
    return importlib.import_module(dotted)


def _discover_routers(package: str) -> list[Any]:
    """The router(s) ``<package>.api`` exports, by convention.

    ``router`` attribute wins; otherwise every ``APIRouter`` named in
    ``__all__`` (or, without ``__all__``, every public module-level one).
    """

    api = _optional_submodule(package, "api")
    if api is None:
        return []
    explicit = getattr(api, "router", None)
    if isinstance(explicit, APIRouter):
        return [explicit]
    names = getattr(api, "__all__", None)
    if names is None:
        names = [n for n in vars(api) if not n.startswith("_")]
    routers: list[Any] = []
    for attr in names:
        value = getattr(api, attr, None)
        if isinstance(value, APIRouter) and all(value is not r for r in routers):
            routers.append(value)
    return routers


def _discover_event_schemas(package: str) -> Mapping[str, dict[str, Any] | None]:
    events = _optional_submodule(package, "events")
    if events is None:
        return {}
    schemas = getattr(events, "EVENT_SCHEMAS", None)
    if schemas is None:
        return {}
    if not isinstance(schemas, Mapping):
        raise TypeError(
            f"{package}.events.EVENT_SCHEMAS must map channel -> JSON schema, "
            f"got {type(schemas).__name__}"
        )
    return cast(Mapping[str, "dict[str, Any] | None"], schemas)


def _caller_package(depth: int) -> str:
    frame = sys._getframe(depth + 1)  # documented fallback
    name = frame.f_globals.get("__name__")
    if not isinstance(name, str) or name == "__main__":
        raise TypeError(
            "define_module() could not infer the module package; call it as "
            "define_module(__name__, ...) from the module's __init__.py"
        )
    return name


def define_module(
    package: str | None = None,
    /,
    *,
    version: str = "0.1.0",
    optional: bool = False,
    consumes: Sequence[Any] = (),
    capabilities: Sequence[Any] = (),
    depends_on: Sequence[str] = (),
    migrations_database: str | None = None,
    migrations_chain: str | None = None,
    health_check: Any | None = None,
    setup: Callable[[ModuleAPI], None] | None = None,
    name: str | None = None,
    router: Any = AUTO,
    events: Any = AUTO,
    issues_tokens: bool = False,
) -> Callable[[ModuleAPI], Any]:
    """Build a module's ``register(core)`` from declarations + conventions.

    In a module's ``__init__.py``::
        from sandloom.core.module_api import define_module

        register = define_module(__name__, version="0.1.0")

    The returned ``register(core)``:

    1. includes the router(s) exported by ``<package>.api`` (its ``router``
       attribute, else every ``APIRouter`` in its ``__all__``) — pass
       ``router=`` an ``APIRouter`` / a list of them / ``None`` to override;
    2. registers ``<package>.events.EVENT_SCHEMAS`` (channel -> JSON schema)
       if present — ``events=`` a mapping or ``None`` overrides. Re-registering
       an identical schema is a no-op; a breaking change still fails the load;
    3. calls ``setup(core)`` for anything custom (service contracts, event
       consumers, projections, startup tasks, health checks);
    4. returns the :class:`~sandloom.core.descriptors.ModuleDescriptor`
       (``name`` defaults to ``package``).

    ``package`` defaults to the caller's ``__name__`` (frame inspection);
    passing ``__name__`` explicitly is preferred. ``consumes`` /
    ``capabilities`` accept requirement objects or plain strings
    (``"tenancy.membership.v1"`` / ``"cache"``). ``issues_tokens=True``
    marks the module as the access-token issuer (see
    :attr:`~sandloom.core.descriptors.ModuleDescriptor.issues_tokens`). Submodules
    are imported lazily, inside ``register``, exactly like a hand-written one.
    Modules are REQUIRED by default (a failed load aborts startup, SANDLOOM-B001);
    ``optional=True`` declares one the process may run without.
    An explicit ``def register(core)`` keeps working unchanged.
    """
    pkg = package or _caller_package(1)
    descriptor_name = name or pkg
    short = _module_name(descriptor_name)

    def register(core: ModuleAPI) -> Any:
        if router == AUTO:
            routers = _discover_routers(pkg)
        elif router is None:
            routers = []
        elif isinstance(router, list | tuple):
            routers = list(router)
        else:
            routers = [router]
        for item in routers:
            core.include_router(item)

        schemas = (
            _discover_event_schemas(pkg)
            if events == AUTO
            else (events if events is not None else {})
        )
        for channel, schema in schemas.items():
            core.register_event_schema(channel=channel, name=channel, schema=schema)

        if setup is not None:
            setup(core)

        core.logger.info("%s module registered", short)
        return _descriptors.ModuleDescriptor(
            name=descriptor_name,
            version=version,
            optional=optional,
            health_check=health_check,
            depends_on=list(depends_on),
            migrations_database=migrations_database,
            migrations_chain=migrations_chain,
            consumes=[
                _descriptors.ContractRequirement(c) if isinstance(c, str) else c
                for c in consumes
            ],
            capabilities=[
                _descriptors.CapabilityRequirement(c) if isinstance(c, str) else c
                for c in capabilities
            ],
            issues_tokens=issues_tokens,
        )

    register.__qualname__ = register.__name__ = "register"
    register.__module__ = pkg
    register.__doc__ = f"Register the {short} module (declared via define_module)."
    return register
