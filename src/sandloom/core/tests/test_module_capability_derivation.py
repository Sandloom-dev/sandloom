"""A module's infrastructure needs: declared, derived from its code, checked.

Owner decision on module infrastructure dependencies (2026-11-08), item 2:

* a module with a ``tasks`` submodule needs ``taskqueue``; one with a beat
  hook needs its feature ``periodic``, which only a driver that runs
  periodic jobs advertises (``celery``), never one that only enqueues;
* the kernel derives both at load
  (:meth:`ModuleDescriptor.effective_capabilities`); a declaration keeps its
  ``required`` / ``instance`` and only gains the derived feature;
* ``sandloom check`` asks for the declaration (SANDLOOM-C033), and the module
  snapshot records the effective requirements;
* a selected module whose required capability is unmet refuses startup even
  when it is optional;
* billing requires ``taskqueue`` + ``periodic``; identity declares it
  optional; identity and tenancy require ``notification`` + ``email``, so a
  production deployment on the log notifier refuses them.
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import sandloom.core.loader.plugin_loader as plugin_loader
from sandloom.core.archcheck.analyzers import parse_file
from sandloom.core.archcheck.module_rules import (
    _declared_capabilities,
    find_undeclared_capabilities,
)
from sandloom.core.cli.generated import module_surface
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.descriptors.derived_capabilities import DerivedNeed, derived_needs
from sandloom.core.drivers.celery_taskqueue import CELERY_TASKQUEUE_FEATURES
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.capabilities import TASKQUEUE_CAPABILITY, CapabilitySpec
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.infra.registry import FeatureUnsupportedError, InfraRegistry
from sandloom.core.loader.manifest import module_manifest
from sandloom.core.loader.plugin_loader import (
    ModuleLoadError,
    _preflight_capabilities,
    load_modules_with_summary,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.tasks.periodic import has_periodic_tasks

REPO = Path(__file__).resolve().parents[4]
BUNDLED = REPO / "src"

_HOOK = """
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(60.0, purge.s())
"""
_TASK = """
def purge():
    return 0
