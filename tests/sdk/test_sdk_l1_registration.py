"""``test_l1_equals_l2_registration`` (spec §3, ledger G-SDK-03).

For a fixture module per L1 feature and for every example module, the
descriptor and the recorded registration calls (``registration_surface``,
the ``ModuleRegistrationDraft`` calls) built through L1 ``define_module``
equal those of a hand-written L2 ``register``. Routes are compared with their
resolved per-route dependencies too (``module_router`` injection).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.testing import registration_surface
from sandloom.sdk.module import ContractRequirement, ModuleAPI, ModuleDescriptor
from sandloom.sdk.web import TenantScope

BACKEND = Path(__file__).resolve().parents[2]
EXAMPLES = BACKEND / "examples"


def _route_policy(router: Any) -> list[tuple[str, str, tuple[str, ...]]]:
    out = []
    for route in router.routes:
        names = []
        for dep in route.dependencies:
            call = dep.dependency
            binds = getattr(call, "__sandloom_binds_tenant__", False)
            names.append("TenantScope" if binds else call.__name__)
        out.append((route.path, ",".join(sorted(route.methods)), tuple(names)))
    return sorted(out)


def _l2_consumer(handler: Any, event: Any) -> Any:
    """The L2 adapter of spec §3: decode the payload into the event, call the
    handler; named after the handler."""

    async def callback(session: Any, envelope: dict[str, Any]) -> None:
        fields = event.model_fields
        payload = envelope["payload"]
        await handler(
            event.model_validate({k: payload[k] for k in payload if k in fields})
        )

    callback.__module__ = handler.__module__
    callback.__qualname__ = handler.__qualname__
    return callback


def _notes_l2(core: ModuleAPI) -> ModuleDescriptor:
    """``sdkfix_notes`` written by hand against the L2 primitives."""
    from sdkfix_notes import api, consumers, contracts, events, projections, sagas

    core.include_router(api.router)
    for event in (
        events.NoteCreated,
        events.NoteSaved,
        events.NoteDeleted,
        events.NoteLiked,
    ):
        core.register_event_schema(event.channel, event.__name__, event.wire_schema())
    core.register_event_consumer(
        "sdkfix_stats.counter.bumped.v1",
        _l2_consumer(consumers.remember_total, consumers.CounterBumped),
        name="remember_total",
        durable=True,
        database="default",
        required=True,
        group=None,
        start_from="earliest",
        bind_tenant_from="tenant_id",
        dead_letter=True,
    )
    core.register_event_consumer(
        "sdkfix_notes.note.created.v1",
        _l2_consumer(consumers.audit_note, events.NoteCreated),
        name="notes-audit",
        durable=False,
        database="default",
        required=True,
        group=None,
        start_from="earliest",
        bind_tenant_from=None,
        dead_letter=False,
    )
    core.register_service("sdkfix_notes.lookup.v1", contracts.Lookup, database=None)
    core.register_saga(sagas.RESERVE)
    projection = projections.NOTE_INDEX
    core.register_query_projection(projection.descriptor("sdkfix_notes"))
    for consumer in projection.consumers():
        core.register_projection_consumer(consumer)
    return ModuleDescriptor(
        name="sdkfix_notes",
        version="0.2.0",
        consumes=[ContractRequirement("sdkfix_stats.quota.v1", required=True)],
    )


def _stats_l2(core: ModuleAPI) -> ModuleDescriptor:
    from sdkfix_stats import consumers, contracts, events

    core.register_event_schema(
        events.CounterBumped.channel,
        "CounterBumped",
        events.CounterBumped.wire_schema(),
    )
    core.register_event_consumer(
        "sdkfix_notes.note.created.v1",
        _l2_consumer(consumers.count_note, consumers.NoteCreated),
        name="count_note",
        durable=True,
        database="default",
        required=True,
        group=None,
        start_from="earliest",
        bind_tenant_from="tenant_id",
        dead_letter=True,
    )
    core.register_service("sdkfix_stats.quota.v1", contracts.Quota, database=None)
    return ModuleDescriptor(name="sdkfix_stats", version="0.1.0")


@pytest.mark.parametrize(
    ("package", "l2"), [("sdkfix_notes", _notes_l2), ("sdkfix_stats", _stats_l2)]
)
def test_l1_equals_l2_registration(package: str, l2: Any) -> None:
    module = __import__(package)
    l1 = registration_surface(module.register)
    hand = registration_surface(l2)
    assert sorted(l1.calls) == sorted(hand.calls)
    assert l1.descriptor == hand.descriptor


def test_module_router_routes_equal_the_l2_router() -> None:
    """The L1 router's per-route dependencies are exactly the §3 L2 form."""
    from sdkfix_notes import api

    assert _route_policy(api.router) == sorted(
        [
            ("/api/v1/sdkfix_notes/", "POST", ("current_context", "TenantScope")),
            ("/api/v1/sdkfix_notes/health", "GET", ("public",)),
            ("/api/v1/sdkfix_notes/settings", "GET", ("current_context",)),
        ]
    )
    binders = [
        d.dependency
        for d in api.router.routes[0].dependencies
        if getattr(d.dependency, "__sandloom_binds_tenant__", False)
    ]
    assert len(binders) == 1
    assert TenantScope(required=True).dependency.__qualname__ == binders[0].__qualname__


_EXAMPLE_PROBE = r"""
import importlib, inspect, json, pkgutil, sys
from sandloom.core.testing import registration_surface
from sandloom.sdk.module import define_module

app, modules_package = sys.argv[1], sys.argv[2]
pkg = importlib.import_module(modules_package)
out = {}
for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
    name = f"{modules_package}.{info.name}"
    module = importlib.import_module(name)
    register = module.register
    closure = inspect.getclosurevars(register).nonlocals
    keys = ("version", "optional", "consumes", "capabilities", "depends_on",
            "migrations_database", "migrations_chain", "health_check", "setup",
            "router", "events", "issues_tokens")
    kwargs = {k: closure[k] for k in keys if k in closure}
    if "descriptor_name" in closure and closure["descriptor_name"] != closure.get("pkg"):
        kwargs["name"] = closure["descriptor_name"]
    l2 = registration_surface(register)
    l1 = registration_surface(define_module(closure["pkg"], **kwargs))
    out[name] = {
        "equal_calls": sorted(l1.calls) == sorted(l2.calls),
        "equal_descriptor": l1.descriptor == l2.descriptor,
        "l1": sorted(map(list, l1.calls)), "l2": sorted(map(list, l2.calls)),
    }
print("RESULT " + json.dumps(out))
"""


def _example_apps() -> list[tuple[str, str]]:
    apps = []
    for rules in sorted(EXAMPLES.glob("*/architecture_rules.yml")):
        for line in rules.read_text(encoding="utf-8").splitlines():
            if line.startswith("modules_package:"):
                apps.append((rules.parent.name, line.split(":", 1)[1].strip()))
    return apps


@pytest.mark.parametrize(("app", "modules_package"), _example_apps())
def test_example_modules_register_identically_through_l1(
    app: str, modules_package: str
) -> None:
    """Every example module: L1 discovery with the module's own arguments
    registers exactly what its current ``register`` does (run in a
    subprocess so the examples' models never join this process's metadata)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(EXAMPLES / app), str(BACKEND / "src"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", _EXAMPLE_PROBE, app, modules_package],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stderr[-4000:]
    results = json.loads(lines[-1][len("RESULT ") :])
    assert results, f"{app}: no modules found under {modules_package}"
    for name, result in results.items():
        assert result["equal_calls"], (name, result["l1"], result["l2"])
        assert result["equal_descriptor"], name
