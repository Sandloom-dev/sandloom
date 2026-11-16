"""CACHE_URL and BROKER_URL are optional settings (flat-urls stage 0a).

An app that selects no Redis or Celery driver starts with neither set. This file
is the kernel's half and imports no driver package (it runs without Redis);
the drivers' refusal of an instance with no url is
``tests/integration/test_driver_url_messages.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

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


def test_the_backup_plan_follows_a_url_less_eventbus_to_the_default_cache_url() -> None:
    """A streams bus with no url runs on the default cache client: its stream data
    (required) is in the Redis ``cache.default`` points at (INFRA), not lost from
    the plan when ``CACHE_URL`` is unset, and ahead of ``CACHE_URL`` when both
    exist."""
    config = {
        "cache": {
            "default": SimpleNamespace(
                driver="redis", options={"url": "redis://infra:6379/0"}
            )
        },
        "eventbus": {"default": SimpleNamespace(driver="streams", options={})},
    }
    expected = {"redis://infra:6379/0": ["cache:default", "eventbus:default (streams)"]}
    assert backup._redis_roles(config, SimpleNamespace(cache_url=None)) == expected
    flat = SimpleNamespace(cache_url="redis://flat:6379/0")
    assert backup._redis_roles(config, flat) == expected
