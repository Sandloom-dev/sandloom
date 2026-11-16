"""Hard ownership of test resources (``sandloom.core.testing.ownership``).

A run never resets a database / Redis index another run marked as its own:
the per-test reset refuses it, and the session refuses to start on it (no test
runs, nothing is truncated). The pre-push hook refuses the shared default
database unless ``SANDLOOM_TEST_DATABASE_URL`` names one explicitly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from sandloom.core.testing import ownership, workers

BACKEND = Path(__file__).resolve().parents[4]


def _test_url(var: str) -> str:
    values = workers.worker_env_values(BACKEND / ".env.test")
    return os.environ.get(var) or values.get(var) or ""


def _plain(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme="postgresql", path=f"/{name}", query=""))


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A fresh database next to the test database (dropped afterwards)."""
    psycopg2 = pytest.importorskip("psycopg2")
    base = _test_url("DATABASE_URL")
    if not base.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    base_name = urlsplit(base).path.lstrip("/")
    name = f"{base_name}_own_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg2.connect(_plain(base, "postgres"), connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield urlunsplit(urlsplit(base)._replace(path=f"/{name}"))
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _marker(url: str) -> str | None:
    import psycopg2  # type: ignore[import-untyped]

    conn = psycopg2.connect(_plain(url, urlsplit(url).path.lstrip("/")))
    try:
        with conn.cursor() as cur:
            return ownership.database_owner(cur)
    finally:
        conn.close()


def test_owner_is_the_explicit_id_else_the_machine_and_checkout_root(
    tmp_path: Path,
) -> None:
    assert ownership.current_owner({"SANDLOOM_TEST_OWNER": "agent-7"}) == "agent-7"
    checkout = tmp_path / "wt"
    (checkout / "src").mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: elsewhere\n")  # a worktree
    env = {"SANDLOOM_HOME": str(tmp_path / "home")}
    owner = ownership.current_owner(env, cwd=checkout / "src")
    machine = (tmp_path / "home" / "machine-id").read_text().strip()
    assert owner == f"{machine}:{checkout.resolve()}"


