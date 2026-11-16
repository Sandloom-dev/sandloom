"""``sandloom.driverkit.process_config``: an instance's options as a process that
boots no application reads them (``celery -A sandloom_celery.worker inspect``).

A driver package imports only ``sandloom.driverkit``, so this is how it reads
``INFRA`` through ``Settings``. Lane: the no-Redis lane over
``src/sandloom/core/tests`` (the instances used here are the kernel's own
``memory`` drivers; nothing connects).
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.tool_infra import FilledInfraConfig
from sandloom.driverkit import process_config
from sandloom.driverkit.process_config import process_instance


def _infra(monkeypatch: pytest.MonkeyPatch, raw: dict[str, object]) -> None:
    monkeypatch.setattr(settings, "infra", raw)


def test_an_instance_of_infra_is_read_with_its_driver_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, {"cache": {"sessions": {"driver": "memory", "ttl": 30}}})

    found = process_instance("cache", "sessions")

    assert found is not None
    assert (found.capability, found.name, found.driver) == (
        "cache",
        "sessions",
        "memory",
    )
    assert found.options["ttl"] == 30


def test_an_instance_the_configuration_does_not_declare_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, {"cache": {"sessions": {"driver": "memory"}}})

    assert process_instance("cache", "nobody") is None
    assert process_instance("objectstore", "sessions") is None


def test_the_options_are_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    _infra(monkeypatch, {"cache": {"sessions": {"driver": "memory", "ttl": 30}}})

    first = process_instance("cache", "sessions")
    assert first is not None
    first.options["ttl"] = 1

    second = process_instance("cache", "sessions")
    assert second is not None
    assert second.options["ttl"] == 30


def test_a_driver_that_cannot_be_loaded_is_refused_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(monkeypatch, {"cache": {"sessions": {"driver": "memory"}}})
    unfilled = FilledInfraConfig(
        {"cache": {"sessions": InstanceConfig("cache", "sessions", "memory")}},
        "driver 'x' failed to import",
    )
    monkeypatch.setattr(process_config, "filled_infra_config", lambda _: unfilled)

    with pytest.raises(RuntimeError, match=r"cache\.sessions.*failed to import"):
        process_instance("cache", "sessions")