"""


def _package(
    root: Path, name: str, *, tasks: str | None = None, package_tasks: bool = False
) -> Path:
    """A throwaway importable package ``name`` under ``root``."""
    directory = root / name
    directory.mkdir()
    (directory / "__init__.py").write_text("")
    if tasks is not None:
        if package_tasks:
            (directory / "tasks").mkdir()
            (directory / "tasks" / "__init__.py").write_text(tasks)
        else:
            (directory / "tasks.py").write_text(tasks)
    return directory


@pytest.fixture
def importable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


# -- derivation ----------------------------------------------------------------


def test_tasks_derive_taskqueue_and_a_beat_hook_derives_periodic(
    importable: Path,
) -> None:
    _package(importable, "capmod_plain", tasks=_TASK)
    _package(importable, "capmod_beat", tasks=_TASK + _HOOK)
    _package(importable, "capmod_pkg", tasks=_TASK + _HOOK, package_tasks=True)
    _package(importable, "capmod_none")

    assert derived_needs("capmod_plain") == (
        DerivedNeed("taskqueue", frozenset(), "capmod_plain/tasks.py"),
    )
    assert derived_needs("capmod_beat") == (
        DerivedNeed(
            "taskqueue",
            frozenset({"periodic"}),
            "setup_periodic_tasks in capmod_beat/tasks.py",
        ),
    )
    assert derived_needs("capmod_pkg")[0].features == frozenset({"periodic"})
    assert derived_needs("capmod_none") == ()
    assert derived_needs("no_such_package.child") == ()
    # Read, not imported: the tasks module never ran.
    assert "capmod_beat.tasks" not in sys.modules
    assert has_periodic_tasks("capmod_beat") and not has_periodic_tasks("capmod_plain")


def test_an_undeclared_derived_capability_is_required(importable: Path) -> None:
    _package(importable, "capmod_undeclared", tasks=_TASK + _HOOK)
    # A hand-written (L2) descriptor gets the same as define_module.
    desc = ModuleDescriptor(name="capmod_undeclared", version="1", capabilities=[])
    assert desc.effective_capabilities() == [
        CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))
    ]
    assert desc.capabilities == []  # the declaration itself is untouched


def test_a_declaration_keeps_required_and_instance(importable: Path) -> None:
    _package(importable, "capmod_declared", tasks=_TASK + _HOOK)
    optional = CapabilityRequirement("taskqueue", required=False)
    desc = ModuleDescriptor(
        name="capmod_declared", version="1", capabilities=[optional]
    )
    (effective,) = desc.effective_capabilities()
    assert effective.required is False
    assert effective.features == frozenset({"periodic"})

    jobs = CapabilityRequirement("taskqueue", instance="jobs")
    desc = ModuleDescriptor(name="capmod_declared", version="1", capabilities=[jobs])
    (effective,) = desc.effective_capabilities()
    assert (effective.instance, effective.required) == ("jobs", True)
    assert effective.features == frozenset({"periodic"})


def test_bundled_modules_need_what_the_decision_says() -> None:
    """Billing requires a periodic driver; identity's cleanup is optional;
    identity and tenancy require email delivery."""
    if not (BUNDLED / "sandloom_billing").is_dir():
        pytest.skip("the bundled modules' sources are not in this tree")
    declared = {
        name: _declared_capabilities(
            {p: parse_file(p) for p in [BUNDLED / name / "__init__.py"]}
        )
        for name in ("sandloom_billing", "sandloom_identity", "sandloom_tenancy")
    }
    assert declared["sandloom_billing"] is not None
    assert declared["sandloom_billing"]["taskqueue"] == {"periodic"}
    assert declared["sandloom_identity"] is not None
    assert declared["sandloom_identity"]["taskqueue"] == {"periodic"}
    assert declared["sandloom_identity"]["notification"] == {"email"}
    assert declared["sandloom_tenancy"] is not None
    assert declared["sandloom_tenancy"]["notification"] == {"email"}
    source = (BUNDLED / "sandloom_identity" / "__init__.py").read_text()
    assert 'features=frozenset({"periodic"}), required=False' in source
    for name in ("sandloom_billing", "sandloom_identity", "sandloom_tenancy"):
        files = sorted(
            p
            for p in (BUNDLED / name).rglob("*.py")
            if "tests" not in p.parts and "migrations" not in p.parts
        )
        assert find_undeclared_capabilities(BUNDLED / name, files) == [], name


# -- the feature `periodic` ------------------------------------------------------


def test_periodic_is_a_taskqueue_feature_only_celery_advertises() -> None:
    assert "periodic" in TASKQUEUE_CAPABILITY.features
    assert "periodic" in CELERY_TASKQUEUE_FEATURES


class _EnqueueOnly(TaskQueueProvider):
    async def enqueue(self, task, payload, *, delay_seconds=None, idempotency_key=None):  # type: ignore[no-untyped-def]
        return "id"


def _core_with_taskqueue(features: frozenset[str]) -> CoreFacade:
    registry = InfraRegistry(
        config={
            "taskqueue": {"default": InstanceConfig("taskqueue", "default", "fake", {})}
        }
    )
    registry.register_capability(
        CapabilitySpec(
            name="taskqueue",
            version="v1",
            provider_contract=TaskQueueProvider,
            features=TASKQUEUE_CAPABILITY.features,
        )
    )
    registry.register_driver(
        "taskqueue",
        "fake",
        lambda cfg: _EnqueueOnly(name=cfg.name, driver=cfg.driver, options={}),
        features=features,
    )
    core = CoreFacade(app=FastAPI())
    core.set_infra(registry)
    return core


def test_an_enqueue_only_driver_cannot_serve_a_beat_hook(importable: Path) -> None:
    _package(importable, "capmod_needs_beat", tasks=_TASK + _HOOK)
    desc = ModuleDescriptor(name="capmod_needs_beat", version="1")
    with pytest.raises(FeatureUnsupportedError, match="periodic"):
        _preflight_capabilities(
            _core_with_taskqueue(frozenset({"delay", "idempotency_key"})), desc
        )
    _preflight_capabilities(
        _core_with_taskqueue(frozenset({"delay", "periodic"})), desc
    )
    # Declared optional: an enqueue-only driver is reported, not refused.
    desc.capabilities = [CapabilityRequirement("taskqueue", required=False)]
    _preflight_capabilities(_core_with_taskqueue(frozenset({"delay"})), desc)


# -- notification(email) under production ----------------------------------------


def test_the_log_notifier_refuses_mail_senders_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required ``notification`` + ``email`` (identity's, tenancy's) is not
    met by the log driver in production unless the risk is accepted."""
    from sandloom.core.drivers.bootstrap import register_builtin_drivers

    def core() -> CoreFacade:
        registry = InfraRegistry(strict_capabilities=True)
        register_builtin_drivers(registry, schema_registry=None)  # type: ignore[arg-type]
        log = InstanceConfig("notification", "default", "log", {})
        registry.set_config({"notification": {"default": log}})
        facade = CoreFacade(app=FastAPI())
        facade.set_infra(registry)
        return facade

    monkeypatch.setattr(type(settings), "is_prod", property(lambda self: True))
    monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
    sender = ModuleDescriptor(
        name="capmod_mailer",
        version="1",
        capabilities=[
            CapabilityRequirement("notification", features=frozenset({"email"}))
        ],
    )
    with pytest.raises(FeatureUnsupportedError, match="email"):
        _preflight_capabilities(core(), sender)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["log_notifier_in_prod"])
    _preflight_capabilities(core(), sender)


# -- the static check (SANDLOOM-C033) -------------------------------------------


def _module(root: Path, init: str, tasks: str | None) -> tuple[Path, list[Path]]:
    directory = root / "mod"
    directory.mkdir()
    (directory / "__init__.py").write_text(textwrap.dedent(init))
    if tasks is not None:
        (directory / "tasks.py").write_text(tasks)
    return directory, sorted(directory.rglob("*.py"))


