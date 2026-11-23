"""A configuration mistake is refused or warned about, never silent.

* the flat ``STORAGE_TYPE`` / ``NOTIFIER_BACKEND`` no longer replace the driver
  an app's composition (or ``INFRA``) selects, unless the operator sets them,
  and then they say so;
* a misspelled framework setting warns in dev and test (prod refuses it) and is
  listed by ``sandloom doctor`` and ``sandloom config dump``;
* a misspelled option of an ``INFRA`` instance is refused with a code, the
  accepted keys and the nearest one.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import sandloom.core.infra.config as config_module
from sandloom.core.cli import config as config_cli
from sandloom.core.cli import doctor
from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.filesystem_storage import Storage
from sandloom.core.error_codes import code_for_exception
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    InstanceConfig,
    load_infra_config,
    resolved_instances,
    use_default_infra,
)
from sandloom.core.infra.registry import ConfigSchemaError, UnknownDriverOptionError
from sandloom.core.schema.registry import InMemorySchemaRegistry

_STRONG = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
_BASE = {"DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d", "JWT_SECRET": _STRONG}

_S3_AND_SMTP: dict[str, Any] = {
    "database": {"default": {"driver": "sqlalchemy"}},
    "objectstore": {"default": {"driver": "s3", "bucket": "b"}},
    "notification": {"default": {"driver": "smtp", "host": "h"}},
}


@pytest.fixture
def app_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's composition selects s3 and smtp; no flat setting, no INFRA."""
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    monkeypatch.setattr(config_module, "_WARNED", set())
    monkeypatch.setattr(settings, "infra", {}, raising=False)
    monkeypatch.setattr(settings, "storage_type", None)
    monkeypatch.setattr(settings, "notifier_backend", None)
    use_default_infra(_S3_AND_SMTP)


# ------------------------------------------------------- flat defaults ----


@pytest.mark.usefixtures("app_selection")
def test_the_apps_drivers_stand_when_no_flat_setting_is_set() -> None:
    cfg = load_infra_config(settings)
    store = cfg["objectstore"][DEFAULT_INSTANCE]
    mail = cfg["notification"][DEFAULT_INSTANCE]
    assert (store.driver, store.options["bucket"]) == ("s3", "b")
    assert "path" not in store.options
    assert (mail.driver, mail.options["host"]) == ("smtp", "h")
    assert resolved_instances(settings, "notification")[DEFAULT_INSTANCE].driver == (
        "smtp"
    )


@pytest.mark.usefixtures("app_selection")
def test_a_capability_nothing_selects_falls_back_to_local_and_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_default_infra({"database": {"default": {"driver": "sqlalchemy"}}})
    cfg = load_infra_config(settings)
    assert cfg["objectstore"][DEFAULT_INSTANCE].driver == "local"
    assert cfg["notification"][DEFAULT_INSTANCE].driver == "log"
    assert resolved_instances(settings, "notification")[DEFAULT_INSTANCE].driver == (
        "log"
    )


@pytest.mark.usefixtures("app_selection")
def test_a_flat_setting_that_replaces_the_apps_driver_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "storage_type", "local")
    monkeypatch.setattr(settings, "notifier_backend", "log")
    with caplog.at_level(logging.WARNING, logger="sandloom.infra"):
        cfg = load_infra_config(settings)
    assert cfg["objectstore"][DEFAULT_INSTANCE].driver == "local"
    assert cfg["notification"][DEFAULT_INSTANCE].driver == "log"
    text = caplog.text
    assert "STORAGE_TYPE=local replaces the objectstore.default driver" in text
    assert "NOTIFIER_BACKEND=log replaces the notification.default driver" in text


@pytest.mark.usefixtures("app_selection")
def test_a_flat_setting_that_agrees_with_the_app_is_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "storage_type", "s3")
    monkeypatch.setattr(settings, "notifier_backend", "smtp")
    with caplog.at_level(logging.WARNING, logger="sandloom.infra"):
        cfg = load_infra_config(settings)
    assert not caplog.records
    assert cfg["objectstore"][DEFAULT_INSTANCE].options["bucket"] == "b"
    assert cfg["notification"][DEFAULT_INSTANCE].options["host"] == "h"


