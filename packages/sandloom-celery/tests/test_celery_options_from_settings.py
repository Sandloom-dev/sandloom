"""The ``celery`` task queue reads options, not settings (ADR-0016).

Its broker is an option of the instance in ``INFRA`` (``BROKER_URL`` is a removed
setting) and it has no result backend unless the instance states one. The only
value the kernel puts into its options is the readiness bound
``HEALTH_CHECK_TIMEOUT_SECONDS``, as the driver's class declares it
(``InfraProvider.settings_fill``: ``bounds`` and ``caps``). The
engine, which names no driver, is
``sandloom.core.tests.test_driver_options_fill``; the Redis drivers' side is
``packages/sandloom-redis/tests/test_redis_options_from_settings.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sandloom_celery import taskqueue as celery_taskqueue

from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import (
    DEFAULT_INSTANCE,
    FILLABLE_SETTINGS,
    InfraConfig,
    InstanceConfig,
    load_infra_config,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tests._driver_fill import fill, registry_for


def _cfg(capability: str, name: str, driver: str, **options: Any) -> InstanceConfig:
    return InstanceConfig(
        capability=capability, name=name, driver=driver, options=options
    )


def test_a_named_taskqueue_gets_what_it_leaves_out_and_keeps_what_it_says() -> None:
    config = {
        "taskqueue": {
            "jobs": _cfg("taskqueue", "jobs", "celery", broker="redis://b/2"),
        },
    }
    fill(config)
    jobs = config["taskqueue"]["jobs"].options
    assert jobs == {
        "broker": "redis://b/2",  # stated: kept
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }  # no result backend: no setting names one
    celery_taskqueue.CeleryTaskQueueOptions.model_validate(jobs)


def test_the_default_taskqueue_keeps_its_infra_broker_and_gets_the_probe_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"taskqueue": {"default": {"broker": "redis://broker-host:6379/2"}}},
    )
    options = fill(load_infra_config(settings))["taskqueue"][DEFAULT_INSTANCE].options
    assert options == {
        "broker": "redis://broker-host:6379/2",
        "health_timeout_seconds": settings.health_check_timeout_seconds,
    }  # no backend: the cache url is no result backend


def test_the_probe_bound_follows_the_setting_and_a_lower_stated_one_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "a": _cfg("taskqueue", "a", "celery"),
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=1.0),
        }
    }
    fill(config)
    assert config["taskqueue"]["a"].options["health_timeout_seconds"] == 5.0
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 1.0


def test_a_stated_probe_bound_never_exceeds_the_readiness_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/ready`` stops waiting at ``HEALTH_CHECK_TIMEOUT_SECONDS``: a connect
    allowed 300 s would only outlive its answered probe (option 300, setting 5:
    the probe is bounded by 5)."""
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 5.0)
    config = {
        "taskqueue": {
            "b": _cfg("taskqueue", "b", "celery", health_timeout_seconds=300),
        }
    }
    fill(config)
    assert config["taskqueue"]["b"].options["health_timeout_seconds"] == 5.0
    options = config["taskqueue"]["b"].options
    provider = celery_taskqueue.create_celery_taskqueue(
        _cfg("taskqueue", "b", "celery", **{**options, "broker": "redis://b/2"})
    )
    assert provider._typed.health_timeout_seconds == 5.0


@pytest.mark.parametrize("bound", [0, -1, 301, "soon"])
def test_a_bad_probe_bound_is_refused_at_build(bound: object) -> None:
    with pytest.raises(ValueError, match="health_timeout_seconds"):
        celery_taskqueue.create_celery_taskqueue(
            _cfg(
                "taskqueue",
                "jobs",
                "celery",
                broker="redis://b/2",
                health_timeout_seconds=bound,
            )
        )


def test_a_celery_instance_without_a_broker_is_refused() -> None:
    with pytest.raises(ValueError, match="'broker'"):
        celery_taskqueue.create_celery_taskqueue(_cfg("taskqueue", "jobs", "celery"))


def test_the_celery_fill_names_settings_that_exist() -> None:
    config: InfraConfig = {
        "taskqueue": {DEFAULT_INSTANCE: _cfg("taskqueue", DEFAULT_INSTANCE, "celery")}
    }
    declared = registry_for(config).settings_fill("taskqueue", "celery")
    assert declared is not None
    named = {*declared.bounds.values(), *declared.caps.values()}
    assert named
    assert named <= set(Settings.model_fields), named - set(Settings.model_fields)
    assert named <= FILLABLE_SETTINGS


def test_a_registry_built_from_the_settings_fills_what_drivers_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "infra",
        {"taskqueue": {"default": {"broker": "redis://broker-host:6379/2"}}},
    )
    registry = build_infra_registry(InMemorySchemaRegistry())
    queue = registry.instance_config("taskqueue", DEFAULT_INSTANCE)
    assert queue is not None
    assert queue.options["broker"] == "redis://broker-host:6379/2"
    assert queue.options["health_timeout_seconds"] == (
        settings.health_check_timeout_seconds
    )
