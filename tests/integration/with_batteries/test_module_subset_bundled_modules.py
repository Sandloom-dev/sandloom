"""Subsets of the bundled modules boot standalone (moved from the kernel's
``test_module_subset.py``, KB-05c).

The loader-level subsetting the deploy profiles rely on, exercised on the
standard modules: each subset loads with the providers of what it requires,
mounts its routes and none of the others', and a subset whose modules have
``tasks.py`` loads on plain sandloom without Celery (KB-01). The kernel's
own suite keeps the rules that hold for any module set.
"""

from __future__ import annotations

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.loader.plugin_loader import load_modules_with_summary
from sandloom.core.tests.test_module_subset import (
    DEFAULT_MODULES,
    _fresh_core,
    _route_paths,
    _subset,
)


@pytest.mark.parametrize(
    "named,expected_prefix,absent_module,absent_prefix",
    [
        (["sandloom_tenancy"], "/api/v1/orgs", "sandloom_identity", "/auth"),
        (["sandloom_identity"], "/auth", "sandloom_tenancy", "/api/v1/orgs"),
        (
            ["sandloom_tenancy", "sandloom_billing"],
            "/api/v1/billing",
            "sandloom_identity",
            "/auth",
        ),
        (["sandloom_billing"], "/api/v1/billing", "sandloom_tenancy", "/api/v1/orgs"),
    ],
)
def test_module_subset_boots_alone(
    monkeypatch: pytest.MonkeyPatch,
    named: list[str],
    expected_prefix: str,
    absent_module: str,
    absent_prefix: str,
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    modules = _subset(*named)
    monkeypatch.setattr(settings, "sandloom_modules", modules)

    core = _fresh_core()
    summary = load_modules_with_summary(core)

    assert summary.success(), summary.to_dict()
    assert {d.name for d in summary.loaded} == set(modules)

    paths = _route_paths(core)
    assert any(p.startswith(expected_prefix) for p in paths)
    if absent_module not in modules:
        assert not any(p.startswith(absent_prefix) for p in paths)


def test_full_monolith_set_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandloom_modules", list(DEFAULT_MODULES))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)

    core = _fresh_core()
    summary = load_modules_with_summary(core)
    assert summary.success(), summary.to_dict()
    assert len(summary.loaded) == len(DEFAULT_MODULES)

    paths = _route_paths(core)
    for prefix in ("/auth", "/api/v1/orgs", "/api/v1/billing"):
        assert any(p.startswith(prefix) for p in paths), f"missing {prefix} routes"


#: Run in a fresh interpreter by the tests below: Celery is made unimportable
#: before anything is imported (an interpreter that already imported it
#: would serve ``celery.*`` from ``sys.modules``), then the subset named by
#: argv[1] loads and its tasks are enqueued.
_WITHOUT_CELERY = """
import sys

class _NoCelery:
    def find_spec(self, name, path=None, target=None):
        if name == "celery" or name.startswith("celery."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, _NoCelery())

import importlib, json
from fastapi import FastAPI
from sandloom.core.config.settings import settings
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import load_modules_with_summary
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.sdk.tasks import cron
from sandloom.sdk._prim.tasks import TaskQueueNotInstalled, _UnqueuedTask

modules = json.loads(sys.argv[1])
settings.module_load_entrypoints = False
settings.sandloom_modules = modules
settings.module_required = modules
core = CoreFacade(app=FastAPI())
core.register_schema_registry(InMemorySchemaRegistry())
summary = load_modules_with_summary(core)
assert summary.success(), summary.to_dict()
refused = {}
for module in modules:
    try:
        tasks = importlib.import_module(module + ".tasks")
    except ModuleNotFoundError:
        continue
    for task in vars(tasks).values():
        if isinstance(task, _UnqueuedTask):
            try:
                task.delay()
            except TaskQueueNotInstalled as exc:
                refused[task.name] = str(exc)
try:
    cron("0 3 * * *")
except TaskQueueNotInstalled as exc:
    refused["cron"] = str(exc)
assert "celery" not in sys.modules
print(json.dumps({"loaded": [d.name for d in summary.loaded], "refused": refused}))
"""


@pytest.mark.parametrize(
    "module,task",
    [
        (
            "sandloom_billing",
            "sandloom_billing.tasks.reconcile_operation_intents",
        ),
        (
            "sandloom_identity",
            "sandloom_identity.tasks.cleanup_expired_refresh_tokens",
        ),
    ],
)
def test_a_module_with_tasks_loads_without_celery(module: str, task: str) -> None:
    """KB-01: ``tasks.py`` on the typed ``shared_task`` loads on plain sandloom.

    Celery is the optional extra; without it the subset still loads, every
    enqueue (and a beat ``cron``) refuses with ``TaskQueueNotInstalled``
    naming the extra, and Celery is never imported.
    """
    import json
    import subprocess
    import sys

    modules = _subset(module)
    done = subprocess.run(
        [sys.executable, "-c", _WITHOUT_CELERY, json.dumps(modules)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert set(result["loaded"]) == set(modules)
    assert task in result["refused"], result
    for message in result["refused"].values():
        assert "pip install 'sandloom[celery]'" in message
