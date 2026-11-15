"""SMTP_*, S3_* and EVENT_STREAM_* are driver options, not flat settings.

The smtp notifier, the s3 object store and the streams event bus read their
configuration from the options of their INFRA instance. The flat settings that
used to feed them are REMOVED: still setting one is refused with the INFRA
spelling to move to (an empty value only warns), and the defaults those
settings carried (STARTTLS on, the streams retention) live on the driver.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from sandloom.core.config import tiers
from sandloom.core.config.settings import Settings
from sandloom.core.infra.config import load_infra_config

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
    "CACHE_URL": "redis://cache-redis:6379/0",
    "BROKER_URL": "redis://cache-redis:6379/1",
    "JWT_SECRET": "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b",
}

#: setting -> (INFRA capability, driver option) it moved to.
MOVED = {
    "SMTP_HOST": ("notification", "host"),
    "SMTP_PORT": ("notification", "port"),
    "SMTP_USERNAME": ("notification", "username"),
    "SMTP_PASSWORD": ("notification", "password"),
    "SMTP_FROM": ("notification", "from_addr"),
    "SMTP_USE_TLS": ("notification", "use_tls"),
    "S3_BUCKET": ("objectstore", "bucket"),
    "S3_REGION": ("objectstore", "region"),
    "S3_ACCESS_KEY_ID": ("objectstore", "access_key_id"),
    "S3_SECRET_ACCESS_KEY": ("objectstore", "secret_access_key"),
    "EVENT_STREAM_RETENTION_SECONDS": ("eventbus", "retention_seconds"),
    "EVENT_STREAM_MAXLEN": ("eventbus", "max_length"),
    "EVENT_STREAM_PREFIX": ("eventbus", "prefix"),
}


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for alias in {f.alias or n.upper() for n, f in Settings.model_fields.items()}:
        monkeypatch.delenv(alias, raising=False)
    for name in MOVED:
        monkeypatch.delenv(name, raising=False)


def _settings(**env: Any) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **env})  # type: ignore[call-arg]


REMOVED_BY_NAME = {removed.name: removed for removed in tiers.REMOVED}


def _active(name: str) -> str:
    """A value that is no longer supported: not the inert one."""
    return "false" if REMOVED_BY_NAME[name].inert is True else "x1"


@pytest.mark.parametrize("name", MOVED)
def test_the_flat_setting_is_removed_and_names_its_instance_option(name: str) -> None:
    capability, option = MOVED[name]
    removed = REMOVED_BY_NAME[name]
    assert removed.removed_in == "1.0"
    assert f"`{option}`" in removed.hint
    assert f"`{capability}`" in removed.hint
    assert "INFRA" in removed.hint
    assert name not in {f.alias or n.upper() for n, f in Settings.model_fields.items()}


@pytest.mark.parametrize("name", MOVED)
def test_a_configured_value_is_refused_with_the_infra_spelling(name: str) -> None:
    with pytest.raises(ValueError, match="is no longer supported") as raised:
        _settings(**{name: _active(name)})
    assert name in str(raised.value)
    assert MOVED[name][1] in str(raised.value)


@pytest.mark.parametrize("name", MOVED)
def test_the_process_environment_is_refused_too(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, _active(name))
    with pytest.raises(ValueError, match=name):
        _settings()


@pytest.mark.parametrize("name", [n for n in MOVED if n != "SMTP_USE_TLS"])
def test_an_empty_value_only_warns(name: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
        _settings(**{name: ""})
    assert f"{name} was removed in 1.0" in caplog.text


def test_smtp_use_tls_true_was_the_default_and_only_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
        _settings(SMTP_USE_TLS="true")
    assert "SMTP_USE_TLS was removed in 1.0" in caplog.text


def test_smtp_use_tls_false_is_refused_not_silently_turned_back_on() -> None:
    """``false`` was a choice (a local mail catcher): ignoring it would turn
    STARTTLS on behind the operator's back, so it fails and names the option."""
    with pytest.raises(ValueError, match="SMTP_USE_TLS=false is no longer supported"):
        _settings(SMTP_USE_TLS="false")


def _infra(monkeypatch: pytest.MonkeyPatch, infra: dict[str, Any], **env: Any) -> Any:
    monkeypatch.setenv("INFRA", json.dumps(infra))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_infra_config(_settings())


def test_an_smtp_instance_keeps_starttls_on_unless_the_option_says_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _infra(
        monkeypatch, {"notification": {"default": {"driver": "smtp", "host": "mail"}}}
    )["notification"]["default"]
    assert on.driver == "smtp"
    assert on.options["use_tls"] is True
    off = _infra(
        monkeypatch,
        {
            "notification": {
                "default": {"driver": "smtp", "host": "mail", "use_tls": False}
            }
        },
    )["notification"]["default"]
    assert off.options["use_tls"] is False


def test_only_an_smtp_instance_gets_the_tls_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_instance = _infra(
        monkeypatch,
        {"notification": {"default": {"driver": "file", "path": "/m.log"}}},
    )["notification"]["default"]
    assert file_instance.options == {"path": "/m.log"}


def test_a_streams_bus_takes_its_retention_options_from_infra_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _infra(
        monkeypatch,
        {
            "eventbus": {
                "default": {
                    "driver": "streams",
                    "retention_seconds": 3600,
                    "max_length": 500,
                    "prefix": "acme",
                }
            }
        },
    )["eventbus"]["default"]
    assert bus.options["retention_seconds"] == 3600
    assert bus.options["max_length"] == 500
    assert bus.options["prefix"] == "acme"
    # the kernel states none of them when INFRA does not: the driver's own
    # defaults apply (7 days, unbounded length, its own key namespace)
    bare = _infra(monkeypatch, {"eventbus": {"default": {"driver": "streams"}}})[
        "eventbus"
    ]["default"]
    for option in ("retention_seconds", "max_length", "prefix"):
        assert option not in bare.options


def test_the_s3_object_store_is_configured_by_instance_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _infra(
        monkeypatch,
        {"objectstore": {"default": {"driver": "s3", "bucket": "uploads"}}},
        STORAGE_TYPE="s3",
    )["objectstore"]["default"]
    assert store.driver == "s3"
    assert store.options["bucket"] == "uploads"
    assert "path" not in store.options
