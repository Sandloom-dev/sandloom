"""``sandloom dev`` without Docker: identity, state, locks, ports, the ``.env``
block, refusals, CI and the one-line connection error.

The run against real containers (two checkouts, ``dev up`` from both at
once) is the end-to-end probe recorded on the dev-command task.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sandloom.core import error_codes
from sandloom.core.cli import dev as dev_cli
from sandloom.core.cli.dev import (
    commands,
    docker,
    envfile,
    errors,
    identity,
    locks,
    ports,
)
from sandloom.core.cli.dev.state import (
    CheckoutRow,
    State,
    load_registry,
    load_state,
    save_registry,
    save_state,
)
from sandloom.core.cli.project import generate_project
from sandloom.core.testing.machine import machine_id

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing here may touch the real ``~/.sandloom``."""
    monkeypatch.setenv("SANDLOOM_HOME", str(tmp_path / "home"))


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    """The process environment with a private ``~/.sandloom``, not in CI."""
    env = {k: v for k, v in os.environ.items() if k not in {"CI", "DOCKER_HOST"}}
    env["SANDLOOM_HOME"] = str(tmp_path / "home")
    return env


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository holding a minimal app, with one commit."""
    root = tmp_path / "shop"
    (root / "deploy").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    (root / ".env.example").write_text(
        "ENVIRONMENT=dev\nJWT_SECRET=change_me\n" + envfile.empty_block()
    )
    (root / ".gitignore").write_text(".env\n.env.test.local\n.sandloom/\n")
    (root / "deploy" / "docker-compose.dev.yml").write_text("services: {}\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "init",
    )
    return root


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "shop-wt"
    _git(repo, "worktree", "add", "-q", str(path), "-b", "feature")
    return path


def _ctx(root: Path, environ: dict[str, str]) -> commands.Context:
    return commands.Context(
        identity=identity.resolve(root, environ),
        environ=environ,
        out=io.StringIO(),
        err=io.StringIO(),
    )


def _text(stream: Any) -> str:
    return str(stream.getvalue())


def _state(ctx: commands.Context) -> State:
    return State(
        repository=str(ctx.identity.repository),
        project=ctx.identity.project,
        machine=ctx.identity.machine,
        ports={
            "db": 24001,
            "redis": 24002,
            "postgres_test": 24003,
            "redis_test": 24004,
        },
    )


# --------------------------------------------------------------------------- #
# Identity and state
# --------------------------------------------------------------------------- #


def test_a_worktree_shares_the_repository_state_but_not_the_database(
    repo: Path, worktree: Path, environ: dict[str, str]
) -> None:
    main = identity.resolve(repo, environ)
    wt = identity.resolve(worktree / "deploy", environ)
    assert main.repository == wt.repository
    assert main.state_dir == wt.state_dir == main.repository / "sandloom"
    assert main.project == wt.project and main.project.startswith("shop-")
    assert main.main and not wt.main
    assert main.database == "shop"
    assert wt.database == f"shop_{wt.checkout_hash}"
    assert main.test_database != wt.test_database
    assert wt.root == worktree.resolve()


def test_git_dir_from_a_hook_is_ignored(
    repo: Path, worktree: Path, environ: dict[str, str]
) -> None:
    hooked = {**environ, "GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(repo)}
    assert identity.resolve(worktree, hooked).checkout == worktree.resolve()


def test_outside_git_the_state_lives_in_the_project(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "plain-app"\n')
    found = identity.resolve(root, environ)
    assert not found.git and found.main
    assert found.state_dir == root.resolve() / ".sandloom"
    assert found.app == "plain_app" and found.project.startswith("plain-app-")


def test_the_machine_id_is_created_once(environ: dict[str, str]) -> None:
    first = machine_id(environ)
    assert first == machine_id(environ)
    assert len(first) == 12
    assert (Path(environ["SANDLOOM_HOME"]) / "machine-id").is_file()


def _first_machine_id(home: str, out: str) -> None:
    Path(out).write_text(machine_id({"SANDLOOM_HOME": home}))


@pytest.mark.parametrize("broken", [None, ""])
def test_concurrent_first_calls_agree_on_the_machine_id(
    tmp_path: Path, broken: str | None
) -> None:
    """On a new machine, and when an empty file is repaired concurrently."""
    home = tmp_path / "fresh-home"
    if broken is not None:
        home.mkdir()
        (home / "machine-id").write_text(broken)
    spawn = multiprocessing.get_context("spawn")
    outs = [tmp_path / f"id-{n}" for n in range(6)]
    procs = [
        spawn.Process(target=_first_machine_id, args=(str(home), str(out)))
        for out in outs
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
    ids = {out.read_text() for out in outs}
    assert len(ids) == 1
    assert (home / "machine-id").read_text().strip() == ids.pop()
    assert not [p for p in home.iterdir() if p.name.startswith(".machine-id.")]


@pytest.mark.parametrize("broken", ["", "\n", "not-an-id\n"])
def test_an_empty_or_garbage_machine_id_is_repaired_once(
    environ: dict[str, str], broken: str
) -> None:
    home = Path(environ["SANDLOOM_HOME"])
    home.mkdir(parents=True)
    (home / "machine-id").write_text(broken)
    first = machine_id(environ)
    assert len(first) == 12
    assert (home / "machine-id").read_text().strip() == first
    assert machine_id(environ) == first


def test_the_state_round_trips(tmp_path: Path) -> None:
    state = State(repository="/r", project="p-1", machine="m", ports={"db": 1})
    state.checkouts["/r/a"] = CheckoutRow("/r/a", "p", 1, 2, "sandloom_test_x", 5.0)
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    save_registry(tmp_path, {24001: "/r"})
    assert load_registry(tmp_path) == {24001: "/r"}


def test_redis_indexes_are_allocated_per_checkout() -> None:
    state = State(repository="/r", project="p", machine="m")
    assert commands.allocate_indexes(state, "/r", main=True) == (1, 2)
    state.checkouts["/r"] = CheckoutRow("/r", "p", 1, 2, "t")
    assert commands.allocate_indexes(state, "/w", main=False) == (3, 4)
    assert commands.allocate_indexes(state, "/r", main=True) == (1, 2)


def test_a_copied_repository_gets_new_services(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, environ)
    copied = _state(ctx)
    copied.repository = str(repo.parent)  # exists: a `cp -r` source
    copied.project = "shop-000000"
    found = commands.reconcile(ctx, copied, commands.UpOptions())
    assert found.project == ctx.identity.project and not found.ports


def test_two_homes_on_one_repository_keep_their_own_state(
    repo: Path, environ: dict[str, str], tmp_path: Path
) -> None:
    """A devcontainer or a sandboxed agent with its own HOME (another machine
    id) on the same repository gets its own project and rows; the host's
    state is neither replaced nor orphaned."""
    host = _ctx(repo, environ)
    host_state = _state(host)
    host_state.checkouts[str(repo)] = CheckoutRow(str(repo), "shop", 1, 2, "t")
    save_state(host.state_dir, host_state)

    sandbox_env = {**environ, "SANDLOOM_HOME": str(tmp_path / "sandbox-home")}
    sandbox = _ctx(repo, sandbox_env)
    assert sandbox.identity.machine != host.identity.machine
    assert sandbox.state_dir != host.state_dir
    found = commands.reconcile(
        sandbox, load_state(sandbox.state_dir), commands.UpOptions()
    )
    assert found.project == sandbox.identity.project != host_state.project
    assert not found.checkouts
    save_state(sandbox.state_dir, found)
    assert load_state(host.state_dir) == host_state
    again = commands.reconcile(host, load_state(host.state_dir), commands.UpOptions())
    assert again == host_state


def test_an_adopted_move_registers_the_ports_under_the_new_path(
    repo: Path, environ: dict[str, str], tmp_path: Path
) -> None:
    ctx = _ctx(repo, environ)
    gone = str(tmp_path / "old-place" / ".git")
    moved = _state(ctx)
    moved.repository = gone
    save_registry(ctx.home, dict.fromkeys(moved.ports.values(), gone) | {25000: "/x"})
    found = commands.reconcile(ctx, moved, commands.UpOptions(adopt=True))
    assert found.repository == str(ctx.identity.repository)
    assert commands.registry_stale(ctx, found)
    commands.reregister_ports(ctx, found)
    registry = load_registry(ctx.home)
    assert {registry[p] for p in found.ports.values()} == {found.repository}
    assert registry[25000] == "/x"
    assert not commands.registry_stale(ctx, found)
    # `dev prune` keeps them now.
    commands.prune(ctx)
    assert set(found.ports.values()) <= set(load_registry(ctx.home))


def test_up_reregisters_the_ports_after_an_adopted_move(
    repo: Path,
    environ: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(repo, environ)
    gone = str(tmp_path / "old-place" / ".git")
    moved = _state(ctx)
    moved.repository = gone
    save_state(ctx.state_dir, moved)
    save_registry(ctx.home, dict.fromkeys(moved.ports.values(), gone))
    monkeypatch.setattr(commands._docker, "check_docker", lambda env: None)
    monkeypatch.setattr(commands, "ports_needed", lambda ctx, state, **kw: [])
    monkeypatch.setattr(commands, "_finish_up", lambda ctx, state, options: 0)
    assert commands.up(ctx, commands.UpOptions(adopt=True, pull=False)) == 0
    registry = load_registry(ctx.home)
    assert {registry[p] for p in moved.ports.values()} == {str(ctx.identity.repository)}
    assert load_state(ctx.state_dir).repository == str(ctx.identity.repository)


# --------------------------------------------------------------------------- #
# Locks
# --------------------------------------------------------------------------- #


def _hold(path: str, log: str, label: str, seconds: float) -> None:
    with locks.FileLock(Path(path), out=io.StringIO(), poll=0.05):
        with open(log, "a") as handle:
            handle.write(f"{label} start\n")
        time.sleep(seconds)
        with open(log, "a") as handle:
            handle.write(f"{label} end\n")


def test_two_processes_are_serialised_by_the_lock(tmp_path: Path) -> None:
    lock, log = tmp_path / "dev.lock", tmp_path / "log"
    spawn = multiprocessing.get_context("spawn")
    first = spawn.Process(target=_hold, args=(str(lock), str(log), "a", 1.0))
    first.start()
    deadline = time.monotonic() + 30
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    second = spawn.Process(target=_hold, args=(str(lock), str(log), "b", 0.1))
    second.start()
    first.join(30)
    second.join(30)
    assert log.read_text().splitlines() == ["a start", "a end", "b start", "b end"]


def test_a_waiter_names_the_holder_then_proceeds(tmp_path: Path) -> None:
    lock = tmp_path / "dev.lock"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with locks.FileLock(lock, command="sandloom dev up", out=io.StringIO()):
            holding.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(10)
    out = io.StringIO()
    threading.Timer(0.5, release.set).start()
    with locks.FileLock(lock, out=out, poll=0.05):
        pass
    thread.join(10)
    assert "sandloom dev up" in out.getvalue()
    assert str(os.getpid()) in out.getvalue()


def _hold_forever(path: str, ready: str) -> None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # The record names a process that is gone (as when a dead holder's child
    # inherited the open file and keeps the lock).
    record = {"pid": 2**22 + 12345, "host": socket.gethostname(), "heartbeat": 0}
    os.write(fd, json.dumps(record).encode())
    Path(ready).write_text("ok")
    time.sleep(60)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl")
def test_a_holder_whose_record_names_an_invisible_pid_keeps_the_lock(
    tmp_path: Path,
) -> None:
    """The record says the holder is gone, but a live process holds the
    ``flock`` (a child that inherited it, another pid namespace): the waiter
    waits, never takes it over and never unlinks the file."""
    lock, ready = tmp_path / "dev.lock", tmp_path / "ready"
    spawn = multiprocessing.get_context("spawn")
    stuck = spawn.Process(target=_hold_forever, args=(str(lock), str(ready)))
    stuck.start()
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        inode = lock.stat().st_ino
        out = io.StringIO()
        waiter = locks.FileLock(lock, out=out, poll=0.05, timeout=1.5)
        with pytest.raises(TimeoutError, match=str(2**22 + 12345)):
            waiter.acquire()
        assert lock.stat().st_ino == inode
        assert locks.read_holder(lock)["pid"] == 2**22 + 12345
        assert "waiting for" in out.getvalue()
    finally:
        stuck.kill()
        stuck.join()
    # The holder died: the operating system released the lock.
    with locks.FileLock(lock, out=io.StringIO(), poll=0.05, timeout=5):
        assert locks.read_holder(lock)["pid"] == os.getpid()
    assert lock.exists()


def test_a_live_holder_is_never_taken_over(tmp_path: Path) -> None:
    lock = tmp_path / "dev.lock"
    with locks.FileLock(lock, out=io.StringIO()):
        waiter = locks.FileLock(lock, out=io.StringIO(), poll=0.05, timeout=1)
        with pytest.raises(TimeoutError, match=str(os.getpid())):
            waiter.acquire()


def test_a_second_up_waits_for_the_first(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    calls: list[str] = []
    monkeypatch.setattr(commands._docker, "check_docker", lambda env: None)
    monkeypatch.setattr(commands, "ports_needed", lambda ctx, state, **kw: [])

    def finish(ctx: Any, state: State, options: Any) -> int:
        calls.append("finish")
        return 0

    monkeypatch.setattr(commands, "_finish_up", finish)
    holding, release = threading.Event(), threading.Event()

    def first() -> None:
        with ctx.repo_lock("sandloom dev up"):
            holding.set()
            release.wait(10)
            calls.append("first done")

    thread = threading.Thread(target=first)
    thread.start()
    holding.wait(10)
    threading.Timer(0.5, release.set).start()
    assert commands.up(ctx, commands.UpOptions(pull=False)) == 0
    thread.join(10)
    assert calls == ["first done", "finish"]
    assert "waiting for another `sandloom dev up`" in _text(ctx.err)


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def test_ports_skip_the_registry_and_taken_ports() -> None:
    first = list(ports.candidates("shop-abc123"))[:3]
    chosen = ports.choose_ports(
        "shop-abc123",
        ["db", "redis"],
        reserved={first[0]},
        is_free=lambda p: p != first[1],
    )
    assert chosen["db"] == first[2]
    assert all(ports.LOW <= p <= ports.HIGH for p in chosen.values())
    assert len(set(chosen.values())) == 2


def test_a_taken_sticky_port_is_reassigned_and_every_checkout_rewritten(
    repo: Path,
    worktree: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        taken = squatter.getsockname()[1]
        state.ports["db"] = taken
        for root in (repo, worktree):
            c = _ctx(root, environ)
            commands.write_checkout_files(c, state, commands.checkout_row(c, state))
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        (ctx.state_dir / commands.STATE_COMPOSE).write_text("services: {}\n")
        monkeypatch.setattr(commands._docker.Compose, "states", lambda self: {})
        needed = commands.ports_needed(ctx, state)
        assert needed == ["db"]
        assert commands.assign_ports(ctx, state, needed) is True
        commands._rewrite_checkouts(ctx, state, needed)
    new = state.ports["db"]
    assert new != taken
    for root in (repo, worktree):
        block = envfile.read_block((root / ".env").read_text())
        assert f"127.0.0.1:{new}/" in block["DATABASE_URL"]
    assert load_registry(ctx.home)[new] == str(ctx.identity.repository)
    assert taken not in load_registry(ctx.home)
    assert "Restart running app servers" in _text(ctx.err)


def test_a_running_service_keeps_its_port(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    with socket.socket() as ours:
        ours.bind(("127.0.0.1", 0))
        state.ports["db"] = ours.getsockname()[1]
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        (ctx.state_dir / commands.STATE_COMPOSE).write_text("services: {}\n")
        monkeypatch.setattr(
            commands._docker.Compose, "states", lambda self: {"db": "running"}
        )
        assert commands.ports_needed(ctx, state) == []


# --------------------------------------------------------------------------- #
# The .env block and .env.test.local
# --------------------------------------------------------------------------- #

_VALUES = {
    "DATABASE_URL": "postgresql+asyncpg://sandloom:sandloom@127.0.0.1:24001/shop",
    "CACHE_URL": "redis://127.0.0.1:24002/1",
    "BROKER_URL": "redis://127.0.0.1:24002/2",
}


def test_the_block_is_rewritten_keeping_user_lines_and_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# mine\nJWT_SECRET=s3cret\n"
        + envfile.render_block({**_VALUES, "DATABASE_URL": "postgresql://old"})
        + "MY_FLAG=1\n"
    )
    env.chmod(0o600)
    assert envfile.write_env_block(env, _VALUES)
    text = env.read_text()
    assert text.startswith("# mine\nJWT_SECRET=s3cret\n")
    assert text.endswith("MY_FLAG=1\n")
    assert envfile.read_block(text) == _VALUES
    assert "postgresql://old" not in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not envfile.write_env_block(env, _VALUES)  # unchanged: not rewritten


def test_a_file_without_the_block_gets_it_and_old_urls_are_commented(
    tmp_path: Path,
) -> None:
    from dotenv import dotenv_values

    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:5432/x\nA=1\n")
    envfile.write_env_block(env, _VALUES)
    lines = env.read_text().splitlines()
    assert lines[0].startswith("# (moved into the sandloom dev block below) ")
    assert "A=1" in lines
    assert dotenv_values(env)["DATABASE_URL"] == _VALUES["DATABASE_URL"]
    once = env.read_text()
    envfile.write_env_block(env, _VALUES)
    assert env.read_text() == once


def test_a_torn_block_keeps_every_user_line(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "JWT_SECRET=s\n"
        f"{envfile.BEGIN}\n"
        "DATABASE_URL=postgresql://old/db\n"
        "STRIPE_KEY=sk_test_keep\n"
        "export CACHE_URL=redis://old/1\n"
        "FEATURE_X=on\n"
    )
    urls = {
        "DATABASE_URL": "postgresql://127.0.0.1:1/shop",
        "CACHE_URL": "redis://127.0.0.1:2/1",
        "BROKER_URL": "redis://127.0.0.1:2/2",
    }
    assert envfile.write_env_block(env, urls)
    text = env.read_text()
    for kept in ("JWT_SECRET=s", "STRIPE_KEY=sk_test_keep", "FEATURE_X=on"):
        assert f"\n{kept}\n" in f"\n{text}"
    assert "# (moved into the sandloom dev block below) DATABASE_URL=" in text
    assert "# (moved into the sandloom dev block below) export CACHE_URL=" in text
    assert text.count(envfile.BEGIN) == 1 and text.count(envfile.END) == 1
    assert envfile.read_block(text) == urls
    # Stable from now on: a second write changes nothing.
    assert not envfile.write_env_block(env, urls)


def test_an_edit_during_the_write_is_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    reads = {"n": 0}
    real = envfile._read

    def racing(path: Path) -> str:
        reads["n"] += 1
        if reads["n"] == 2:  # the user saves the file between read and replace
            env.write_text("A=1\nB=2\n")
        return real(path)

    monkeypatch.setattr(envfile, "_read", racing)
    assert envfile.write_env_block(env, {"DATABASE_URL": "postgresql://x/y"})
    text = env.read_text()
    assert "B=2" in text and "DATABASE_URL=postgresql://x/y" in text


def test_a_symlinked_env_is_written_through_the_link(tmp_path: Path) -> None:
    target = tmp_path / "shared.env"
    target.write_text("A=1\n")
    target.chmod(0o640)
    link = tmp_path / ".env"
    link.symlink_to(target)
    envfile.write_env_block(link, _VALUES)
    assert link.is_symlink()
    assert envfile.read_block(target.read_text()) == _VALUES
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_a_new_worktree_gets_a_seeded_env(
    repo: Path, worktree: Path, environ: dict[str, str]
) -> None:
    assert not (worktree / ".env").exists()
    ctx = _ctx(worktree, environ)
    state = _state(ctx)
    row = commands.checkout_row(ctx, state)
    notes = commands.write_checkout_files(ctx, state, row)
    env = (worktree / ".env").read_text()
    assert any("from .env.example" in note for note in notes)
    assert "ENVIRONMENT=dev" in env
    secret = next(ln for ln in env.splitlines() if ln.startswith("JWT_SECRET="))
    assert secret != "JWT_SECRET=change_me" and len(secret.split("=", 1)[1]) == 64
    assert stat.S_IMODE((worktree / ".env").stat().st_mode) == 0o600
    block = envfile.read_block(env)
    assert block["DATABASE_URL"].endswith(f"/{ctx.identity.database}")
    assert ctx.identity.database != "shop"
    local = (worktree / ".env.test.local").read_text()
    assert f"/{ctx.identity.test_database}\n" in local


def test_localhost_is_never_written(
    repo: Path, environ: dict[str, str], tmp_path: Path
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    commands.write_checkout_files(ctx, state, commands.checkout_row(ctx, state))
    for name in (".env", ".env.test.local"):
        text = (repo / name).read_text()
        assert "localhost" not in text and "127.0.0.1" in text, name
    generate_project("probe", tmp_path, framework_source="pypi")
    app = tmp_path / "probe"
    for name in (".env", ".env.example", ".env.test", "deploy/docker-compose.dev.yml"):
        assert "localhost" not in (app / name).read_text(), name


def test_a_remote_docker_host_is_used_for_the_urls(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, {**environ, "DOCKER_HOST": "tcp://10.0.0.7:2376"})
    state = _state(ctx)
    urls = commands.checkout_urls(ctx, state, commands.checkout_row(ctx, state))
    assert "@10.0.0.7:24001/" in urls["DATABASE_URL"]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """No service answers: only the pid files and the state tell who is busy."""

    def down(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    for name in ("sessions", "advisory_holders", "redis_clients"):
        monkeypatch.setattr(commands._services, name, down)


def test_down_refuses_while_other_checkouts_are_active(
    repo: Path, worktree: Path, environ: dict[str, str], no_services: None
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    commands.checkout_row(ctx, state)
    other = commands.checkout_row(_ctx(worktree, environ), state)
    other.last_up = time.time() - 3600
    save_state(ctx.state_dir, state)
    with pytest.raises(commands.DevError, match=str(worktree.resolve())) as refused:
        commands.down(ctx, all_=False, volumes=False, force=False)
    assert "--all" in str(refused.value)
    other.last_up = time.time() - 2 * commands.ACTIVE_SECONDS
    assert commands._down_refusal(ctx, state, all_=False, force=False) is None


def test_down_and_reset_refuse_while_this_checkout_runs_the_app(
    repo: Path, environ: dict[str, str], no_services: None
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    commands.checkout_row(ctx, state)
    save_state(ctx.state_dir, state)
    marker = commands.run_file(repo)
    marker.parent.mkdir(parents=True)
    record = {"pid": os.getpid(), "host": socket.gethostname(), "url": "http://x"}
    marker.write_text(json.dumps(record))
    with pytest.raises(commands.DevError, match="dev run"):
        commands.reset(ctx, force=False)
    with pytest.raises(commands.DevError, match="--force"):
        commands.down(ctx, all_=True, volumes=False, force=False)
    assert commands._down_refusal(ctx, state, all_=True, force=True) is None


def test_volumes_need_all_and_a_confirmation(
    repo: Path, environ: dict[str, str], no_services: None
) -> None:
    ctx = _ctx(repo, environ)
    save_state(ctx.state_dir, _state(ctx))
    with pytest.raises(commands.DevError, match="needs --all"):
        commands.down(ctx, all_=False, volumes=True, force=False)
    with pytest.raises(commands.DevError, match="confirm"):
        commands.down(ctx, all_=True, volumes=True, force=False)


def test_a_second_dev_run_answers_already_running(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    marker = commands.run_file(repo)
    marker.parent.mkdir(parents=True)
    record = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "url": "http://127.0.0.1:8123",
    }
    marker.write_text(json.dumps(record))
    assert commands.run(ctx, port=None, reload=False) == 0
    assert "already running at http://127.0.0.1:8123" in _text(ctx.out)


def test_a_taken_app_port_names_a_free_one() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(commands.DevError, match="is in use") as refused:
            commands.app_port(str(port))
    assert "--port auto" in str(refused.value)
    assert ports.port_free(commands.app_port("auto"))


# --------------------------------------------------------------------------- #
# CI, Docker missing
# --------------------------------------------------------------------------- #


def test_ci_with_test_services_is_a_no_op(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {"CI": "true", "SANDLOOM_TEST_DATABASE_URL": "postgresql://ci/t"}
    assert dev_cli.main(["up"], environ=env) == 0
    assert "CI provides the services" in capsys.readouterr().out
    assert dev_cli.main(["up"], environ={"CI": "true"}) == 1
    assert "SANDLOOM_TEST_DATABASE_URL" in capsys.readouterr().err
    assert dev_cli.main(["down"], environ=env) == 1


def test_no_docker_is_one_line(
    repo: Path,
    environ: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(commands._docker.shutil, "which", lambda name: None)
    assert dev_cli.main(["up"], environ=environ) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("error: Docker is not reachable")


# --------------------------------------------------------------------------- #
# The one-line connection error
# --------------------------------------------------------------------------- #

_REFUSED = 'connection to server at "127.0.0.1", port 5432 failed: Connection refused'


def test_connection_failures_become_one_line() -> None:
    import psycopg2  # type: ignore[import-untyped]
    import redis

    line = errors.connection_failure(psycopg2.OperationalError(_REFUSED))
    assert line == (
        "error: cannot reach PostgreSQL at 127.0.0.1:5432 (connection refused). "
        "Start the dev services: uv run sandloom dev up   [SANDLOOM-R014]"
    )
    rd = redis.exceptions.ConnectionError(
        "Error 61 connecting to 127.0.0.1:6379. Connection refused."
    )
    assert "cannot reach Redis at 127.0.0.1:6379" in (
        errors.connection_failure(rd) or ""
    )
    assert errors.connection_failure(ValueError("nope")) is None


def test_a_wrapped_asyncpg_refusal_is_recognised() -> None:
    from sqlalchemy.exc import OperationalError

    refused = ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 24817)")

    class FakeAsyncpg(Exception):
        pass

    FakeAsyncpg.__module__ = "asyncpg.exceptions"
    line = None
    try:
        try:
            raise FakeAsyncpg("boom") from refused
        except FakeAsyncpg as inner:
            raise OperationalError("SELECT 1", {}, inner) from inner
    except OperationalError as exc:
        line = errors.connection_failure(exc)
    assert line is not None
    assert "PostgreSQL at 127.0.0.1:24817 (connection refused)" in line


def test_an_error_raised_while_handling_a_refusal_keeps_its_traceback() -> None:
    """``__context__`` is never followed: the real error is not hidden."""
    import psycopg2

    try:
        try:
            raise psycopg2.OperationalError(_REFUSED)
        except psycopg2.OperationalError:
            raise KeyError("a bug in the handler") from None
    except KeyError as exc:
        assert errors.connection_failure(exc) is None
    try:
        try:
            raise psycopg2.OperationalError(_REFUSED)
        except psycopg2.OperationalError:
            {}["missing"]
    except KeyError as exc:
        assert exc.__context__ is not None
        assert errors.connection_failure(exc) is None


def test_the_outermost_exception_decides() -> None:
    """An application error raised ``from`` a refusal is not a refusal."""
    import psycopg2

    try:
        raise RuntimeError("the import job failed") from psycopg2.OperationalError(
            _REFUSED
        )
    except RuntimeError as exc:
        assert errors.connection_failure(exc) is None


def test_authentication_and_missing_database_have_their_own_lines() -> None:
    import psycopg2
    from sqlalchemy.exc import OperationalError

    at = 'connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
    auth = errors.connection_failure(
        psycopg2.OperationalError(at + 'password authentication failed for user "u"')
    )
    assert auth == (
        "error: PostgreSQL at 127.0.0.1:5432 refused the login (authentication "
        "failed): check the user and password in the URL   [SANDLOOM-R014]"
    )
    missing = errors.connection_failure(
        psycopg2.OperationalError(at + 'database "shop" does not exist')
    )
    assert missing == (
        "error: the database shop does not exist on PostgreSQL at 127.0.0.1:5432: "
        "uv run sandloom dev up creates it (else create it, then uv run sandloom "
        "migrate upgrade-all --apply)   [SANDLOOM-R014]"
    )

    class InvalidPasswordError(Exception):
        pass

    InvalidPasswordError.__module__ = "asyncpg.exceptions"
    wrapped = OperationalError("SELECT 1", {}, InvalidPasswordError("bad"))
    assert "refused the login" in (errors.connection_failure(wrapped) or "")


@pytest.mark.parametrize(
    ("argv", "debug", "left"),
    [
        (["sandloom", "--debug", "migrate"], True, ["sandloom", "migrate"]),
        (["sandloom", "test", "--", "--debug"], False, None),
        (["sandloom", "test", "--debug"], False, None),
        (["sandloom", "migrate", "status"], False, None),
    ],
)
def test_debug_is_taken_only_before_the_subcommand(
    argv: list[str], debug: bool, left: list[str] | None
) -> None:
    before = list(argv)
    assert errors.debug_requested(argv, {}) is debug
    assert argv == (left if left is not None else before)


def test_migrate_prints_the_same_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import psycopg2

    from sandloom.core.cli import migrate

    def refused() -> None:
        raise psycopg2.OperationalError(_REFUSED)

    monkeypatch.setattr(migrate, "enforce_configuration", lambda settings: None)
    monkeypatch.setattr(migrate, "_load_core", refused)
    assert migrate.main(["databases"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [
        "error: cannot reach PostgreSQL at 127.0.0.1:5432 (connection refused). "
        "Start the dev services: uv run sandloom dev up   [SANDLOOM-R014]"
    ]


@pytest.mark.subprocess_probe
def test_the_cli_prints_one_line_and_debug_shows_the_traceback(
    tmp_path: Path,
) -> None:
    """A real command against a closed port: one line, or the traceback."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SANDLOOM_", "DATABASE_URL", "CACHE_URL", "BROKER_URL"))
    }
    env.update(
        DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:1/x",
        CACHE_URL="redis://127.0.0.1:2/0",
        BROKER_URL="redis://127.0.0.1:2/1",
        JWT_SECRET="x" * 64,
        ENVIRONMENT="dev",
        SANDLOOM_HOME=str(tmp_path / "home"),
    )
    entry = "import sys; from sandloom import main; main()"

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", entry, *args],
            cwd=tmp_path,
            env={**env, **extra},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    quiet = run("outbox", "list-dead")
    assert quiet.returncode == 1
    errors_ = [ln for ln in quiet.stderr.splitlines() if "[INFO]" not in ln]
    errors_ = [ln for ln in errors_ if "[WARNING]" not in ln]
    assert errors_ == [
        "error: cannot reach PostgreSQL at 127.0.0.1:1 (connection refused). "
        "Start the dev services: uv run sandloom dev up   [SANDLOOM-R014]"
    ]
    for loud in (
        run("--debug", "outbox", "list-dead"),
        run("outbox", "list-dead", SANDLOOM_DEBUG="1"),
    ):
        assert loud.returncode == 1
        assert "Traceback (most recent call last)" in loud.stderr
        assert "[SANDLOOM-R014]" not in loud.stderr


