"""The harness restores process-global runtime state a test rebinds.

``create_app`` binds the app it builds as the process's active core, infra
registry and event bus and fills the settings' module lists. The harness's
autouse ``_sandloom_isolate_*`` fixtures (``sandloom.core.testing.isolation``) put them
back after the test, so the NEXT test still resolves ``db_session()`` /
``publish_event`` through the runtime it started with (billing's class-F
routes answered 500 when a previous test's app stayed bound). Every module
also STARTS from the harness app's runtime, so a test that resolves the
active runtime without asking for ``sandloom_app`` passes alone and in any order.

The "next test" cases run a real ``pytest`` subprocess on a tiny project in a
fixed order, so they hold under the shuffled isolation gate too.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from sandloom.core.testing import isolation

_ENV_TEST = (
    "ENVIRONMENT=test\nJWT_SECRET=isolation_probe_secret_value_long_enough_0\n"
    "DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/probe_test\n"
    "CACHE_URL=redis://127.0.0.1:1/0\nBROKER_URL=redis://127.0.0.1:1/1\n"
)
_RUNTIME = """
def runtime():
    from sandloom.core import runtime
    from sandloom.core.eventing import event_bus
    from sandloom.core.infra import active

    return runtime._process_core, active._active_infra, event_bus.event_bus
"""


def _run_project(root: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        'addopts = ["-p", "sandloom.core.testing.plugin", "-p", "no:cacheprovider"]\n'
        'asyncio_mode = "auto"\n'
    )
    (root / ".env.test").write_text(_ENV_TEST)
    (root / "probe.py").write_text(_RUNTIME)
    for rel, body in files.items():
        (root / rel).write_text(textwrap.dedent(body))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "PYTEST_", "JWT_")) and k != "ENVIRONMENT"
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:randomly"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_step_1_a_test_builds_its_own_app(tmp_path: Path) -> None:
    """``create_app`` in a test really rebinds the process (what used to leak),
    and the harness hands the next test its runtime back."""
    proc = _run_project(
        tmp_path / "app",
        {
            "test_steps.py": """
                from sandloom.core.config.settings import settings
                from probe import runtime

                seen = {}


                def test_1_builds_an_app():
                    from sandloom.core.app_factory import create_app

                    seen["before"] = runtime()
                    seen["modules"] = (settings.sandloom_modules, list(settings.sandloom_modules))
                    core = create_app().state.core
                    seen["app"] = (core, core.infra, core.event_bus)
                    assert runtime() == seen["app"]


                def test_2_the_next_test_gets_the_runtime_back():
                    for now, leaked, before in zip(runtime(), seen["app"], seen["before"]):
                        assert now is not leaked and now is before
                    modules, content = seen["modules"]
                    assert settings.sandloom_modules is modules and settings.sandloom_modules == content
            """,
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def test_step_2_the_next_test_gets_the_previous_runtime_back(tmp_path: Path) -> None:
    """Every module starts from the harness app's runtime: alone, first, or
    after a module whose own module-scoped app was bound."""
    proc = _run_project(
        tmp_path / "app",
        {
            "test_a_alone.py": """
                from sandloom.core.infra.active import get_active_infra


                def test_the_runtime_is_bound_without_asking_for_sandloom_app():
                    # no sandloom_app / client fixture: the harness app is bound anyway
                    get_active_infra()
            """,
            "test_b_own_app.py": """
                import pytest
                from probe import runtime


                @pytest.fixture(scope="module")
                def own_app():
                    from sandloom.core.app_factory import create_app

                    return create_app()


                def test_the_module_sees_its_own_app(own_app):
                    assert runtime()[0] is own_app.state.core
            """,
            "test_c_after.py": """
                from probe import runtime


                def test_the_next_module_gets_the_harness_app(sandloom_app):
                    assert runtime()[0] is sandloom_app.state.core
            """,
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 passed" in proc.stdout


def test_a_registered_channel_is_gone_after_the_test(tmp_path: Path) -> None:
    """A probe channel one test registers on the harness app's schema
    registry is not there for the next test."""
    proc = _run_project(
        tmp_path / "app",
        {
            "test_schemas.py": """
                CHANNEL = "probe.thing.happened.v1"
                SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}


                def test_1_registers_a_channel(sandloom_core):
                    registry = sandloom_core.get_schema_registry()
                    assert registry.get(CHANNEL) is None
                    registry.register(CHANNEL, "probe", SCHEMA)
                    assert registry.get(CHANNEL) is not None


                def test_2_the_next_test_does_not_see_it(sandloom_core):
                    assert sandloom_core.get_schema_registry().get(CHANNEL) is None
            """,
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def test_restore_puts_back_identity_and_content() -> None:
    from sandloom.core import runtime
    from sandloom.core.config.settings import settings

    snapshot = isolation.capture()
    original_core = runtime._process_core
    original_modules = settings.sandloom_modules
    try:
        runtime._process_core = object()  # type: ignore[assignment]
        settings.sandloom_modules = ["sandloom.somewhere"]
        changed = isolation.restore(snapshot)
        assert set(changed) == {
            "sandloom.core.runtime._process_core",
            "settings.sandloom_modules",
        }
        assert runtime._process_core is original_core
        assert settings.sandloom_modules is original_modules
        # An in-place mutation of the very list is undone too.
        before = list(original_modules)
        original_modules.append("sandloom.mutated")
        assert isolation.restore(snapshot) == ["settings.sandloom_modules"]
        assert settings.sandloom_modules == before
        assert isolation.restore(snapshot) == []
    finally:
        runtime._process_core = original_core
        settings.sandloom_modules = original_modules


def test_class_flags_and_process_caches_are_put_back() -> None:
    """``event.remove`` leaves ``Engine._has_events`` set (every later engine
    would leave the event-free fast path) and the RPC client keeps described
    contracts process-wide (a method's first call is treated as a write until
    it is described): the harness undoes both after every test."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    import sandloom.core.rpc.proxy as rpc_proxy

    snapshot = isolation.capture()
    had_events = Engine.__dict__.get("_has_events")

    def listener(*_: Any) -> None:
        return None

    event.listen(Engine, "before_cursor_execute", listener)
    event.remove(Engine, "before_cursor_execute", listener)
    assert Engine._has_events is True  # what used to leak
    assert "sqlalchemy.engine.base.Engine._has_events" in isolation.restore(snapshot)
    assert Engine.__dict__.get("_has_events") == had_events

    rpc_proxy._describe_failed[("http://probe", "probe.v1")] = ("h", 0.0)
    isolation.clear_process_caches()
    assert not rpc_proxy._remote_contracts and not rpc_proxy._describe_failed
