"""A startup that aborts part-way closes the providers it already opened.

``infra_warm_instances`` opens the instances one by one (``aopen_all``); when a
later one fails (any failure in production, a ``fatal_at_startup`` one in dev)
startup aborts with ``CriticalStartupError``. The lifespan then runs
``run_shutdown``, whose ``close_infra`` task reaches ``aclose_all`` and closes
every provider opened before the failing one: without it a failed start leaks
their connections. ``run_shutdown`` logs a failing task and goes on, so each
test reads the provider's own ``closed`` flag rather than waiting for a raise.
A provider of this file's own stands in for a driver, so the lane that runs the
kernel alone covers the path. One test drives the real ``create_app`` lifespan
(the stub instances join the app's own registry); the parametrized one hands
``run_startup`` / ``run_shutdown`` a bare core, in production and dev.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi import FastAPI

from sandloom.core import app_factory
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.lifecycle import CriticalStartupError, run_shutdown, run_startup
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._root_support import create_root_app


class _Misconfigured(RuntimeError):
    fatal_at_startup = True


class _Unreachable(ConnectionError):
    """A transient failure: no ``fatal_at_startup``."""


class _Cache(CacheProvider):
    """Records its own open and close; fails to open when ``failure`` is set."""

    failure: ClassVar[Exception | None] = None
    opened: ClassVar[list[str]]
    closed: ClassVar[list[str]]

    async def aopen(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.opened.append(self.name)

    async def aclose(self) -> None:
        self.closed.append(self.name)

    async def get(self, key: str) -> str | None:  # pragma: no cover - unused
        return None

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:  # pragma: no cover - unused
        return None

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:  # pragma: no cover - unused
        return True

    async def delete(self, key: str) -> None:  # pragma: no cover - unused
        return None

    async def incr(
        self, key: str, *, ttl_seconds: int | None = None
    ) -> int:  # pragma: no cover - unused
        return 1


def _install_stubs(
    reg: InfraRegistry, failure: Exception | None
) -> tuple[list[str], list[str]]:
    """Register the ``stub`` cache driver on ``reg`` and add four instances:
    ``first``, ``second`` and ``fourth`` open fine, ``third`` fails; returns the
    opened / closed name lists."""
    opened: list[str] = []
    closed: list[str] = []
    healthy = type("_Healthy", (_Cache,), {"opened": opened, "closed": closed})
    broken = type(
        "_Broken", (_Cache,), {"opened": opened, "closed": closed, "failure": failure}
    )

    def build(cfg: InstanceConfig) -> CacheProvider:
        cls = broken if cfg.name == "third" else healthy
        return cls(name=cfg.name, driver=cfg.driver, options=dict(cfg.options))

    reg.register_driver("cache", "stub", build)
    for name in ("first", "second", "third", "fourth"):
        reg.configure_instance(
            InstanceConfig(capability="cache", name=name, driver="stub", options={})
        )
    return opened, closed


def _registry(
    failure: Exception | None,
) -> tuple[InfraRegistry, list[str], list[str]]:
    """A registry of the four stub instances; returns it and the opened / closed
    name lists."""
    reg = InfraRegistry()
    opened, closed = _install_stubs(reg, failure)
    return reg, opened, closed


def _core(
    monkeypatch: pytest.MonkeyPatch, infra: InfraRegistry, *, is_prod: bool
) -> CoreFacade:
    monkeypatch.setattr(
        app_factory.lifecycle,
        "settings",
        SimpleNamespace(
            is_prod=is_prod, schema_registry_path=None, production_profile="standard"
        ),
    )
    core = CoreFacade(app=FastAPI())
    app_factory._register_infra_lifecycle(core, InMemorySchemaRegistry(), infra)
    return core


@pytest.mark.parametrize(
    ("is_prod", "failure"),
    [(True, _Unreachable("down")), (False, _Misconfigured("no url"))],
    ids=["prod-any-failure", "dev-fatal-failure"],
)
async def test_an_aborted_startup_closes_what_it_opened(
    monkeypatch: pytest.MonkeyPatch, is_prod: bool, failure: Exception
) -> None:
    infra, opened, closed = _registry(failure)
    core = _core(monkeypatch, infra, is_prod=is_prod)

    with pytest.raises(CriticalStartupError, match="infra_warm_instances"):
        await run_startup(core)

    # ``aopen_all`` opens every instance and reports the failures together: the
    # two before the failing one are open, and so is the one after it.
    assert opened == ["first", "second", "fourth"]
    assert closed == [], "nothing is closed before the lifespan's teardown"

    # Stands in for the lifespan's own ``except`` branch, which the test below
    # drives: this one covers the registry's side (``run_shutdown`` reaches
    # ``aclose_all``), in production and dev, without booting an app.
    await run_shutdown(core)

    # ``aclose_all`` closes every provider that was opened, the latest first.
    opened_closed = [name for name in closed if name in opened]
    assert opened_closed == ["fourth", "second", "first"]


async def test_the_lifespan_of_an_app_closes_what_an_aborted_start_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``create_app`` lifespan: its ``except`` branch (app_factory/
    lifecycle.py) runs ``run_shutdown`` when startup aborts, and a provider that
    opened before the failing one is closed by it, not leaked."""
    real_build = app_factory.build_infra_registry
    stubs: dict[str, list[str]] = {}

    def build_with_stubs(
        schema_registry: InMemorySchemaRegistry,
    ) -> InfraRegistry:
        registry = real_build(schema_registry)
        stubs["opened"], stubs["closed"] = _install_stubs(
            registry, _Misconfigured("no url")
        )
        return registry

    monkeypatch.setattr(app_factory, "build_infra_registry", build_with_stubs)
    app = create_root_app()

    with pytest.raises(CriticalStartupError, match="infra_warm_instances"):
        async with app.router.lifespan_context(app):
            pytest.fail("the app must not serve after an aborted start")

    assert stubs["opened"] == ["first", "second", "fourth"]
    assert [n for n in stubs["closed"] if n in stubs["opened"]] == [
        "fourth",
        "second",
        "first",
    ]
