"""Negative controls for the ladder load client (``benchsuite.loadclient``).

A fake server (``harness_fake_server.py``, its own process) answers wrong
statuses, 5xx, timeouts and dropped connections at KNOWN rates: the workload
schedule is deterministic, so the exact number of requests that hit each
behaviour is known before the run. The client must count every request,
report each error class exactly, fail the run on any unexpected status and
time latency with sub-millisecond resolution.

These pin the four defects of the 2026-10-11 integration run's stand
harness: open-loop errors were lost; ``--mix`` accepted the UNION of the
workload's statuses; latency used uvloop's 1 ms ``loop.time()``; (the dataset
reset is pinned in ``test_harness_dataset.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from benchsuite import loadclient as lc

HERE = Path(__file__).resolve().parent
TIMEOUT = 0.3  # client per-request timeout; the fake /slow sleeps 2 s

#: (method, path, expect, weight, outcome). Weights sum to 100, so with the
#: 1000-slot schedule every 1000 consecutive requests hit each entry exactly
#: 10 x weight times.
NASTY: list[tuple[str, str, str, int, str]] = [
    ("GET", "/ok", "200", 40, "ok"),
    ("GET", "/chunked", "200", 10, "ok"),
    ("POST", "/created", "201", 10, "ok"),
    ("GET", "/redirect", "307", 5, "ok"),
    # wrong status that IS in the union of the workload's codes (200 ∈ {..})
    ("POST", "/ok", "201", 8, "unexpected_status"),
    # ... and the other way round (201 ∈ {..}): the old union check passed both
    ("GET", "/created", "200", 6, "unexpected_status"),
    ("GET", "/missing", "200", 3, "unexpected_status"),
    ("GET", "/e500", "200", 5, "server_error"),
    ("GET", "/e503", "200", 3, "server_error"),
    ("GET", "/slow", "200", 3, "timeout"),
    ("GET", "/drop", "200", 4, "connection"),
    ("GET", "/half", "200", 3, "connection"),
]
CLEAN = [e for e in NASTY if e[4] == "ok"]


def _mix(rows: list[tuple[str, str, str, int, str]]) -> list[dict[str, Any]]:
    return [
        {
            "method": m,
            "path": p,
            "body": '{"i": {i}}' if m == "POST" else "",
            "expect": x,
            "weight": w,
        }
        for m, p, x, w, _o in rows
    ]


def _expected(
    rows: list[tuple[str, str, str, int, str]], wl: lc.Workload, start: int, n: int
) -> dict[str, int]:
    outcome = {(m, p, lc.parse_expect(x)): o for m, p, x, _w, o in rows}
    want = dict.fromkeys(("ok", *lc.ERROR_CLASSES), 0)
    for e, c in wl.expected_counts(start, n).items():
        want[outcome[(e.method, e.target, e.expect)]] += c
    return want


def _got(stats: lc.RunStats) -> dict[str, int]:
    return {"ok": stats.ok, **stats.errors_by_class}


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """On uvloop, as the stand runs (its loop clock is the 1 ms one)."""
    uvloop = pytest.importorskip("uvloop")
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as r:
        return r.run(coro)


@pytest.fixture(scope="module")
def server_port() -> Iterator[int]:
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "harness_fake_server.py")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        yield int(proc.stdout.readline())
    finally:
        proc.terminate()
        proc.wait(10)


def _sub_ms(lat: list[float]) -> bool:
    """Latencies carry sub-millisecond information (not 1 ms-quantised)."""
    frac = [(x * 1e3) % 1.0 for x in lat]
    off_grid = sum(1 for f in frac if 1e-6 < f < 1 - 1e-6)
    distinct = len({round(x * 1e6) for x in lat})
    return off_grid >= 0.9 * len(lat) and distinct >= min(100, len(lat) // 2)


def test_schedule_is_per_request_and_exact() -> None:
    wl = lc.Workload.from_mix(_mix(NASTY))
    counts: dict[tuple[str, str, frozenset[int]], int] = {}
    for e, c in wl.expected_counts(0, 1000).items():
        counts[(e.method, e.target, e.expect)] = c
    assert sum(counts.values()) == 1000
    for m, p, x, w, _o in NASTY:
        assert counts[(m, p, lc.parse_expect(x))] == 10 * w
    # the expectation travels with the request, not as the workload's union
    raw, expect = wl.request(next(i for i in range(1000) if wl.path(i) == "/missing"))
    assert expect == frozenset({200}) and raw.startswith(b"GET /missing ")


def test_status_classification() -> None:
    s = lc.RunStats(planned=4)
    s.response(b"HTTP/1.1 201 Created\r\n\r\n", frozenset({201}), 0.001)
    s.response(b"HTTP/1.1 200 OK\r\n\r\n", frozenset({201}), 0.001)
    s.response(b"HTTP/1.1 502 Bad Gateway\r\n\r\n", frozenset({200}), 0.001)
    s.response(b"HTTP/1.1 404 Not Found\r\n\r\n", frozenset({200, 201}), 0.001)
    s.check()
    assert _got(s) == {
        "ok": 1,
        "unexpected_status": 2,
        "server_error": 1,
        "timeout": 0,
        "connection": 0,
    }
    with pytest.raises(AssertionError):
        lc.RunStats(planned=5, ok=4).check()  # a lost request is a harness bug


def test_closed_loop_counts_every_error_class_exactly(server_port: int) -> None:
    wl = lc.Workload.from_mix(_mix(NASTY))
    stats = _run(
        lc.closed_loop([("127.0.0.1", server_port)], wl, 0, 1000, 10, timeout=TIMEOUT)
    )
    assert stats.planned == 1000 and stats.completed == 1000
    assert _got(stats) == _expected(NASTY, wl, 0, 1000)
    assert set(stats.samples) == set(lc.ERROR_CLASSES)
    assert lc.exit_code(stats) == 1


def test_open_loop_counts_every_error_class_exactly(server_port: int) -> None:
    wl = lc.Workload.from_mix(_mix(NASTY))
    stats = _run(
        lc.open_loop(
            [("127.0.0.1", server_port)],
            wl,
            400,
            2.5,
            start=500,
            pool=64,
            timeout=TIMEOUT,
        )
    )
    assert stats.planned == 1000 and stats.completed == 1000
    assert _got(stats) == _expected(NASTY, wl, 500, 1000)
    assert lc.exit_code(stats) == 1
    res = lc.open_result(stats)
    assert res["errors"] == 1000 - stats.ok
    assert res["errors_by_class"]["timeout"] == 30


def test_clean_run_passes_with_sub_ms_latency(server_port: int) -> None:
    wl = lc.Workload.from_mix(_mix(CLEAN))
    stats = _run(
        lc.open_loop([("127.0.0.1", server_port)], wl, 500, 2.0, pool=32, timeout=5)
    )
    assert stats.errors == 0 and stats.ok == 1000
    assert lc.exit_code(stats) == 0
    assert _sub_ms(stats.latencies)
    q = lc.open_result(stats)
    assert q["latency_clock"] == "perf_counter"
    assert q["p50_ms"] != round(q["p50_ms"])  # reported below 1 ms resolution
    # control: the same latencies on a 1 ms clock fail the resolution check
    assert not _sub_ms([round(x, 3) for x in stats.latencies])

    closed = _run(
        lc.closed_loop([("127.0.0.1", server_port)], wl, 0, 1000, 8, timeout=5)
    )
    assert closed.errors == 0 and closed.ok == 1000
    assert _sub_ms(closed.latencies)


def test_cli_fails_the_run_on_unexpected_status(
    server_port: int, tmp_path: Path
) -> None:
    def cli(rows: list[tuple[str, str, str, int, str]], *args: str) -> tuple[int, dict]:
        f = tmp_path / "mix.json"
        f.write_text(json.dumps(_mix(rows)))
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchsuite.loadclient",
                "--port",
                str(server_port),
                "--mix",
                str(f),
                "--timeout",
                str(TIMEOUT),
                *args,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(HERE.parent)},
            timeout=120,
        )
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

    # one wrong status in an otherwise clean workload fails the run
    one_bad = [*CLEAN, ("POST", "/ok", "201", 1, "unexpected_status")]
    rc, out = cli(one_bad, "--n", "1000", "--conc", "4")
    want = _expected(one_bad, lc.Workload.from_mix(_mix(one_bad)), 0, 1000)
    assert rc == 1 and want["unexpected_status"] > 0
    assert out["errors_by_class"]["unexpected_status"] == want["unexpected_status"]
    assert out["ok"] + out["errors"] == out["planned"] == 1000

    rc, out = cli(NASTY, "--rate", "400", "--seconds", "1")
    assert rc == 1 and out["planned"] == out["completed"] == 400
    assert out["errors"] == 400 - out["ok"] > 0

    rc, out = cli(CLEAN, "--n", "800", "--conc", "4")
    assert rc == 0 and out["errors"] == 0 and out["ok"] == 800