@pytest.mark.usefixtures("app_selection")
def test_infra_that_overrides_an_explicit_flat_setting_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "storage_type", "local")
    monkeypatch.setattr(
        settings,
        "infra",
        {"objectstore": {"default": {"driver": "s3", "bucket": "other"}}},
        raising=False,
    )
    with caplog.at_level(logging.WARNING, logger="sandloom.infra"):
        cfg = load_infra_config(settings)
    assert cfg["objectstore"][DEFAULT_INSTANCE].driver == "s3"
    assert "INFRA.objectstore.default selects s3; STORAGE_TYPE=local is ignored" in (
        caplog.text
    )


@pytest.mark.usefixtures("app_selection")
def test_a_warning_is_written_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "storage_type", "local")
    with caplog.at_level(logging.WARNING, logger="sandloom.infra"):
        load_infra_config(settings)
        load_infra_config(settings)
    assert caplog.text.count("STORAGE_TYPE=local replaces") == 1


@pytest.mark.usefixtures("app_selection")
def test_infra_naming_the_driver_gets_one_warning_not_two(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "notifier_backend", "log")
    monkeypatch.setattr(
        settings,
        "infra",
        {"notification": {"default": {"driver": "smtp", "host": "other"}}},
        raising=False,
    )
    with caplog.at_level(logging.WARNING, logger="sandloom.infra"):
        load_infra_config(settings)
    assert "NOTIFIER_BACKEND=log is ignored" in caplog.text
    assert "replaces" not in caplog.text


@pytest.mark.parametrize("name", ["STORAGE_TYPE", "NOTIFIER_BACKEND"])
def test_an_empty_selector_is_unset(name: str) -> None:
    """What a compose file's ``${NAME:-}`` passes when the operator sets nothing."""
    loaded = Settings(_env_file=None, **{name: ""}, **_BASE)  # type: ignore[call-arg]
    field = {"STORAGE_TYPE": "storage_type", "NOTIFIER_BACKEND": "notifier_backend"}
    assert getattr(loaded, field[name]) is None


def test_an_empty_selector_keeps_the_composition_s_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "_PIN", None)
    monkeypatch.setattr(config_module, "_APP", None)
    monkeypatch.setattr(config_module, "_WARNED", set())
    use_default_infra(_S3_AND_SMTP)
    loaded = Settings(  # type: ignore[call-arg]
        _env_file=None, STORAGE_TYPE="", NOTIFIER_BACKEND="", **_BASE
    )
    monkeypatch.setattr(loaded, "infra", {}, raising=False)
    cfg = load_infra_config(loaded)
    assert cfg["objectstore"][DEFAULT_INSTANCE].driver == "s3"
    assert cfg["notification"][DEFAULT_INSTANCE].options["host"] == "h"


def test_a_wrong_selector_value_is_still_refused() -> None:
    with pytest.raises(ValueError, match="NOTIFIER_BACKEND"):
        Settings(_env_file=None, NOTIFIER_BACKEND="nope", **_BASE)  # type: ignore[call-arg]


@pytest.mark.usefixtures("app_selection")
def test_a_plugin_store_gets_no_path_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_default_infra({"objectstore": {"default": {"driver": "gcs"}}})
    cfg = load_infra_config(settings)
    assert "path" not in cfg["objectstore"][DEFAULT_INSTANCE].options


@pytest.mark.usefixtures("app_selection")
def test_the_local_store_gets_the_path_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_default_infra({"database": {"default": {"driver": "sqlalchemy"}}})
    cfg = load_infra_config(settings)
    assert cfg["objectstore"][DEFAULT_INSTANCE].options["path"] == (
        settings.storage_path
    )


