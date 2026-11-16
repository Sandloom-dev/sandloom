"""``process_instance`` with the real Celery driver: what the importable Celery
app reads (``celery -A sandloom_celery.worker inspect``). Lane: ``celery-drivers``.
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.drivers import bootstrap
from sandloom.core.infra.config import InfraConfig
from sandloom.core.infra.registry import InfraRegistry
from sandloom.driverkit.process_config import process_instance


def _infra(monkeypatch: pytest.MonkeyPatch, raw: dict[str, object]) -> None:
    monkeypatch.setattr(settings, "broker_url", None)
    monkeypatch.setattr(settings, "cache_url", None)
    monkeypatch.setattr(settings, "infra", raw)


def test_the_taskqueue_instance_of_infra_is_read_with_its_driver_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(
        monkeypatch,
        {
            "taskqueue": {
                "default": {
                    "driver": "celery",
                    "broker": "redis://127.0.0.1:1/9",
                    "backend": "redis://127.0.0.1:1/8",
                }
            }
        },
    )

    found = process_instance("taskqueue", "default")

    assert found is not None
    assert (found.capability, found.name, found.driver) == (
        "taskqueue",
        "default",
        "celery",
    )
    assert found.options["broker"] == "redis://127.0.0.1:1/9"
    assert found.options["backend"] == "redis://127.0.0.1:1/8"


def test_an_instance_the_configuration_does_not_declare_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _infra(
        monkeypatch,
        {"taskqueue": {"default": {"driver": "celery", "broker": "memory://"}}},
    )

    assert process_instance("taskqueue", "nobody") is None


def test_only_the_taskqueue_drivers_are_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken or slow driver of the database or the event bus must not break
    ``celery inspect``: the plugins loaded are those of the taskqueue alone, even
    with another capability's instance in ``INFRA``."""
    _infra(
        monkeypatch,
        {
            "taskqueue": {"default": {"driver": "celery", "broker": "memory://"}},
            "cache": {"sessions": {"driver": "memory"}},
        },
    )
    loaded: list[set[str]] = []
    real = bootstrap.load_selected_plugins

    def record(registry: InfraRegistry, config: InfraConfig) -> None:
        loaded.append(set(config))
        real(registry, config)

    monkeypatch.setattr(bootstrap, "load_selected_plugins", record)

    process_instance("taskqueue")

    assert loaded == [{"taskqueue"}]


def test_the_options_are_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    _infra(
        monkeypatch,
        {"taskqueue": {"default": {"driver": "celery", "broker": "memory://"}}},
    )

    first = process_instance("taskqueue")
    assert first is not None
    first.options["broker"] = "redis://elsewhere"

    second = process_instance("taskqueue")
    assert second is not None
    assert second.options["broker"] == "memory://"
