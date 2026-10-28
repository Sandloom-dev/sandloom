"""No framework request-path dependency is a SYNC callable.

FastAPI runs a sync ``def`` dependency (and a sync generator) in the anyio
thread pool on EVERY request: a thread hop plus a worker-thread handoff for
what is usually an attribute read (``get_core`` cost ~100 profiled µs per
request on the S5 fast route). Framework dependencies do no blocking I/O, so
they are all ``async``. This test enumerates them two ways:

* every dependency (transitively) of every route of the composition-root app
  whose callable lives in the framework or a bundled module (``sandloom.*``);
* the products of every public dependency factory (``Capability``,
  ``Database``, ``TenantScope``, ``rate_limiter``, ``idempotency_guard``,
  ``require``, ``authorize_resource``, the auth guards, ``PlannedReads``...),
  so a factory no bundled route happens to use is covered too.
"""

from __future__ import annotations

from typing import Any

from fastapi import params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute

from sandloom.shared.authz import Account

FRAMEWORK_PREFIXES = ("sandloom.",)


def _name(call: Any) -> str:
    mod = getattr(call, "__module__", None) or type(call).__module__
    qual = getattr(call, "__qualname__", None) or type(call).__qualname__
    return f"{mod}.{qual}"


def _sync_framework_calls(dep: Dependant, where: str, out: list[str]) -> None:
    for sub in dep.dependencies:
        call = sub.call
        if (
            call is not None
            and _name(call).startswith(FRAMEWORK_PREFIXES)
            and not (sub.is_coroutine_callable or sub.is_async_gen_callable)
        ):
            out.append(f"{where}: {_name(call)}")
        _sync_framework_calls(sub, where, out)


def _factory_products() -> dict[str, Any]:
    from sandloom.core import di
    from sandloom.shared.authz import (
        Credits,
        Subscribed,
        SubscriptionActive,
        authorize_resource,
        require,
    )
    from sandloom.shared.guards import auth as guards

    async def _loader(_id: Any, _db: Any) -> Any:  # pragma: no cover
        return None

    products: dict[str, Any] = {
        "Capability": di.Capability("cache"),
        "Database": di.Database("analytics"),
        "TenantScope": di.TenantScope(),
        "rate_limiter": di.rate_limiter(requests=1, period=1),
        "idempotency_guard": di.idempotency_guard(scope="t"),
        "require(claims)": require(Subscribed(of=Account.user())),
        "require(live)": require(
            SubscriptionActive(of=Account.user()) & Credits(1, of=Account.user())
        ),
        "authorize_resource": authorize_resource(
            loader=_loader, by="id", policy=Subscribed(of=Account.user())
        ),
        "get_core": di.get_core,
        "get_db": di.get_db,
        "get_read_only_db": di.get_read_only_db,
        "get_planned_reads": di.get_planned_reads,
    }
    for name in getattr(guards, "__all__", ()):
        obj = getattr(guards, name)
        if callable(obj) and not isinstance(obj, type):
            products[f"guards.{name}"] = obj
    return products


def test_dependency_factories_are_async() -> None:
    offenders: list[str] = []
    for label, product in _factory_products().items():
        call = product.dependency if isinstance(product, params.Depends) else product
        assert call is not None, label
        root = Dependant(dependencies=[get_dependant(path="/", call=call)])
        _sync_framework_calls(root, label, offenders)
    assert offenders == [], (
        "sync framework dependencies run in the thread pool per request:\n"
        + "\n".join(offenders)
    )


def test_composition_root_routes_have_no_sync_framework_dependency() -> None:
    from sandloom.core.composition import load_composition_app

    app = load_composition_app(None)
    offenders: list[str] = []
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert routes, "the composition root mounted no API routes"
    for route in routes:
        _sync_framework_calls(route.dependant, route.path, offenders)
    assert offenders == [], "\n".join(sorted(set(offenders)))