def test_the_error_code_is_catalogued() -> None:
    entry = error_codes.get(errors.CODE)
    assert entry.kind == "runtime" and "sandloom dev up" in entry.fix


def test_an_exported_url_that_differs_from_the_block_is_warned(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(envfile.render_block(_VALUES))
    out = io.StringIO()
    drift = errors.warn_env_drift(
        tmp_path, {"DATABASE_URL": "postgresql://elsewhere/x"}, out
    )
    assert drift == ["DATABASE_URL"]
    assert "the exported value wins" in out.getvalue()


# --------------------------------------------------------------------------- #
# First run: sticky app port, a port Docker releases late, quiet down, exit 0
# --------------------------------------------------------------------------- #


def _compose_copy(ctx: commands.Context) -> None:
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / commands.STATE_COMPOSE).write_text("services: {}\n")


def test_a_port_docker_releases_a_moment_after_down_stays(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker Desktop frees a published port a moment after ``down``: the
    check waits for it instead of moving the port."""
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    _compose_copy(ctx)
    monkeypatch.setattr(commands._docker.Compose, "states", lambda self: {})
    monkeypatch.setattr(commands, "PORT_SETTLE_SECONDS", 3.0)
    monkeypatch.setattr(commands, "PORT_SETTLE_STEP", 0.01)
    calls: list[int] = []

    def late(port: int) -> bool:
        calls.append(port)
        return port != state.ports["db"] or len(calls) > 4

    monkeypatch.setattr(commands._ports, "port_free", late)
    assert commands.ports_needed(ctx, state) == []
    assert "moved" not in _text(ctx.err)


def test_a_port_dockers_proxy_holds_right_after_down_is_ours(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    _compose_copy(ctx)
    monkeypatch.setattr(commands._docker.Compose, "states", lambda self: {})
    monkeypatch.setattr(commands, "PORT_SETTLE_SECONDS", 0.0)
    held = state.ports["db"]
    monkeypatch.setattr(commands._ports, "port_free", lambda port: port != held)
    for proxy in ("com.docker.backend", "vpnkit-bridge", "docker-proxy", "ssh"):
        monkeypatch.setattr(commands, "port_listener", lambda port, p=proxy: (p, 7))
        state.stopped_at = time.time() - 5  # `dev down` just ran
        assert commands.ports_needed(ctx, state) == [], proxy
        # Long after the services stopped, Docker holds it for someone else.
        state.stopped_at = time.time() - 2 * commands.STOPPING_SECONDS
        assert commands.ports_needed(ctx, state) == ["db"], proxy


def test_a_foreign_holder_still_moves_the_port(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    state.stopped_at = time.time()
    _compose_copy(ctx)
    monkeypatch.setattr(commands._docker.Compose, "states", lambda self: {})
    monkeypatch.setattr(commands, "PORT_SETTLE_SECONDS", 0.0)
    held = state.ports["db"]
    monkeypatch.setattr(commands._ports, "port_free", lambda port: port != held)
    monkeypatch.setattr(commands, "port_listener", lambda port: ("postgres", 99))
    assert commands.ports_needed(ctx, state) == ["db"]


def test_down_records_when_the_services_stopped_and_prints_one_line(
    repo: Path,
    environ: dict[str, str],
    no_services: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(repo, environ)
    save_state(ctx.state_dir, _state(ctx))
    seen: list[dict[str, Any]] = []

    def run(
        self: Any, *args: str, capture: bool = False, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        seen.append({"args": args, "capture": capture})
        noise = " Container shop-db-1 Removing\n Network shop_default Removed\n"
        return subprocess.CompletedProcess(args, 0, noise if capture else None, "")

    monkeypatch.setattr(commands._docker.Compose, "run", run)
    before = time.time()
    assert commands.down(ctx, all_=False, volumes=False, force=False) == 0
    assert seen[-1]["capture"] is True
    assert _text(ctx.out).strip().splitlines() == [
        f"stopped {ctx.identity.project} (the data stays)"
    ]
    stored = load_state(ctx.state_dir)
    assert stored is not None and stored.stopped_at >= before
    loud = _ctx(repo, environ)
    loud.verbose = True
    assert commands.down(loud, all_=False, volumes=False, force=False) == 0
    assert seen[-1]["capture"] is False  # Compose prints its progress itself


def test_a_failing_down_is_one_line(
    repo: Path,
    environ: dict[str, str],
    no_services: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(repo, environ)
    save_state(ctx.state_dir, _state(ctx))
    monkeypatch.setattr(
        commands._docker.Compose,
        "run",
        lambda self, *a, **k: subprocess.CompletedProcess(a, 1, "", "boom\nno daemon"),
    )
    with pytest.raises(commands.DevError, match="no daemon"):
        commands.down(ctx, all_=False, volumes=False, force=False)


def test_the_app_port_is_8000_else_the_next_free_and_not_a_siblings(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    assert commands.ensure_app_port(ctx, state, is_free=lambda p: True) == 8000
    assert commands.ensure_app_port(ctx, state, is_free=lambda p: False) == 8000
    state.app_ports.clear()
    state.app_ports["/elsewhere/worktree"] = 8001
    assert commands.ensure_app_port(ctx, state, is_free=lambda p: p != 8000) == 8002
    save_state(ctx.state_dir, state)
    stored = load_state(ctx.state_dir)
    assert stored is not None
    assert stored.app_ports[str(ctx.root)] == 8002


def test_an_old_state_without_app_ports_still_loads(tmp_path: Path) -> None:
    (tmp_path / "dev.json").write_text(
        json.dumps({"repository": "/r", "project": "p", "machine": "m"})
    )
    stored = load_state(tmp_path)
    assert stored is not None and stored.app_ports == {} and stored.stopped_at == 0


def test_dev_run_without_port_serves_the_checkouts_port(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    state = _state(ctx)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        sticky = probe.getsockname()[1]
    state.app_ports[str(ctx.root)] = sticky
    commands.checkout_row(ctx, state)
    save_state(ctx.state_dir, state)
    _reachable(monkeypatch, up=True)
    served: list[list[str]] = []
    monkeypatch.setattr(
        commands, "_serve", lambda c, argv, env, **kw: served.append(argv) or 0
    )
    assert commands.run(ctx, port=None, reload=False) == 0
    assert served[0][served[0].index("--port") + 1] == str(sticky)
    assert f"http://127.0.0.1:{sticky}" in _text(ctx.out)


def test_dev_run_signals_the_app_to_set_a_stale_database_aside(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SANDLOOM-B028: only the app `dev run` serves sets a SQLite file whose
    tables lag the models aside; an explicit setting wins."""
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    state = _state(ctx)
    commands.checkout_row(ctx, state)
    save_state(ctx.state_dir, state)
    _reachable(monkeypatch, up=True)
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        commands, "_serve", lambda c, argv, env, **kw: envs.append(env) or 0
    )
    assert commands.run(ctx, port=None, reload=False) == 0
    assert envs[0]["DB_RECREATE_ON_MODEL_CHANGE"] == "true"
    assert "DB_RECREATE_ON_MODEL_CHANGE" not in environ


