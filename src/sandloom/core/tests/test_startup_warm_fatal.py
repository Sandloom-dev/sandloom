"""The ``infra_warm_instances`` startup task, driver-free.

The task is critical in every environment, and ``aopen_all`` raises in dev only
for a failure that carries ``fatal_at_startup`` (a configuration mistake): that
one aborts startup, any other open failure is logged as a warning and startup
goes on. A provider of this file's own stands in for a driver, so the lane that
runs the kernel alone covers the rule.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import app_factory
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import CacheProvider
from sandloom.core.infra.registry import InfraRegistry, InfraStartupError
from sandloom.core.schema.registry import InMemorySchemaRegistry


class _Misconfigured(RuntimeError):
    fatal_at_startup = True


class _Unreachable(ConnectionError):
    """A transient failure: no ``fatal_at_startup``."""


class _FailingCache(CacheProvider):
    failure: Exception

    async def aopen(self) -> None:
        raise self.failure

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


def _registry(failure: Exception) -> InfraRegistry:
    provider = type("_Cache", (_FailingCache,), {"failure": failure})
    reg = InfraRegistry(
        config={
            "cache": {
                "default": InstanceConfig(
                    capability="cache", name="default", driver="failing", options={}
                )
            }
        }
    )
    reg.register_driver(
        "cache",
        "failing",
        lambda cfg: provider(
            name=cfg.name, driver=cfg.driver, options=dict(cfg.options)
        ),
    )
    return reg


class _CaptureCore:
    def __init__(self) -> None:
        self.startup: dict[str, tuple[Any, bool]] = {}

    def add_startup_task(
        self, fn: Any, *, critical: bool = False, phase: str = "modules"
    ) -> None:
        self.startup[fn.__name__] = (fn, critical)

    def add_shutdown_task(self, fn: Any) -> None:
        pass


def _warm_task(
    monkeypatch: pytest.MonkeyPatch, infra: InfraRegistry, *, is_prod: bool
) -> tuple[Any, bool]:
    monkeypatch.setattr(
        app_factory.lifecycle,
        "settings",
        SimpleNamespace(
            is_prod=is_prod, schema_registry_path=None, production_profile="standard"
        ),
    )
    core = _CaptureCore()
    app_factory._register_infra_lifecycle(
        core,  # type: ignore[arg-type]
        InMemorySchemaRegistry(),
        infra,
    )
    return core.startup["infra_warm_instances"]


@pytest.mark.parametrize("is_prod", [True, False])
def test_the_warm_task_is_critical_in_every_environment(
    monkeypatch: pytest.MonkeyPatch, is_prod: bool
) -> None:
    _, critical = _warm_task(monkeypatch, _registry(_Unreachable()), is_prod=is_prod)
    assert critical is True


async def test_a_fatal_failure_aborts_startup_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _ = _warm_task(
        monkeypatch, _registry(_Misconfigured("no url")), is_prod=False
    )
    with pytest.raises(InfraStartupError, match="cache/default: no url"):
        await task()


async def test_a_non_fatal_failure_only_warns_in_dev(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    task, _ = _warm_task(monkeypatch, _registry(_Unreachable("down")), is_prod=False)
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra.registry"):
        await task()
    warned = [
        r for r in caplog.records if "error opening cache/default" in r.getMessage()
    ]
    assert [r.exc_info[0] for r in warned if r.exc_info] == [_Unreachable]


async def test_a_non_fatal_failure_aborts_startup_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _ = _warm_task(monkeypatch, _registry(_Unreachable("down")), is_prod=True)
    with pytest.raises(InfraStartupError, match="cache/default: down"):
        await task()
