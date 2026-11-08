"""Monolith ↔ microservices guarantee: any module subset boots standalone.

The deploy profiles in deploy/docker-compose.*.yml rely on SANDLOOM_MODULES
subsetting; these tests pin that behaviour at the loader level. A subset is
the named modules plus the default modules that provide what they REQUIRE
(``with_required_providers``), so a module that gains a required contract
needs no edit here.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI

from sandloom.core.config.settings import settings
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.core.facade import CoreFacade
from sandloom.core.loader.plugin_loader import load_modules_with_summary
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing.boot import module_topology, with_required_providers

#: The standard modules installed here (import paths).
DEFAULT_MODULES = [m.path for m in installed_modules()]


def _subset(*modules: str) -> list[str]:
    return with_required_providers(list(modules), DEFAULT_MODULES)


def _fresh_core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


def _route_paths(core: CoreFacade) -> set[str]:
    return {getattr(r, "path", "") for r in core.app.router.routes}


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


def test_blacklist_excludes_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    # A module no other default module requires (billing, unless that changed).
    required = {
        provider
        for module in DEFAULT_MODULES
        for provider in _subset(module)
        if provider != module
    }
    excluded = next(
        m for m in ("sandloom_billing", *reversed(DEFAULT_MODULES)) if m not in required
    )
    monkeypatch.setattr(settings, "sandloom_modules", list(DEFAULT_MODULES))
    monkeypatch.setattr(settings, "module_blacklist", [excluded])

    core = _fresh_core()
    summary = load_modules_with_summary(core)
    loaded = {d.name for d in summary.loaded}
    assert excluded not in loaded
    assert set(DEFAULT_MODULES) - {excluded} <= loaded


def test_a_subset_includes_the_providers_of_required_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    for module in DEFAULT_MODULES:
        subset = _subset(module)
        provided = {c for m in subset for c in module_topology(m).provides}
        for member in subset:
            assert module_topology(member).requires <= provided, (module, subset)


async def _ready_status(core: CoreFacade, monkeypatch: pytest.MonkeyPatch) -> int:
    """/ready of a subset process with healthy (stubbed) local infra."""
    import httpx

    from sandloom.core.app_factory import _install_ops_endpoints
    from sandloom.core.descriptors import HealthResult

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.health_checks.clear()
    core.register_health_check("db", ok)
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "service_bindings", {})
    _install_ops_endpoints(core.app, core)
    transport = httpx.ASGITransport(app=core.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return (await client.get("/ready")).status_code


@pytest.mark.parametrize("module", DEFAULT_MODULES)
async def test_each_single_module_subset_is_ready(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    modules = _subset(module)
    monkeypatch.setattr(settings, "sandloom_modules", modules)
    monkeypatch.setattr(settings, "module_required", list(modules))
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    core = _fresh_core()
    load_modules_with_summary(core)
    assert await _ready_status(core, monkeypatch) == 200


async def test_subset_whose_only_module_failed_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SANDLOOM_MODULES='["sandloom.biling"]'`` (typo) with the module optional: the
    process boots but serves nothing — it must never report ready."""
    monkeypatch.setattr(settings, "sandloom_modules", ["sandloom.biling"])
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    core = _fresh_core()
    summary = load_modules_with_summary(core)
    assert [f.name for f in summary.failed] == ["sandloom.biling"]
    core.app.state.module_summary = summary
    assert await _ready_status(core, monkeypatch) == 503


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


def test_a_task_without_celery_runs_inline_and_refuses_to_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decorated function keeps its name and body; every queue path refuses."""
    import sys

    from sandloom.sdk._prim.tasks import TaskQueueNotInstalled
    from sandloom.sdk.tasks import shared_task

    monkeypatch.setitem(sys.modules, "celery", None)

    @shared_task(name="probe.add", acks_late=True)
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @shared_task
    def bare() -> str:
        return "ran"

    assert add(2, 3) == 5
    assert add.name == "probe.add"
    assert add.__doc__ == "Add two numbers."
    assert bare() == "ran"
    assert bare.name == f"{__name__}.bare"
    enqueues: tuple[Callable[[], object], ...] = (
        lambda: add.delay(1, 2),
        lambda: add.apply_async((1, 2)),
        lambda: add.s(1, 2),
        lambda: add.si(1, 2),
    )
    for enqueue in enqueues:
        with pytest.raises(
            TaskQueueNotInstalled, match=r"'probe\.add'.*sandloom\[celery\]"
        ):
            enqueue()


def test_with_celery_installed_the_decorator_is_celerys() -> None:
    """Celery installed: the task is Celery's own (worker and beat unchanged)."""
    pytest.importorskip("celery")
    from celery.local import Proxy

    from sandloom.sdk.tasks import shared_task

    @shared_task(name="probe.with_celery")
    def probe() -> int:
        return 1

    assert isinstance(probe, Proxy)
    assert probe.name == "probe.with_celery"
