"""Gunicorn process config: CPU-quota-aware workers + single relay leader."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_gunicorn_worker_count_honours_cgroup_quota() -> None:
    from sandloom.core import cpu_quota
    from sandloom.core.cli import gunicorn_conf as g

    assert cpu_quota.cgroup_cpu_limit(v2="100000 100000") == 1.0
    assert cpu_quota.cgroup_cpu_limit(v2="50000 100000") == 0.5
    assert cpu_quota.cgroup_cpu_limit(v2="max 100000") is None
    assert (
        cpu_quota.cgroup_cpu_limit(v2="", v1_quota="200000", v1_period="100000") == 2.0
    )
    assert cpu_quota.cgroup_cpu_limit(v2="", v1_quota="-1", v1_period="100000") is None
    assert g.default_workers(0.5) == 3
    assert g.default_workers(1.0) == 3
    assert g.default_workers(16.0) == 4  # capped
    assert g.resolve_workers({"WEB_CONCURRENCY": "7"}) == 7
    assert g.resolve_workers({"WEB_CONCURRENCY": ""}) == g.default_workers()


def test_gunicorn_elects_exactly_one_relay_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import gunicorn_conf as g

    server = SimpleNamespace(WORKERS={})
    workers = []
    for pid in range(3):
        w = SimpleNamespace()
        g.pre_fork(server, w)
        server.WORKERS[pid] = w
        workers.append(w)
    assert [w.sandloom_relay_leader for w in workers] == [True, False, False]
    # Leader dies -> the replacement becomes leader.
    del server.WORKERS[0]
    replacement = SimpleNamespace()
    g.pre_fork(server, replacement)
    assert replacement.sandloom_relay_leader is True

    monkeypatch.setenv("OUTBOX_RELAY_ENABLED", "auto")
    # This hook normally runs in a freshly forked child, not pytest's process.
    # Capture its exported worker count so it cannot leak into later pool tests.
    monkeypatch.setenv("SANDLOOM_WORKER_COUNT", "1")
    g.post_fork(server, workers[1])
    import os

    assert os.environ["OUTBOX_RELAY_ENABLED"] == "delegated"


def test_gunicorn_uses_maintained_uvicorn_worker_with_lb_safe_keepalive() -> None:
    from sandloom.core.cli import gunicorn_conf as g

    # uvicorn.workers is deprecated upstream; the class lives in uvicorn-worker.
    assert g.worker_class == "uvicorn_worker.UvicornWorker"
    from uvicorn_worker import UvicornWorker

    assert UvicornWorker.CONFIG_KWARGS == {"loop": "auto", "http": "auto"}
    # Above common LB idle timeouts (ALB 60 s); uvicorn's own default is 5 s.
    assert g.resolve_keepalive({}) == 75
    assert g.resolve_keepalive({"GUNICORN_KEEPALIVE": "620"}) == 620
    assert g.resolve_keepalive({"GUNICORN_KEEPALIVE": "nope"}) == 75
    assert g.resolve_keepalive({"GUNICORN_KEEPALIVE": "0"}) == 75


def test_uvicorn_standard_selects_uvloop_and_httptools_when_installed() -> None:
    """Smoke: the runtime deps (uvicorn[standard]) give the fast loop/parser."""
    import importlib.util

    if importlib.util.find_spec("uvloop") is None:  # pragma: no cover
        pytest.skip("uvloop unavailable on this platform")
    from uvicorn.config import Config

    from sandloom.core.cli import gunicorn_conf as g

    # Before the worker's loop starts (post_worker_init): "auto" resolved.
    assert g.server_implementation() == {"loop": "uvloop", "http": "httptools"}
    assert g.server_implementation("asyncio")["loop"] == "asyncio"

    # Inside the loop uvicorn actually builds (a loop factory, no policy).
    factory = Config(
        app="sandloom.main:app", loop="auto", http="auto"
    ).get_loop_factory()
    assert factory is not None

    async def _probe() -> dict[str, str]:
        return g.server_implementation("asyncio")  # the running loop wins

    loop = factory()
    try:
        assert loop.run_until_complete(_probe()) == {
            "loop": "uvloop",
            "http": "httptools",
        }
    finally:
        loop.close()


def test_post_worker_init_logs_the_server_implementation() -> None:
    from sandloom.core.cli import gunicorn_conf as g

    lines: list[str] = []
    log = SimpleNamespace(info=lambda msg, *args: lines.append(msg % args))
    g.post_worker_init(SimpleNamespace(log=log, cfg=SimpleNamespace(keepalive=75)))
    assert lines and "loop=" in lines[0] and "http=" in lines[0]
    assert "keepalive=75s" in lines[0]


class _FakeArbiter:
    """The slice of gunicorn's Arbiter the relay-leader hooks interact with:
    ``spawn`` (pre_fork + register), ``manage`` (spawn up to / kill the
    OLDEST beyond ``num_workers``), ``reload`` (SIGHUP: on_reload, spawn a full
    new generation, then manage) and ``reap`` (pop the exited worker, then
    ``child_exit``)."""

    def __init__(self, num_workers: int) -> None:
        import logging

        from sandloom.core.cli import gunicorn_conf as g

        self.g = g
        self.WORKERS: dict[int, SimpleNamespace] = {}
        self.num_workers = num_workers
        self.log = logging.getLogger("test.gunicorn")
        self.terminating: list[int] = []
        self._pid = 100
        self._age = 0

    def spawn(self) -> None:
        self._pid += 1
        self._age += 1
        w = SimpleNamespace(age=self._age, pid=self._pid)
        self.g.pre_fork(self, w)
        self.WORKERS[self._pid] = w

    def manage(self) -> None:
        while len(self.WORKERS) < self.num_workers:
            self.spawn()
        alive = sorted(
            (item for item in self.WORKERS.items() if item[0] not in self.terminating),
            key=lambda item: item[1].age,
        )
        while len(alive) > self.num_workers:
            pid, _ = alive.pop(0)
            self.kill_worker(pid, 15)

    def reload(self) -> None:
        getattr(self.g, "on_reload", lambda _s: None)(self)
        for _ in range(self.num_workers):
            self.spawn()
        self.manage()

    def kill_worker(self, pid: int, _sig: int) -> None:
        if pid not in self.terminating:
            self.terminating.append(pid)

    def reap(self) -> None:
        """Every terminating worker exits; the master then manages workers."""
        while self.terminating:
            pid = self.terminating.pop(0)
            worker = self.WORKERS.pop(pid)
            getattr(self.g, "child_exit", lambda _s, _w: None)(self, worker)
        self.manage()

    def leaders(self) -> list[int]:
        return [
            pid
            for pid, w in self.WORKERS.items()
            if w.sandloom_relay_leader and pid not in self.terminating
        ]


def test_relay_leader_survives_a_sighup_graceful_reload() -> None:
    arbiter = _FakeArbiter(3)
    arbiter.manage()
    assert len(arbiter.leaders()) == 1
    arbiter.reload()  # new generation forks while the old one still lives
    arbiter.reap()  # the old generation exits
    assert len(arbiter.WORKERS) == 3
    assert len(arbiter.leaders()) == 1


def test_relay_leader_is_reelected_when_ttou_retires_it() -> None:
    arbiter = _FakeArbiter(3)
    arbiter.manage()
    arbiter.num_workers = 2  # TTOU: gunicorn retires the OLDEST = the leader
    arbiter.manage()
    arbiter.reap()
    arbiter.reap()  # the recycled worker exits; its replacement is elected
    assert len(arbiter.WORKERS) == 2
    assert len(arbiter.leaders()) == 1


def test_crashed_leader_is_replaced_without_recycling_others() -> None:
    arbiter = _FakeArbiter(3)
    arbiter.manage()
    [leader] = arbiter.leaders()
    survivors = set(arbiter.WORKERS) - {leader}
    arbiter.kill_worker(leader, 9)
    arbiter.reap()
    assert len(arbiter.leaders()) == 1
    assert survivors <= set(arbiter.WORKERS)  # nobody else was recycled
