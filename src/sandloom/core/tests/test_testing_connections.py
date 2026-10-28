"""The connection budget of a test run (``sandloom.core.testing.connections``).

A test process gets small pools; a run (x xdist workers) is checked against
the server's free connections before any test and fails fast with a clear
message instead of "too many clients" mid-run; ``-n auto`` is capped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sandloom.core.testing import connections, ownership, workers

BACKEND = Path(__file__).resolve().parents[4]


def _test_url(var: str) -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    return os.environ.get(var) or values.get(var) or ""


def _database_url() -> str:
    url = _test_url("DATABASE_URL")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    if connections.server_headroom(url) is None:
        pytest.skip("PostgreSQL unreachable")
    return url


def test_mirrored_defaults_are_the_settings_defaults() -> None:
    from sandloom.core.config.settings import Settings

    fields = {f.alias: f for f in Settings.model_fields.values() if f.alias}
    for env, default in connections.SETTINGS_DEFAULTS.items():
        assert fields[env].default == default, env


def test_test_profile_is_a_default_an_explicit_value_wins() -> None:
    env = {"DB_POOL_SIZE": "7"}
    applied = connections.apply_test_pool_profile(env)
    assert env["DB_POOL_SIZE"] == "7" and "DB_POOL_SIZE" not in applied
    assert env["DB_MAX_OVERFLOW"] == connections.TEST_POOL_PROFILE["DB_MAX_OVERFLOW"]
    assert env["DB_POOL_OVERFLOW_LINGER_SECONDS"] == "0"


def test_a_test_process_needs_a_small_fixed_number_of_connections() -> None:
    profile = dict(connections.TEST_POOL_PROFILE)
    # request 2+3, contract 1+1, harness engine 2+2, ad-hoc 2
    assert connections.process_connection_need(profile) == 13
    # Production defaults would be 10+20 + 5+5 per process.
    assert connections.process_connection_need({}) == 46
    # Four workers (CI's -n auto) + one multi-process suite fit a stock
    # server (max_connections 100, 3 superuser-reserved).
    assert 4 * 13 + connections.SUBPROCESS_RESERVE <= 97


def test_this_process_runs_with_the_test_profile() -> None:
    from sandloom.core.config.settings import settings

    assert settings.db_pool_size + settings.db_max_overflow <= 5
    assert settings.contract_db_pool_size + settings.contract_db_max_overflow <= 2


def test_insufficient_headroom_fails_fast_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _database_url()
    monkeypatch.delenv(connections.SKIP_ENV, raising=False)
    room = connections.check_headroom(url, 1, "a tiny run")
    assert room is not None and room.free >= 1
    with pytest.raises(connections.ConnectionBudgetError) as refused:
        connections.check_headroom(url, 100_000, "a huge run")
    message = str(refused.value)
    assert "a huge run needs up to 100000" in message
    assert f"max_connections {room.max_connections}" in message
    assert connections.SKIP_ENV in message
    monkeypatch.setenv(connections.SKIP_ENV, "1")
    assert connections.check_headroom(url, 100_000, "a huge run") is None


def test_auto_workers_are_capped_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _database_url()
    room = connections.server_headroom(url)
    assert room is not None
    # Parallel workers open and close connections between two live probes;
    # pin the one snapshot so the cap and the bound see the same headroom.
    monkeypatch.setattr(connections, "server_headroom", lambda _url: room)
    capped = connections.affordable_workers(512, url, connections.TEST_POOL_PROFILE)
    assert 1 <= capped < 512
    assert capped <= max(1, (room.free - connections.SUBPROCESS_RESERVE) // 13)
    assert connections.affordable_workers(2, "", connections.TEST_POOL_PROFILE) == 2


def test_a_run_without_room_aborts_before_any_test(tmp_path: Path) -> None:
    """End to end: an app suite whose pools cannot fit stops at session start
    with the budget message, before any test runs."""
    url = _database_url()
    (tmp_path / ".env.test").write_text(
        f"ENVIRONMENT=test\nDATABASE_URL={url}\n"
        f"CACHE_URL={_test_url('CACHE_URL')}\nBROKER_URL={_test_url('BROKER_URL')}\n"
        "JWT_SECRET=test_secret_for_the_budget_probe_only\n"
        "DB_POOL_SIZE=100000\n"
    )
    (tmp_path / "test_probe.py").write_text(
        textwrap.dedent(
            """
            def test_never_runs():
                raise AssertionError("ran without connection room")
            """
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_TEST_", "PYTEST_", "DB_", "CONTRACT_DB_"))
        and k != connections.SKIP_ENV
    }
    env[ownership.OWNER_ENV] = ownership.current_owner()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "sandloom.core.testing.plugin",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-cov",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "PostgreSQL connections" in output and "free" in output
    assert "ran without connection room" not in output