def test_an_empty_storage_path_is_accepted_by_settings_and_refused_by_the_driver() -> (
    None
):
    Settings(  # type: ignore[call-arg]
        _env_file=None, STORAGE_TYPE="", STORAGE_PATH="", **_BASE
    )
    with pytest.raises(ValueError, match="STORAGE_PATH is required"):
        Storage(storage_type="local", path="")


# ------------------------------------------------- misspelled settings ----


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_a_misspelled_setting_warns_outside_production(
    environment: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
        loaded = Settings(  # type: ignore[call-arg]
            _env_file=None, ENVIRONMENT=environment, TENANCY_MOD="single", **_BASE
        )
    assert loaded.tenancy_mode != "single"
    assert "TENANCY_MOD is ignored; did you mean TENANCY_MODE?" in caplog.text


def test_a_misspelled_setting_is_refused_in_production() -> None:
    with pytest.raises(ValueError, match="TENANCY_MOD.*did you mean TENANCY_MODE"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, ENVIRONMENT="prod", TENANCY_MOD="single", **_BASE
        )


def test_a_known_setting_and_an_unrelated_variable_warn_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TENANCY_MODE", "multi")
    monkeypatch.setenv("SOMETHING_ELSE_ENTIRELY", "1")
    with caplog.at_level(logging.WARNING, logger="sandloom.settings"):
        Settings(_env_file=None, ENVIRONMENT="dev", **_BASE)  # type: ignore[call-arg]
    assert "is ignored; did you mean" not in caplog.text


def test_doctor_lists_a_misspelled_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANCY_MOD", "single")
    report = doctor.Report()
    doctor.check_misspelled_settings(report)
    found = [c for c in report.checks if c.name == "misspelled_setting"]
    assert [c.status for c in found] == [doctor.WARN]
    assert "TENANCY_MOD" in found[0].message and "TENANCY_MODE" in found[0].message


def test_doctor_is_quiet_without_a_misspelled_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TENANCY_MOD", raising=False)
    report = doctor.Report()
    doctor.check_misspelled_settings(report)
    assert not report.checks


def test_config_dump_prints_a_misspelled_setting_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TENANCY_MOD", "single")
    assert config_cli.main(["dump", "--kernel-only", "--only", "LOG_"]) == 0
    captured = capsys.readouterr()
    assert "warning: TENANCY_MOD is not a framework setting" in captured.err
    assert "did you mean TENANCY_MODE" in captured.err
    assert "TENANCY_MOD" not in captured.out


# ------------------------------------------------------- INFRA options ----


def _build(driver: str, capability: str, options: dict[str, Any]) -> None:
    config = {
        capability: {"x": InstanceConfig(capability, "x", driver, options)},
    }
    build_infra_registry(InMemorySchemaRegistry(), config=config, load_plugins=False)


def test_a_misspelled_infra_option_is_refused_with_a_code_and_the_keys() -> None:
    with pytest.raises(UnknownDriverOptionError) as raised:
        _build("smtp", "notification", {"host": "h", "prot": 25})
    message = str(raised.value)
    assert "SANDLOOM-B042" in message
    assert "'prot' (did you mean 'port'?)" in message
    assert "it accepts:" in message and "host" in message and "port" in message
    assert code_for_exception(raised.value) == "SANDLOOM-B042"


def test_an_unknown_option_without_a_near_miss_lists_the_accepted_keys() -> None:
    with pytest.raises(UnknownDriverOptionError) as raised:
        _build("smtp", "notification", {"host": "h", "zzzzzzzz": 1})
    assert "'zzzzzzzz'" in str(raised.value)
    assert "did you mean" not in str(raised.value)


def test_another_options_error_is_still_the_generic_schema_error() -> None:
    """A wrong type is not a typo: the generic refusal, no unknown-option code."""
    with pytest.raises(ConfigSchemaError) as raised:
        _build("smtp", "notification", {"host": "h", "port": "not-a-port"})
    assert not isinstance(raised.value, UnknownDriverOptionError)
    assert "SANDLOOM-B042" not in str(raised.value)
