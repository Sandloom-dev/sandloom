"""The service urls are options of their instances, not settings (flat-urls Z).

An app that selects no Redis or Celery driver starts with no url anywhere. This
file is the kernel's half and imports no driver package (it runs without
Redis); the drivers' refusal of an instance with no url is
``tests/integration/test_driver_url_messages.py``, and the refusal of the
removed flat settings is ``test_config_tiers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from sandloom.core.config.settings import Settings
from sandloom.core.infra import fenced_lease

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


def test_settings_load_with_no_service_url_and_carry_none() -> None:
    cfg = _settings()
    for name in ("cache_url", "broker_url", "cache_socket_timeout_seconds"):
        assert name not in Settings.model_fields
        assert not hasattr(cfg, name)


@pytest.mark.parametrize("name", ["CACHE_URL", "BROKER_URL"])
def test_a_set_url_is_refused_not_accepted_and_ignored(name: str) -> None:
    with pytest.raises(ValidationError, match=f"{name} was removed"):
        _settings(**{name: "redis://c:6379/0"})


def test_a_missing_database_url_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None, JWT_SECRET=_STRONG)  # type: ignore[call-arg]


def _core_with_broker(options: dict[str, Any] | None) -> Any:
    config = None if options is None else SimpleNamespace(options=options)
    infra = SimpleNamespace(instance_config=lambda _capability, _instance: config)
    return SimpleNamespace(infra=infra)


def test_the_beat_scope_of_an_unset_broker_is_empty_not_the_text_none() -> None:
    assert fenced_lease.broker_url_of(SimpleNamespace(infra=None), "default") == ""
    assert fenced_lease.broker_url_of(_core_with_broker(None), "default") == ""
    assert fenced_lease.broker_url_of(_core_with_broker({}), "default") == ""
    assert (
        fenced_lease.broker_url_of(
            _core_with_broker({"broker": "redis://b:6379/1"}), "default"
        )
        == "redis://b:6379/1"
    )


def test_no_setting_names_the_beat_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flat ``broker_url`` on the settings object is not read any more: the
    scope is the instance's ``broker`` option or empty."""
    monkeypatch.setattr(
        fenced_lease, "settings", SimpleNamespace(broker_url="redis://flat:6379/9")
    )
    assert fenced_lease.broker_url_of(_core_with_broker({}), "default") == ""


# The backup plan's reading of a url-less bus (it follows the default cache's url,
# or is skipped when no url exists anywhere) is tested where the hint lives:
# ``test_backup_cli.py`` with stand-in drivers, and the Redis package's
# ``test_redis_backup_plan.py`` with the real Streams bus.
