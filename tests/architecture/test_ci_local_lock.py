"""The CPU-quiet lock shared by local CI and CPU benchmarks (``tools/ci/ci_lock.py``).

On 2026-10-21 an empty ``/tmp/sandloom-ci-local.lock`` left by a benchmark that only
flocked it blocked every ``tools/ci/ci_local.sh`` run, because ci_local treated
the file's existence as "held". Both sides now speak one protocol — a flock on
the file — so a leftover file nobody holds is stale. Each test uses its own
lock path, never the real one.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "tools" / "ci"


def _load(name: str) -> ModuleType:
    path = SCRIPTS / f"{name}.py"
    if not path.exists():
        pytest.fail(f"required public CI tool is missing: tools/ci/{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def ci_lock() -> ModuleType:
    return _load("ci_lock")


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "sandloom-ci-local.lock"


def _holder(lock_path: Path) -> subprocess.Popen[str]:
    """A separate process holding the lock until killed."""
    code = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import ci_lock, pathlib, time\n"
        f"lock = ci_lock.try_acquire({{'tool': 'test'}}, pathlib.Path({str(lock_path)!r}))\n"
        "print('held' if lock else 'busy', flush=True)\n"
        "time.sleep(600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_empty_leftover_file_is_stale(ci_lock: ModuleType, lock_path: Path) -> None:
    lock_path.write_text("")  # what the old http_pipeline.py left behind
    assert not ci_lock.is_held(lock_path)
    lock = ci_lock.try_acquire({"run_id": "r1"}, lock_path)
    assert lock is not None
    text = lock_path.read_text()
    assert "protocol=flock\n" in text and "run_id=r1\n" in text and "pid=" in text
    assert ci_lock.is_held(lock_path)
    lock.release()
    assert not lock_path.exists()
    lock.release()  # idempotent


def test_unparseable_leftover_is_stale(ci_lock: ModuleType, lock_path: Path) -> None:
    lock_path.write_bytes(b"\xff\x00garbage without pid\n")
    with ci_lock.held({"tool": "t"}, lock_path) as lock:
        assert lock is not None


def test_live_holder_blocks_and_crash_releases(
    ci_lock: ModuleType, lock_path: Path
) -> None:
    proc = _holder(lock_path)
    try:
        assert ci_lock.is_held(lock_path)
        assert ci_lock.try_acquire({"tool": "t"}, lock_path) is None
        assert f"pid={proc.pid}\n" in ci_lock.holder_text(lock_path)
    finally:
        proc.kill()  # SIGKILL: no release code runs
        proc.wait()
    assert lock_path.exists()  # the file is left behind ...
    assert not ci_lock.is_held(lock_path)  # ... but nobody holds it
    lock = ci_lock.acquire({"tool": "t"}, lock_path, poll_s=0.01)
    lock.release()


@pytest.mark.parametrize("protocol", ["", "protocol=flock\n"])
def test_dead_pid_is_taken_over(
    ci_lock: ModuleType, lock_path: Path, protocol: str
) -> None:
    lock_path.write_text(f"{protocol}run_id=old\npid={_dead_pid()}\nref=main\n")
    assert not ci_lock.is_held(lock_path)
    lock = ci_lock.try_acquire({"run_id": "new"}, lock_path)
    assert lock is not None
    assert "run_id=new\n" in lock_path.read_text()
    assert "run_id=old" not in lock_path.read_text()
    lock.release()


def test_legacy_holder_with_live_pid_is_respected(
    ci_lock: ModuleType, lock_path: Path
) -> None:
    """An older ci_local (no flock) holds the file: wait for it, never steal."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        lock_path.write_text(f"run_id=old\npid={proc.pid}\nref=main\n")
        assert ci_lock.is_held(lock_path)
        assert ci_lock.try_acquire({"tool": "t"}, lock_path) is None
        assert lock_path.read_text().startswith("run_id=old\n")
    finally:
        proc.kill()
        proc.wait()
    assert not ci_lock.is_held(lock_path)


def test_concurrent_acquire_is_mutually_exclusive(
    ci_lock: ModuleType, lock_path: Path
) -> None:
    """Many contenders, release unlinking the file: never two holders at once."""
    inside = 0
    peak = 0
    acquired = 0
    guard = threading.Lock()
    start = threading.Barrier(8)

    def contend() -> None:
        nonlocal inside, peak, acquired
        start.wait()
        for _ in range(25):
            lock = ci_lock.acquire({"tool": "t"}, lock_path, poll_s=0.0005)
            with guard:
                inside += 1
                peak = max(peak, inside)
                acquired += 1
            time.sleep(0.0005)
            with guard:
                inside -= 1
            lock.release()

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert acquired == 8 * 25
    assert peak == 1
    assert not lock_path.exists()


def test_concurrent_acquire_across_processes(
    ci_lock: ModuleType, lock_path: Path
) -> None:
    proc = _holder(lock_path)
    got: list[object] = []
    waiter = threading.Thread(
        target=lambda: got.append(
            ci_lock.acquire({"tool": "w"}, lock_path, poll_s=0.01)
        )
    )
    waiter.start()
    time.sleep(0.2)
    assert not got  # still waiting on the other process
    proc.kill()
    proc.wait()
    waiter.join(timeout=10)
    assert len(got) == 1
    got[0].release()  # type: ignore[attr-defined]


def test_ci_local_uses_the_shared_lock(ci_lock: ModuleType, lock_path: Path) -> None:
    ci_local = _load("ci_local")
    said: list[str] = []
    run = SimpleNamespace(
        run_id="20261021-x",
        args=SimpleNamespace(ref="HEAD"),
        say=said.append,
        lock=None,
    )
    lock_path.write_text("")  # the leftover that deadlocked CI on 2026-10-21
    ci_local.acquire_lock(run, wait=True, path=lock_path)
    assert "run_id=20261021-x\n" in lock_path.read_text()
    assert not said  # never waited

    other = SimpleNamespace(
        run_id="other", args=SimpleNamespace(ref="HEAD"), say=said.append, lock=None
    )
    with pytest.raises(SystemExit, match="is held"):
        ci_local.acquire_lock(other, wait=False, path=lock_path)

    ci_local.release_lock(run)
    assert run.lock is None and not lock_path.exists()
    ci_local.acquire_lock(other, wait=False, path=lock_path)
    ci_local.release_lock(other)