def test_dev_run_names_a_free_port_when_the_sticky_one_is_taken(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    state = _state(ctx)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        state.app_ports[str(ctx.root)] = busy.getsockname()[1]
        commands.checkout_row(ctx, state)
        save_state(ctx.state_dir, state)
        _reachable(monkeypatch, up=True)
        monkeypatch.setattr(commands, "_serve", lambda c, argv, env, **kw: 0)
        with pytest.raises(commands.DevError, match="is in use") as refused:
            commands.run(ctx, port=None, reload=False)
    assert "uv run sandloom dev run --port" in str(refused.value)
    assert "\n" not in str(refused.value)


def test_the_app_url_is_the_live_run_else_the_sticky_port(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, environ)
    assert commands.app_url(repo, environ) is None
    state = _state(ctx)
    state.app_ports[str(ctx.root)] = 8001
    save_state(ctx.state_dir, state)
    assert commands.app_url(repo, environ) == "http://127.0.0.1:8001"
    marker = commands.run_file(repo)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "url": "http://127.0.0.1:9100",
            }
        )
    )
    assert commands.app_url(repo, environ) == "http://127.0.0.1:9100"


def test_ctrl_c_stops_dev_run_with_exit_status_0(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Child:
        waited = 0
        signals: list[int] = []

        def wait(self, timeout: float | None = None) -> int:
            self.waited += 1
            if self.waited == 1:
                raise KeyboardInterrupt
            return 1  # uvicorn's status after the interrupt

        def send_signal(self, signum: int) -> None:
            self.signals.append(signum)

    child = Child()
    monkeypatch.setattr(commands._process, "spawn", lambda argv, env, cwd: child)
    ctx = _ctx(repo, environ)
    assert commands._serve(ctx, ["uvicorn"], {}) == 0
    assert child.signals  # the child was asked to stop


def test_up_suggests_the_migrations_only_for_a_new_or_empty_database(
    repo: Path, environ: dict[str, str]
) -> None:
    ctx = _ctx(repo, environ)
    state = _state(ctx)
    row = commands.checkout_row(ctx, state)
    state.app_ports[str(ctx.root)] = 8001
    commands._print_summary(ctx, state, row, migrate=True)
    first = _text(ctx.out)
    assert "next: uv run sandloom migrate upgrade-all --apply, then" in first
    again = _ctx(repo, environ)
    commands._print_summary(again, state, row, migrate=False)
    text = _text(again.out)
    assert "migrate" not in text
    assert "next: uv run sandloom dev run (http://127.0.0.1:8001)" in text


def test_debug_brings_the_framework_logs_back() -> None:
    """``sandloom --debug`` (or SANDLOOM_DEBUG=1) shows tracebacks and the
    DEBUG startup lines, in this process and the ones it starts (dev run);
    an exported LOG_LEVEL is the person's own choice and stays."""
    env: dict[str, str] = {}
    errors.enable_debug(env)
    assert env == {"SANDLOOM_DEBUG": "1", "LOG_LEVEL": "DEBUG"}
    mine = {"LOG_LEVEL": "INFO"}
    errors.enable_debug(mine)
    assert mine["LOG_LEVEL"] == "INFO"


def _reachable(monkeypatch: pytest.MonkeyPatch, up: bool) -> None:
    answer = None if up else "connection refused"
    monkeypatch.setattr(commands._services, "pg_ready", lambda pg: answer)
    monkeypatch.setattr(commands._services, "redis_ready", lambda *a, **k: answer)


def test_dev_run_starts_stopped_services_first(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dev run` is the person's "run my app": stopped services are started
    the way `dev up` starts them (one line), then the app is served."""
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    state = _state(ctx)
    commands.checkout_row(ctx, state)
    state.app_ports[str(ctx.root)] = 8123
    save_state(ctx.state_dir, state)
    _reachable(monkeypatch, up=False)
    started: list[commands.UpOptions] = []

    def up(c: commands.Context, options: commands.UpOptions) -> int:
        print("dev services up (compose project x)", file=c.out)
        started.append(options)
        _reachable(monkeypatch, up=True)
        return 0

    monkeypatch.setattr(commands, "up", up)
    monkeypatch.setattr(commands, "app_port", lambda port, sticky=None: sticky or 8000)
    served: list[list[str]] = []
    monkeypatch.setattr(
        commands, "_serve", lambda c, argv, env, **kw: served.append(argv) or 0
    )
    assert commands.run(ctx, port=None, reload=False) == 0
    assert len(started) == 1 and served
    text = _text(ctx.out)
    assert "starting the dev services" in text
    assert "dev services up" not in text  # `up`'s own summary stays quiet


def test_dev_run_with_running_services_starts_nothing(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    (repo / ".env").write_text(envfile.render_block(_VALUES))
    state = _state(ctx)
    commands.checkout_row(ctx, state)
    save_state(ctx.state_dir, state)
    _reachable(monkeypatch, up=True)
    monkeypatch.setattr(commands, "up", lambda c, o: pytest.fail("started"))
    monkeypatch.setattr(commands, "app_port", lambda port, sticky=None: 8123)
    monkeypatch.setattr(commands, "_serve", lambda c, argv, env, **kw: 0)
    assert commands.run(ctx, port=None, reload=False) == 0
    assert "starting" not in _text(ctx.out)


def test_dev_run_without_a_block_starts_the_services(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh checkout (no `dev up` yet): `dev run` brings them up."""
    ctx = _ctx(repo, environ)
    environ.pop("DATABASE_URL", None)

    def up(c: commands.Context, options: commands.UpOptions) -> int:
        (repo / ".env").write_text(envfile.render_block(_VALUES))
        return 0

    monkeypatch.setattr(commands, "up", up)
    monkeypatch.setattr(commands, "app_port", lambda port, sticky=None: 8123)
    monkeypatch.setattr(commands, "_serve", lambda c, argv, env, **kw: 0)
    assert commands.run(ctx, port=None, reload=False) == 0
    assert "starting the dev services" in _text(ctx.out)


def test_a_failing_start_is_one_line(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    environ.pop("DATABASE_URL", None)

    def up(c: commands.Context, options: commands.UpOptions) -> int:
        raise commands.DevError("`docker compose up` failed: port is already allocated")

    monkeypatch.setattr(commands, "up", up)
    with pytest.raises(commands.DevError, match="already allocated"):
        commands.run(ctx, port=None, reload=False)


#: What `docker compose up` printed on an Arch machine whose kernel was
#: updated without a reboot: the error wraps, its last line says nothing.
IPTABLES_FAILURE = """\
 Network my-app-20773e_default  Creating
 Network my-app-20773e_default  Created
 Container my-app-20773e-db-1  Starting
Error response from daemon: failed to set up container networking: driver
 failed programming external connectivity on endpoint my-app-20773e-db-1:
 (iptables failed: iptables --wait -t nat -A DOCKER -p tcp -d 0/0 --dport
 15432 -j DNAT --to-destination 172.18.0.2:5432 ! -i br-1: iptables v1.8.11
 (nf_tables): Chain 'DOCKER' does not exist
 (exit status 4))
"""


def test_a_wrapped_docker_error_is_read_whole() -> None:
    """The reason starts at the line that names the error, not at its last
    wrapped line: Docker's own words say what broke."""
    reason = docker.failure_reason(IPTABLES_FAILURE)
    assert reason.startswith("Error response from daemon: failed to set up")
    assert "Chain 'DOCKER' does not exist" in reason
    assert reason.endswith("(exit status 4))") and "\n" not in reason
    # A long error keeps its end, where the cause is.
    long_error = IPTABLES_FAILURE.replace("--dport", "--dport " + "x " * 200)
    shortened = docker.failure_reason(long_error)
    assert len(shortened) <= docker.LIMIT
    assert shortened.startswith("Error response from daemon")
    assert shortened.endswith("Chain 'DOCKER' does not exist (exit status 4))")
    plain = docker.failure_reason("pulling\nError: port is already allocated\n")
    assert plain == "Error: port is already allocated"
    assert docker.failure_reason("") == ""


def test_a_failed_up_names_the_cause_not_the_last_line(
    repo: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(repo, environ)
    failed = subprocess.CompletedProcess(["docker"], 1, "", IPTABLES_FAILURE)
    monkeypatch.setattr(docker.Compose, "up", lambda self, *a, **k: failed)
    monkeypatch.setattr(commands, "_apply_compose_file", lambda *a, **k: False)
    state = State(repository=str(repo), project="my-app-20773e", machine="m")
    with pytest.raises(commands.DevError) as raised:
        commands._finish_up(ctx, state, commands.UpOptions())
    message = str(raised.value)
    assert "failed to set up container networking" in message
    assert "nf_tables" in message and "\n" not in message


def test_shared_images_are_pulled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev and test services use the same images: list and pull each once."""
    seen: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(docker._process, "run", run)
    images = ["postgres:16-alpine", "redis:7-alpine"] * 2
    assert docker.missing_images(images, {}) == images[:2]
    assert len(seen) == 2


def test_a_port_with_only_closed_connections_left_is_free() -> None:
    """After `dev down` the connections the app had to the database linger in
    TIME_WAIT on the database's port: no one listens, so it is free (this
    is what moved the port after every down/up)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = listener.accept()
    served.close()  # the server side closes first: its port enters TIME_WAIT
    client.close()
    listener.close()
    assert ports.port_free(port)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert not ports.port_free(busy.getsockname()[1])
