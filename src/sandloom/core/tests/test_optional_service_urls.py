"""CACHE_URL and BROKER_URL are optional settings (flat-urls stage 0a).

An app that selects no Redis or Celery driver starts with neither set; one that
selects a driver and gives it no url is refused at startup with the INFRA option
to set, never with ``CACHE_URL`` named as the way to configure it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sandloom_celery.taskqueue import CeleryTaskQueueProvider, MissingBrokerError
from sandloom_redis.cache import MissingUrlError, RedisCacheProvider

from sandloom.core.config.settings import Settings
from sandloom.core.infra import fenced_lease
from sandloom.core.infra.config import (
    InfraConfig,
    InstanceConfig,
    _flat_options,
)
from sandloom.core.migrations import backup

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "JWT_SECRET": _STRONG,
}


@pytest.fixture(autouse=True)
def _no_service_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CACHE_URL", raising=False)
    monkeypatch.delenv("BROKER_URL", raising=False)


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


def test_settings_load_with_neither_cache_url_nor_broker_url() -> None:
    cfg = _settings()
    assert cfg.cache_url is None
    assert cfg.broker_url is None


def test_a_set_url_is_still_accepted_and_still_validated() -> None:
    cfg = _settings(CACHE_URL="redis://c:6379/0", BROKER_URL="rediss://b:6379/1")
    assert (cfg.cache_url, cfg.broker_url) == ("redis://c:6379/0", "rediss://b:6379/1")
    for name in ("CACHE_URL", "BROKER_URL"):
        with pytest.raises(ValidationError, match="redis://"):
            _settings(**{name: "http://not-redis"})


def test_a_missing_database_url_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None, JWT_SECRET=_STRONG)  # type: ignore[call-arg]


def _taskqueue_config() -> InfraConfig:
    return {
        "taskqueue": {
            "default": InstanceConfig("taskqueue", "default", "celery", {}),
        }
    }


def test_an_unset_flat_url_gives_the_instance_no_option() -> None:
    config = _taskqueue_config()
    _flat_options(config, "taskqueue", {"broker": None, "backend": None})
    assert config["taskqueue"]["default"].options == {}  # no ``None`` option


def test_a_set_flat_url_still_gives_its_option_and_an_instance_option_wins() -> None:
    config = _taskqueue_config()
    config["taskqueue"]["default"].options = {"backend": "redis://own:6379/2"}
    _flat_options(
        config,
        "taskqueue",
        {"broker": "redis://b:6379/1", "backend": "redis://c:6379/0"},
    )
    assert config["taskqueue"]["default"].options == {
        "broker": "redis://b:6379/1",
        "backend": "redis://own:6379/2",
    }


def test_the_beat_scope_of_an_unset_broker_is_empty_not_the_text_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fenced_lease, "settings", SimpleNamespace(broker_url=None))
    assert fenced_lease.broker_url_of(SimpleNamespace(infra=None), "default") == ""
    monkeypatch.setattr(
        fenced_lease, "settings", SimpleNamespace(broker_url="redis://b:6379/1")
    )
    assert (
        fenced_lease.broker_url_of(SimpleNamespace(infra=None), "default")
        == "redis://b:6379/1"
    )


def test_the_backup_plan_skips_an_eventbus_with_no_url_and_no_cache_url() -> None:
    config = {
        "eventbus": {
            "default": SimpleNamespace(driver="streams", options={}),
            "named": SimpleNamespace(
                driver="streams", options={"url": "redis://own:6379/3"}
            ),
        },
    }
    roles = backup._redis_roles(config, SimpleNamespace(cache_url=None))
    assert roles == {"redis://own:6379/3": ["eventbus:named (streams)"]}
    # ...and the flat ``CACHE_URL`` is still the url of one that has none.
    roles = backup._redis_roles(config, SimpleNamespace(cache_url="redis://c:6379/0"))
    assert roles["redis://c:6379/0"] == ["eventbus:default (streams)"]


@pytest.mark.parametrize("name", ["default", "sessions"])
def test_a_redis_cache_without_a_url_names_the_infra_option(name: str) -> None:
    cfg = InstanceConfig("cache", name, "redis", {})
    with pytest.raises(MissingUrlError) as caught:
        RedisCacheProvider.from_config(cfg)
    message = str(caught.value)
    assert f"cache.{name}.url" in message
    assert "INFRA=" in message
    assert "CACHE_URL" not in message
    # A misconfiguration, so startup aborts in every environment, not only
    # under strict startup (``InfraRegistry._open_one``).
    assert caught.value.fatal_at_startup is True


def test_a_redis_cache_with_a_url_builds_without_connecting() -> None:
    provider = RedisCacheProvider.from_config(
        InstanceConfig("cache", "sessions", "redis", {"url": "redis://x:6379/0"})
    )
    assert provider.name == "sessions"


@pytest.mark.parametrize("name", ["default", "jobs"])
def test_a_celery_taskqueue_without_a_broker_names_the_infra_option(
    name: str,
) -> None:
    cfg = InstanceConfig("taskqueue", name, "celery", {})
    with pytest.raises(MissingBrokerError) as caught:
        CeleryTaskQueueProvider.from_config(cfg)
    message = str(caught.value)
    assert f"taskqueue.{name}.broker" in message
    assert "INFRA=" in message
    assert "BROKER_URL" not in message
    assert caught.value.fatal_at_startup is True
