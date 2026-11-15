"""Every module-facing taskqueue resolution returns the kernel's layer.

A module that reached the raw provider could publish under a job id of its
choosing (``enqueue(task_id=...)``), e.g. the id of another key's pending
reservation: reconcile would then finalise a key whose publish never
happened. So ``taskqueue()``, ``capability("taskqueue")``,
``require_capability("taskqueue")``, DI ``Capability("taskqueue")`` and
``sandloom.sdk.infra.capability(CapabilityRef("taskqueue"))`` all return the
:class:`KernelTaskQueue` wrapper, whose surface has no ``task_id``.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core import di
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.capabilities import CapabilityRef
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.sdk import infra as sdk_infra


@pytest.fixture
def core() -> Any:
    cfg = {
        "notification": {
            "default": InstanceConfig(
                capability="notification", name="default", driver="log"
            ),
        },
        "taskqueue": {
            "default": InstanceConfig(
                capability="taskqueue",
                name="default",
                driver="celery",
                options={"broker": "memory://"},
            ),
            "side": InstanceConfig(
                capability="taskqueue",
                name="side",
                driver="celery",
                options={"broker": "memory://"},
            ),
        },
    }
    registry = build_infra_registry(
        InMemorySchemaRegistry(), config=cfg
    )
    facade = CoreFacade(app=FastAPI())
    facade.set_infra(registry)
    with use_infra(registry):
        yield facade


def _wrapped(queue: object, name: str = "default") -> None:
    assert type(queue) is KernelTaskQueue, type(queue)
    assert queue.name == name


def test_the_kernel_facade_keeps_the_provider(core) -> None:
    # The kernel's own facade (the worker) is not module-facing.
    assert isinstance(core.taskqueue(), CeleryTaskQueueProvider)
    assert isinstance(core.capability("taskqueue"), CeleryTaskQueueProvider)


def test_core_runtime_taskqueue_and_capability(core) -> None:
    runtime = core.runtime_view()
    _wrapped(runtime.taskqueue())
    _wrapped(runtime.capability("taskqueue"))
    _wrapped(runtime.capability("taskqueue", "side"), "side")


def test_core_runtime_require_capability(core) -> None:
    runtime = core.runtime_view()
    _wrapped(runtime.require_capability("taskqueue"))
    _wrapped(
        runtime.require_capability("taskqueue", "side", features={"delay"}), "side"
    )
    _wrapped(runtime.require_capability("taskqueue", version="v1"))


async def test_di_capability_dependency(core) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(core=core)))
    for ref in ("taskqueue", CapabilityRef("taskqueue"), CapabilityRef("taskqueue.v1")):
        dep = di.Capability(ref).dependency
        _wrapped(await dep(request))
    dep = di.Capability("taskqueue", "side", features={"delay"}).dependency
    _wrapped(await dep(request), "side")


def test_sdk_and_di_capability_accessors(core) -> None:
    for accessor in (sdk_infra.capability, di.capability):
        _wrapped(accessor(CapabilityRef("taskqueue")))
        _wrapped(accessor(CapabilityRef("taskqueue.v1"), "side"), "side")
    _wrapped(sdk_infra.taskqueue())
    _wrapped(di.taskqueue("side"), "side")


def test_other_capabilities_are_not_wrapped(core) -> None:
    runtime = core.runtime_view()
    notifier = runtime.capability("notification")
    assert notifier is core.capability("notification")
    ref = CapabilityRef("notification")
    assert sdk_infra.capability(ref) is core.capability("notification")


def test_task_id_is_on_no_module_surface() -> None:
    assert "task_id" not in inspect.signature(KernelTaskQueue.enqueue).parameters
    public = {n for n in dir(KernelTaskQueue) if not n.startswith("_")}
    assert public == {"name", "driver", "features", "enqueue"}
