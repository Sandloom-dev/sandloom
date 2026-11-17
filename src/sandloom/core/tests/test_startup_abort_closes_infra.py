"""A startup that aborts part-way closes the providers it already opened.

``infra_warm_instances`` opens the instances one by one (``aopen_all``); when a
later one fails (any failure in production, a ``fatal_at_startup`` one in dev)
startup aborts with ``CriticalStartupError``. The lifespan then runs
``run_shutdown``, whose ``close_infra`` task reaches ``aclose_all`` and closes
every provider opened before the failing one: without it a failed start leaks
their connections. ``run_shutdown`` logs a failing task and goes on, so each
test reads the provider's own ``closed`` flag rather than waiting for a raise.
A provider of this file's own stands in for a driver, so the lane that runs the
kernel alone covers the path.
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


def _registry(
    failure: Exception | None,
) -> tuple[InfraRegistry, list[str], list[str]]:
    """``first``, ``second`` and ``fourth`` open fine, ``third`` fails; returns
    the registry and the opened / closed name lists."""
    opened: list[str] = []
    closed: list[str] = []
    healthy = type("_Healthy", (_Cache,), {"opened": opened, "closed": closed})
    broken = type(
        "_Broken", (_Cache,), {"opened": opened, "closed": closed, "failure": failure}
    )
    names = ("first", "second", "third", "fourth")
    reg = InfraRegistry(
        config={
            "cache": {
                name: InstanceConfig(
                    capability="cache", name=name, driver="stub", options={}
                )
                for name in names
            }
        }
    )

    def build(cfg: InstanceConfig) -> CacheProvider:
        cls = broken if cfg.name == "third" else healthy
        return cls(name=cfg.name, driver=cfg.driver, options=dict(cfg.options))

    reg.register_driver("cache", "stub", build)
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

    # What the lifespan does on an aborted start (app_factory/lifecycle.py).
    await run_shutdown(core)

    # ``aclose_all`` closes every provider that was opened, the latest first.
    opened_closed = [name for name in closed if name in opened]
    assert opened_closed == ["fourth", "second", "first"]