def test_a_legacy_bare_path_marker_is_still_ours(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A marker written before owners carried the machine id (the bare
    checkout path) belongs to the same checkout: claimed, not refused."""
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    legacy = str(tmp_path.resolve())
    ownership.claim_database(scratch_db, owner=legacy)
    ownership._claimed.clear()
    ownership.claim_database(scratch_db, owner=f"abcdef123456:{legacy}")
    assert _marker(scratch_db) == f"abcdef123456:{legacy}"
    assert ownership.same_owner(legacy, f"abcdef123456:{legacy}")
    assert not ownership.same_owner("/elsewhere", f"abcdef123456:{legacy}")
    assert not ownership.same_owner("run-a", "x:run-a")


def test_an_unmarked_database_is_claimed_then_refused_to_others(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    ownership.claim_database(scratch_db, owner="run-a")
    assert _marker(scratch_db) == "run-a"
    ownership.claim_database(scratch_db, owner="run-a")  # its owner: fine
    with pytest.raises(ownership.TestResourceOwnershipError) as refused:
        ownership.claim_database(scratch_db, owner="run-b")
    message = str(refused.value)
    assert "run-a" in message and "SANDLOOM_TEST_DATABASE_URL" in message
    assert ownership.TAKEOVER_ENV in message
    assert _marker(scratch_db) == "run-a"


def test_takeover_is_explicit(scratch_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ownership.claim_database(scratch_db, owner="run-a")
    monkeypatch.setenv(ownership.TAKEOVER_ENV, "1")
    with pytest.warns(UserWarning, match="taking over"):
        ownership.claim_database(scratch_db, owner="run-b")
    assert _marker(scratch_db) == "run-b"


def test_a_database_the_harness_creates_is_stamped_on_creation(
    scratch_db: str,
) -> None:
    import psycopg2

    name = f"{urlsplit(scratch_db).path.lstrip('/')}_new"
    url = urlunsplit(urlsplit(scratch_db)._replace(path=f"/{name}"))
    try:
        workers.ensure_database(url)
        assert _marker(url) == ownership.current_owner()
    finally:
        admin = psycopg2.connect(_plain(scratch_db, "postgres"))
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


async def test_the_per_test_reset_refuses_a_foreign_database(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.testing.database import open_test_engine

    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    monkeypatch.delenv("SANDLOOM_TEST_DATABASE_URL", raising=False)
    ownership.claim_database(scratch_db, owner="someone-else")
    with pytest.raises(ownership.TestResourceOwnershipError):
        async with open_test_engine(scratch_db):
            pass  # pragma: no cover - refused before the reset


# --------------------------------------------------------------------------- #
# What a harness family uses for a resource of its own (no server needed)
# --------------------------------------------------------------------------- #

_HINT = "export SANDLOOM_TEST_THING_URL=…"


def test_check_holder_lets_an_unmarked_or_own_resource_through() -> None:
    ownership.check_holder("thing", "t://h/1", None, "run-a", _HINT)
    ownership.check_holder("thing", "t://h/1", "run-a", "run-a", _HINT)


def test_check_holder_refuses_another_owner_naming_both_and_the_way_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ownership.TAKEOVER_ENV, raising=False)
    with pytest.raises(ownership.TestResourceOwnershipError) as refused:
        ownership.check_holder("thing", "t://h/1", "run-a", "run-b", _HINT)
    message = str(refused.value)
    assert "t://h/1" in message and "run-a" in message and "run-b" in message
    assert _HINT in message and ownership.TAKEOVER_ENV in message


def test_check_holder_takeover_is_explicit_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ownership.TAKEOVER_ENV, "1")
    with pytest.warns(UserWarning, match="taking over thing t://h/1"):
        ownership.check_holder("thing", "t://h/1", "run-a", "run-b", _HINT)


def test_a_recorded_claim_is_remembered_per_kind_resource_and_owner() -> None:
    assert not ownership.already_claimed("thing", "t://h/9", "run-a")
    ownership.record_claim("thing", "t://h/9", "run-a")
    try:
        assert ownership.already_claimed("thing", "t://h/9", "run-a")
        assert not ownership.already_claimed("thing", "t://h/9", "run-b")
        assert not ownership.already_claimed("other", "t://h/9", "run-a")
    finally:
        ownership._claimed.pop(("thing", "t://h/9"), None)


# --------------------------------------------------------------------------- #
# The whole session / the pre-push hook
# --------------------------------------------------------------------------- #


def test_a_run_on_a_foreign_database_aborts_before_any_test(
    scratch_db: str, tmp_path: Path
) -> None:
    """End to end: an app suite (the plugin) pointed at another run's
    database stops at session start — no test runs, nothing is truncated."""
    import psycopg2

    plain = _plain(scratch_db, urlsplit(scratch_db).path.lstrip("/"))
    conn = psycopg2.connect(plain)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE precious (v int)")
        cur.execute("INSERT INTO precious VALUES (42)")
    conn.close()
    ownership.claim_database(scratch_db, owner="the-other-agent")

    (tmp_path / ".env.test").write_text(
        f"ENVIRONMENT=test\nDATABASE_URL={scratch_db}\n"
        f"CACHE_URL={_test_url('CACHE_URL')}\nBROKER_URL={_test_url('BROKER_URL')}\n"
        "JWT_SECRET=test_secret_for_the_ownership_probe_only\n"
    )
    (tmp_path / "test_probe.py").write_text(
        textwrap.dedent(
            """
            def test_never_runs():
                raise AssertionError("ran on a foreign database")
            """
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_TEST_", "PYTEST_XDIST", "PYTEST_"))
    }
    env["SANDLOOM_TEST_OWNER"] = "this-run"
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
    assert "belongs to another test run" in output and "the-other-agent" in output
    assert "ran on a foreign database" not in output
    conn = psycopg2.connect(plain)
    with conn.cursor() as cur:
        cur.execute("SELECT v FROM precious")
        assert cur.fetchall() == [(42,)]
    conn.close()


@pytest.mark.subprocess_probe
def test_prepush_hook_refuses_the_shared_default_database() -> None:
    env = {k: v for k, v in os.environ.items() if k != "SANDLOOM_TEST_DATABASE_URL"}
    result = subprocess.run(
        ["bash", str(BACKEND / "tools" / "checks" / "prepush_full_tests.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "SANDLOOM_TEST_DATABASE_URL is not set" in result.stderr
    assert "export SANDLOOM_TEST_DATABASE_URL=" in result.stderr


@pytest.mark.subprocess_probe
@pytest.mark.parametrize("given", [None, "4242"])
def test_prepush_hook_runs_the_suite_in_one_printed_seeded_order(
    tmp_path: Path, given: str | None
) -> None:
    """Both runs of the hook (parallel, then the serial fallback after exit 4)
    take the same seed, and it is the one printed: a fresh random one, or the
    ``--sandloom-shuffle=<seed>`` a replay passes. ``docker`` and ``uv`` are
    stand-ins that record their arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "uv-calls.txt"
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$*" >> {calls}
            # the parallel run: the isolation guard refuses (exit 4)
            case "$*" in *" -n "*) exit 4;; esac
            exit 0
            """
        )
    )
    for tool in ("docker", "uv"):
        (bin_dir / tool).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SANDLOOM_TEST_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/own",
        "SANDLOOM_PREPUSH_WORKERS": "2",
    }
    result = subprocess.run(
        [
            "bash",
            str(BACKEND / "tools" / "checks" / "prepush_full_tests.sh"),
            *([] if given is None else [f"--sandloom-shuffle={given}"]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    printed = [
        line.rsplit(" ", 1)[-1]
        for line in result.stderr.splitlines()
        if line.startswith("backend-full-tests: test order seed ")
    ]
    assert len(printed) == 1 and printed[0].isdigit(), result.stderr
    if given is not None:
        assert printed == [given]
    runs = calls.read_text().splitlines()
    assert len(runs) == 2, runs  # parallel, then serial
    assert " -n 2 " in runs[0] and " -n " not in runs[1]
    assert all(f"--sandloom-shuffle={printed[0]}" in run.split() for run in runs)