_PERIODIC = (
    'capabilities=[CapabilityRequirement("taskqueue", '
    'features=frozenset({"periodic"}))]'
)


@pytest.mark.parametrize(
    ("declaration", "tasks", "found"),
    [
        ("", None, None),  # no tasks: nothing to declare
        ("", _TASK, "need the taskqueue capability"),
        ('capabilities=["database"]', _TASK, "need the taskqueue capability"),
        ('capabilities=["taskqueue"]', _TASK, None),
        ('capabilities=["taskqueue"]', _TASK + _HOOK, "feature 'periodic'"),
        ("", _TASK + _HOOK, "feature 'periodic'"),
        (_PERIODIC, _TASK + _HOOK, None),
        (
            'capabilities=[CapabilityRequirement("taskqueue", '
            'features=frozenset({"periodic"}), required=False)]',
            _TASK + _HOOK,
            None,
        ),
        # Not readable statically: decides nothing (C030 reports it).
        ("capabilities=CAPS_FROM_ELSEWHERE()", _TASK + _HOOK, None),
        ("capabilities=[make_requirement()]", _TASK, None),
    ],
)
def test_the_c033_analyzer(
    tmp_path: Path, declaration: str, tasks: str | None, found: str | None
) -> None:
    args = f"__name__, {declaration}" if declaration else "__name__"
    directory, files = _module(tmp_path, f"register = define_module({args})\n", tasks)
    hits = find_undeclared_capabilities(directory, files)
    if found is None:
        assert hits == []
        return
    ((path, message),) = hits
    assert path == directory / "tasks.py"
    assert found in message
    line = int(message.split(":", 1)[0].removeprefix("Line "))
    source = (directory / "tasks.py").read_text().splitlines()
    assert source[line - 1].startswith("def ")  # where a waiver can go


def test_c033_needs_a_declaration_call_to_decide(tmp_path: Path) -> None:
    directory, files = _module(tmp_path, "register = object()\n", _TASK)
    assert find_undeclared_capabilities(directory, files) == []


def test_c033_reads_a_hand_written_descriptor(tmp_path: Path) -> None:
    init = """
        def register(core):
            return ModuleDescriptor(name="mod", version="1", capabilities=[])
    """
    directory, files = _module(tmp_path, init, _TASK)
    ((_, message),) = find_undeclared_capabilities(directory, files)
    assert "taskqueue" in message


def test_c033_is_a_catalogued_check_rule() -> None:
    from sandloom.core import error_codes
    from sandloom.core.archcheck.runner import RULES

    entry = error_codes.lookup("undeclared-capability")
    assert entry is not None and entry.code == "SANDLOOM-C033"
    assert "undeclared-capability" in RULES


# -- the module snapshot -----------------------------------------------------------


def test_the_snapshot_records_the_effective_capabilities(importable: Path) -> None:
    _package(importable, "capmod_snap", tasks=_TASK + _HOOK)
    desc = ModuleDescriptor(
        name="capmod_snap",
        version="1",
        capabilities=[
            CapabilityRequirement("eventbus"),
            CapabilityRequirement("database"),
        ],
    )
    surface = module_surface(module_manifest(desc))
    assert surface["capabilities"] == [
        {
            "capability": "database",
            "instance": "default",
            "version": "v1",
            "features": [],
            "required": True,
        },
        {
            "capability": "eventbus",
            "instance": "default",
            "version": "v1",
            "features": [],
            "required": True,
        },
        {
            "capability": "taskqueue",
            "instance": "default",
            "version": "v1",
            "features": ["periodic"],
            "required": True,
        },
    ]


# -- a selected optional module ------------------------------------------------------

_PROBE = "capability_selected_probe"


def _install_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic optional module needing a feature no driver has."""
    module = types.ModuleType(_PROBE)

    def register(core: Any) -> ModuleDescriptor:
        return ModuleDescriptor(
            name=_PROBE,
            version="1",
            optional=True,
            capabilities=[
                CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))
            ],
        )

    module.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _PROBE, module)
    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    monkeypatch.setattr(settings, "module_fail_fast", False)


def _core() -> CoreFacade:
    core = _core_with_taskqueue(frozenset({"delay"}))
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


def test_a_selected_optional_module_with_an_unmet_capability_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_optional(monkeypatch)
    monkeypatch.setattr(settings, "sandloom_modules", [_PROBE])
    monkeypatch.setattr(settings, "module_required", [])
    with pytest.raises(ModuleLoadError) as refused:
        load_modules_with_summary(_core())
    (failed,) = refused.value.summary.failed
    assert failed.required and "periodic" in failed.error


def test_an_optional_module_the_deployment_did_not_select_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_optional(monkeypatch)
    defaults = [_PROBE]
    monkeypatch.setattr(plugin_loader, "_defaulted_modules", defaults)
    monkeypatch.setattr(settings, "sandloom_modules", defaults)
    monkeypatch.setattr(settings, "module_required", [])
    summary = load_modules_with_summary(_core())
    (failed,) = summary.failed
    assert not failed.required
    assert summary.loaded == []
