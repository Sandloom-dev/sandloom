"""The ``celery`` driver is what the kernel's facade keeps and what a module
reaches only through the kernel's layer.

The wrapping on every module-facing path is
``sandloom.core.tests.test_taskqueue_module_paths`` (on a stub driver); this is
the same two doors over the real Celery provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks.queue import KernelTaskQueue


@pytest.fixture
def core() -> Iterator[Any]:
    cfg = {
        "taskqueue": {
            "default": InstanceConfig(
                capability="taskqueue",
                name="default",
                driver="celery",
                options={"broker": "memory://"},
            ),
        },
    }
    registry = build_infra_registry(InMemorySchemaRegistry(), config=cfg)
    facade = CoreFacade(app=FastAPI())
    facade.set_infra(registry)
    with use_infra(registry):
        yield facade


def test_the_kernel_facade_keeps_the_celery_provider(core) -> None:
    assert isinstance(core.taskqueue(), CeleryTaskQueueProvider)
    assert isinstance(core.capability("taskqueue"), CeleryTaskQueueProvider)


def test_a_module_reaches_the_celery_provider_only_through_the_layer(core) -> None:
    runtime = core.runtime_view()
    for queue in (runtime.taskqueue(), runtime.capability("taskqueue")):
        assert type(queue) is KernelTaskQueue, type(queue)
        assert isinstance(queue._provider, CeleryTaskQueueProvider)
