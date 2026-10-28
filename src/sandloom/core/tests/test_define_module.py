"""``define_module``: a module's register(core) from declarations + conventions."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandloom.core.descriptors import (
    CapabilityRequirement,
    ContractRequirement,
    ModuleDescriptor,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleAPI, define_module

_SCHEMA = {
    "type": "object",
    "properties": {"gizmo_id": {"type": "string"}},
    "required": ["gizmo_id"],
}


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


@pytest.fixture
def make_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Factory writing a throwaway module package importable as ``name``."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created: list[str] = []

    def _make(name: str, files: dict[str, str]) -> str:
        for rel, body in files.items():
            _write(tmp_path / name, rel, body)
        created.append(name)
        return name

    yield _make
    for mod in [m for m in sys.modules if m.split(".")[0] in created]:
        del sys.modules[mod]


_API = """
    from fastapi import APIRouter

    gizmos_router = APIRouter(prefix="/api/v1/gizmos")

    @gizmos_router.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    __all__ = ["gizmos_router"]
"""
_EVENTS = f"""
    EVENT_SCHEMAS = {{"gizmos.created.v1": {_SCHEMA!r}}}
"""


def _routes(core: CoreFacade) -> set[str]:
    return {getattr(r, "path", "") for r in core.app.routes}


def test_conventions_mount_router_and_register_schemas(
    make_pkg: object, core_facade: CoreFacade
) -> None:
    pkg = make_pkg(  # type: ignore[operator]
        "gizmos_a",
        {
            "__init__.py": "",
            "api/__init__.py": _API,
            "events.py": _EVENTS,
        },
    )
    register = define_module(pkg, version="1.2.3", consumes=["tenancy.membership.v1"])
    desc = register(ModuleAPI(core_facade))

    assert isinstance(desc, ModuleDescriptor)
    assert (desc.name, desc.version, desc.optional) == ("gizmos_a", "1.2.3", False)
    assert desc.consumes == [ContractRequirement("tenancy.membership.v1")]
    assert "/api/v1/gizmos/ping" in _routes(core_facade)
    entry = core_facade.get_event_schema("gizmos.created.v1")
    assert entry is not None and entry.schema == _SCHEMA


def test_frame_inference_and_setup_hook(
    make_pkg: object, core_facade: CoreFacade
) -> None:
    make_pkg(  # type: ignore[operator]
        "gizmos_b",
        {
            "__init__.py": """
                from sandloom.core.module_api import define_module

                CALLS = []

                def _setup(core):
                    CALLS.append(core)
                    core.register_service("gizmos_b.read.v1", object)

                register = define_module(
                    version="0.2.0", setup=_setup, capabilities=["cache"]
                )
            """,
            "api.py": """
                from fastapi import APIRouter
                router = APIRouter(prefix="/b")
                other = APIRouter(prefix="/ignored")
            """,
        },
    )
    import gizmos_b  # type: ignore[import-not-found]

    api = ModuleAPI(core_facade)
    desc = gizmos_b.register(api)
    assert desc.name == "gizmos_b"  # inferred from the caller's __name__
    assert desc.capabilities == [CapabilityRequirement("cache")]
    assert gizmos_b.CALLS == [api]
    assert core_facade.service_registry.contains("gizmos_b.read.v1")


def test_module_without_api_or_events(
    make_pkg: object, core_facade: CoreFacade
) -> None:
    pkg = make_pkg("gizmos_c", {"__init__.py": ""})  # type: ignore[operator]
    desc = define_module(pkg, name="acme.gizmos", optional=True)(ModuleAPI(core_facade))
    assert (desc.name, desc.optional) == ("acme.gizmos", True)


def test_explicit_router_and_events_override_discovery(
    make_pkg: object, core_facade: CoreFacade
) -> None:
    from fastapi import APIRouter

    pkg = make_pkg(  # type: ignore[operator]
        "gizmos_d",
        {"__init__.py": "", "api/__init__.py": _API, "events.py": _EVENTS},
    )
    explicit = APIRouter(prefix="/explicit")

    @explicit.get("/x")
    async def x() -> None:
        return None

    define_module(pkg, router=explicit, events=None)(ModuleAPI(core_facade))
    assert "/explicit/x" in _routes(core_facade)
    assert "/api/v1/gizmos/ping" not in _routes(core_facade)
    assert core_facade.get_event_schema("gizmos.created.v1") is None


def test_identical_schema_reregistration_is_idempotent_breaking_still_raises(
    make_pkg: object, core_facade: CoreFacade
) -> None:
    """No try/except around schema registration is needed any more."""
    from sandloom.core.module_api import ModuleRegistrationConflict

    pkg = make_pkg(  # type: ignore[operator]
        "gizmos_e", {"__init__.py": "", "events.py": _EVENTS}
    )
    register = define_module(pkg)
    with core_facade.module_registration("gizmos_e") as api:
        register(api)
    # A second module (or a reload) re-registering the identical schema.
    with core_facade.module_registration("gizmos_e2") as api:
        register(api)
    assert core_facade.get_event_schema("gizmos.created.v1") is not None

    breaking = {"type": "object", "properties": {}, "required": ["other"]}
    with pytest.raises(ModuleRegistrationConflict, match="breaking"):
        with core_facade.module_registration("gizmos_e3") as api:
            define_module(pkg, events={"gizmos.created.v1": breaking})(api)


def test_inference_refuses_main() -> None:
    namespace = {"__name__": "__main__", "define_module": define_module}
    with pytest.raises(TypeError, match="__name__"):
        exec("define_module()", namespace)  # noqa: S102 - literal code simulating a __main__ caller
