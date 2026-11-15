"""Billing's task declarations as the task queue reads them: the reconciler
stays at-least-once and the module runs on the worker, off the request path
(moved here from the kernel tests, which may not import a bundled module)."""

from __future__ import annotations

from sandloom.core.app_factory.ops import taskqueue_use
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.tasks.registry import declared_task
from sandloom_billing import tasks as billing_tasks


def test_the_billing_reconcile_task_stays_at_least_once() -> None:
    declared = declared_task(billing_tasks.reconcile_operation_intents.name)
    assert declared is not None and declared.at_least_once


def test_billing_stays_background() -> None:
    descriptor = ModuleDescriptor(
        name="sandloom_billing",
        version="1",
        capabilities=[
            CapabilityRequirement(
                "taskqueue", features=frozenset({"periodic", "redelivery"})
            )
        ],
    )
    use = taskqueue_use([descriptor], ["sandloom_billing"])
    assert use.background == ["sandloom_billing"]
    assert use.request_path == {}
