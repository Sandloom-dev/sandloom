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

from sandloom.core import di
from sandloom.core.drivers.bootstrap import register_builtin_drivers
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.active import use_infra
from sandloom.core.infra.capabilities import CapabilityRef
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.ports.taskqueue import TaskQueueProvider
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.sdk import infra as sdk_infra


class _StubQueue(TaskQueueProvider):
    """A task-queue driver the kernel never heard of: it sends nothing."""

    contract_version = 2
    supported_features = frozenset({"delay"})

    def worker_host(self) -> Any:
        raise NotImplementedError

    async def send(self, *args: Any, **kwargs: Any) -> str:
        return "id"


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
                capability="taskqueue", name="default", driver="stub"
            ),
            "side": InstanceConfig(capability="taskqueue", name="side", driver="stub"),
        },
    }
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, InMemorySchemaRegistry())
    registry.register_driver("taskqueue", "stub", _StubQueue)
    registry.set_config(cfg)
    registry.validate()
    facade = CoreFacade(app=FastAPI())
    facade.set_infra(registry)
    with use_infra(registry):
        yield facade


def _wrapped(queue: object, name: str = "default") -> None:
    assert type(queue) is KernelTaskQueue, type(queue)
    assert queue.name == name


def test_the_kernel_facade_keeps_the_provider(core) -> None:
    # The kernel's own facade (the worker) is not module-facing.
    assert isinstance(core.taskqueue(), _StubQueue)
    assert isinstance(core.capability("taskqueue"), _StubQueue)


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
