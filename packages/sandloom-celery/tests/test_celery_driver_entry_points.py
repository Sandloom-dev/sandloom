"""The ``sandloom.drivers`` entry point this distribution publishes.

The registry reads the wheel's entry points, never this source tree, so a typo
in ``pyproject.toml`` or a class that lost its ``tier`` fails here, in the job
that tests the built wheel, not when an application boots. No broker is
reached: the driver is loaded and registered, never built.
"""

from __future__ import annotations

import logging
from importlib import metadata
from types import SimpleNamespace
from typing import Any

import pytest
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.driverkit import TaskQueueProvider


def _published() -> dict[str, metadata.EntryPoint]:
    return {
        entry.name: entry
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None
        and entry.dist.name.replace("_", "-") == "sandloom-celery"
    }


def test_the_distribution_publishes_exactly_the_taskqueue_driver() -> None:
    assert sorted(_published()) == ["taskqueue.celery"]


def test_the_entry_point_loads_its_class_and_the_kits_provider_base() -> None:
    loaded = _published()["taskqueue.celery"].load()
    assert loaded is CeleryTaskQueueProvider
    assert issubclass(loaded, TaskQueueProvider)


def test_the_driver_declares_a_tier_production_accepts() -> None:
    """An undeclared tier is ``experimental``: production refuses it (B023)."""
    assert CeleryTaskQueueProvider.tier in {"beta", "stable"}


def _production(*risks: str) -> Any:
    return SimpleNamespace(
        is_prod=True, accepts_risk=lambda risk: risk in risks, infra={}
    )


def test_a_production_registry_accepts_the_driver_without_the_experimental_risk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = {
        "taskqueue": {
            "default": InstanceConfig(
                "taskqueue",
                "default",
                "celery",
                {"broker": "redis://127.0.0.1:1/0"},
            )
        }
    }
    with caplog.at_level(logging.WARNING, logger="sandloom.core.infra"):
        registry = build_infra_registry(
            InMemorySchemaRegistry(), settings=_production(), config=config
        )
    assert registry.driver_tier("taskqueue", "celery") in {"beta", "stable"}
    assert not [r for r in caplog.records if "experimental" in r.getMessage()]
