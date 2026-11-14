"""Which loaded modules use the taskqueue on the request path (readiness).

``redelivery`` is background only when every ``at_least_once`` task is
``@periodic``: a module with a task a request enqueues stays on the request
path, so a broker outage drops web readiness for it.
"""

from __future__ import annotations

import sys
import types
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from sandloom.core.app_factory.ops import taskqueue_use
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor

_PERIODIC_ONLY = """
from sandloom.sdk.tasks import periodic

@periodic(cron="*/5 * * * *", id="rt.sweep", at_least_once=True)
def sweep():
    return 0
"""
_REDELIVERY_ONLY = """
from sandloom.sdk import tasks

@tasks.task(at_least_once=True)
def retried():
    return 0
"""
_MIXED = _PERIODIC_ONLY + _REDELIVERY_ONLY


def _package(
    root: Path, monkeypatch: pytest.MonkeyPatch, name: str, tasks: str
) -> None:
    directory = root / name
    directory.mkdir()
    (directory / "__init__.py").write_text("")
    (directory / "tasks.py").write_text(tasks)
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    package.__spec__ = ModuleSpec(name, None, is_package=True)
    monkeypatch.setitem(sys.modules, name, package)


def _descriptor(name: str, *features: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        name=name,
        version="1",
        capabilities=[CapabilityRequirement("taskqueue", features=frozenset(features))],
    )


def test_redelivery_only_request_path_module_is_not_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package(tmp_path, monkeypatch, "rt_redelivery", _REDELIVERY_ONLY)
    use = taskqueue_use([_descriptor("rt_redelivery", "redelivery")], ["rt_redelivery"])
    assert use.request_path == {"default": ["rt_redelivery"]}
    assert use.background == []


def test_a_plain_task_beside_a_periodic_one_stays_on_the_request_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package(tmp_path, monkeypatch, "rt_mixed", _MIXED)
    use = taskqueue_use(
        [_descriptor("rt_mixed", "periodic", "redelivery")], ["rt_mixed"]
    )
    assert use.request_path == {"default": ["rt_mixed"]}
    assert use.background == []


def test_an_all_periodic_redelivery_module_is_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package(tmp_path, monkeypatch, "rt_periodic", _PERIODIC_ONLY)
    use = taskqueue_use(
        [_descriptor("rt_periodic", "periodic", "redelivery")], ["rt_periodic"]
    )
    assert use.request_path == {}
    assert use.background == ["rt_periodic"]
