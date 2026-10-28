"""Keep-alive HTTP/1.1 load client of the ladder stand (``server_bench`` /
``split_bench``): closed loop (fixed concurrency) and open loop (fixed arrival
rate, coordinated-omission corrected).

Accounting contract (pinned by ``benchmarks/tests/test_harness_loadclient.py``
against a fake server with known failure rates):

* every PLANNED request ends in exactly one bucket: ``ok`` or one error
  class, so ``ok + sum(errors_by_class) == planned`` (``check()`` asserts it);
* error classes: ``unexpected_status`` (a non-5xx status the request did not
  expect), ``server_error`` (5xx), ``timeout`` (no full response within
  ``timeout`` seconds), ``connection`` (the connection dropped / reset before
  a full response);
* a status is checked against the expectation of THAT request (its workload
  entry), never against the union of the workload's codes;
* latency is timed with ``time.perf_counter`` (sub-microsecond), never with
  the event loop clock (uvloop's ``loop.time()`` has 1 ms resolution);
* a connection that timed out or dropped is closed and replaced, so neither
  loop shrinks its concurrency / pool after an error.

A run with ANY error is a failed run: :func:`exit_code` returns 1 and the
ladder scripts exit non-zero after printing their JSON.

Also a CLI against an already-running server (the harness self-test uses it)::

    python -m benchsuite.loadclient --port 8000 --mix workload.json \\
        (--n 1000 --conc 8 | --rate 200 --seconds 5) [--timeout 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

ERROR_CLASSES = ("unexpected_status", "server_error", "timeout", "connection")
#: error samples kept per class
SAMPLES = 3

_CL = re.compile(rb"content-length:\s*(\d+)")
_CHUNKED = re.compile(rb"transfer-encoding:[^\r\n]*chunked")


# --------------------------------------------------------------------------
# workloads


@dataclass(frozen=True)
class Entry:
    method: str
    target: str
    body: str
    expect: frozenset[int]


def parse_expect(spec: str | int) -> frozenset[int]:
    """``"201"`` / ``"201,307"`` / ``200`` -> accepted status codes."""
    return frozenset(int(c) for c in str(spec).split(",") if c.strip())


def schedule(entries: Sequence[dict[str, Any]], size: int = 1000) -> list[Entry]:
    """Deterministic smooth weighted round-robin over a workload's entries.

    Request ``i`` is ``schedule[i % size]``, so every run issues the same
    interleaved sequence (and the test can compute exact per-entry counts).
    """
    weights = [float(e["weight"]) for e in entries]
    total = sum(weights)
    current = [0.0] * len(entries)
    out = []
    for _ in range(size):
        for k, w in enumerate(weights):
            current[k] += w
        best = max(range(len(entries)), key=lambda k: current[k])
        current[best] -= total
        e = entries[best]
        out.append(
            Entry(
                e["method"].upper(),
                e["path"],
                e.get("body") or "",
                parse_expect(e.get("expect", "200")),
            )
        )
    return out


def _raw(method: str, target: str, body_t: str, i: int, headers: str) -> bytes:
    if not body_t:
        return (
            f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nUser-Agent: ladder\r\n"
            f"{headers}\r\n"
        ).encode()
    body = body_t.replace("{i}", str(i)).replace("{u}", os.urandom(6).hex()).encode()
    return (
        f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nUser-Agent: ladder\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        f"{headers}\r\n"
    ).encode() + body


class Workload:
    """Request ``i`` -> (raw request bytes, accepted statuses, path)."""

    def __init__(
        self,
        entries: Sequence[Entry],
        headers: Sequence[str] = (),
        target: Callable[[Entry, int], str] | None = None,
    ) -> None:
        if not entries:
            raise ValueError("empty workload")
        self.entries = list(entries)
        self.headers = "".join(f"{h}\r\n" for h in headers)
        self._target = target

    @classmethod
    def from_mix(
        cls, entries: Sequence[dict[str, Any]], headers: Sequence[str] = ()
    ) -> Workload:
        return cls(schedule(entries), headers)

    def entry(self, i: int) -> Entry:
        return self.entries[i % len(self.entries)]

    def path(self, i: int) -> str:
        e = self.entry(i)
        return self._target(e, i) if self._target else e.target

    def request(self, i: int) -> tuple[bytes, frozenset[int]]:
        e = self.entry(i)
        return _raw(e.method, self.path(i), e.body, i, self.headers), e.expect

    def expected_counts(self, start: int, n: int) -> dict[Entry, int]:
        """How many of requests ``start .. start+n-1`` hit each entry."""
        out: dict[Entry, int] = {}
        for i in range(start, start + n):
            e = self.entry(i)
            out[e] = out.get(e, 0) + 1
        return out


# --------------------------------------------------------------------------
# connection


class _Conn(asyncio.Protocol):
    """One keep-alive connection; one request in flight at a time."""

    def __init__(self) -> None:
        self.buf = b""
        self.waiter: asyncio.Future[bytes] | None = None
        self.closed = False
        self.t: asyncio.Transport

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.t = transport

    def _complete(self) -> int:
        head_end = self.buf.find(b"\r\n\r\n")
        if head_end < 0:
            return 0
        head = self.buf[:head_end].lower()
        if _CHUNKED.search(head):
            end = self.buf.find(b"\r\n0\r\n\r\n", head_end)
            if end < 0:
                if self.buf.startswith(b"0\r\n\r\n", head_end + 4):
                    return head_end + 9
                return 0
            return end + 7
        m = _CL.search(head)
        total = head_end + 4 + (int(m.group(1)) if m else 0)
        return total if len(self.buf) >= total else 0

    def data_received(self, data: bytes) -> None:
        self.buf += data
        total = self._complete()
        if total and self.waiter is not None and not self.waiter.done():
            resp, self.buf = self.buf[:total], self.buf[total:]
            w, self.waiter = self.waiter, None
            w.set_result(resp)

    def connection_lost(self, exc) -> None:  # type: ignore[override]
        self.closed = True
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_exception(ConnectionError(f"connection lost: {exc!r}"))

    async def send(self, raw: bytes) -> bytes:
        if self.closed:
            raise ConnectionError("connection already closed")
        self.waiter = asyncio.get_running_loop().create_future()
        self.t.write(raw)
        return await self.waiter

    def close(self) -> None:
        self.closed = True
        try:
            self.t.close()
        except Exception:  # noqa: BLE001
            pass


async def _connect(host: str, port: int) -> _Conn:
    loop = asyncio.get_running_loop()
    return (await loop.create_connection(_Conn, host, port))[1]


def status_of(resp: bytes) -> int:
    """``b"HTTP/1.1 201 Created..."`` -> 201 (0 when unparseable)."""
    try:
        return int(resp[9:12])
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# accounting


@dataclass
class RunStats:
    planned: int = 0
    ok: int = 0
    errors_by_class: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(ERROR_CLASSES, 0)
    )
    samples: dict[str, list[str]] = field(default_factory=dict)
    #: per OK request, seconds (perf_counter); open loop: from the SCHEDULED
    #: send time (CO-corrected), closed loop: from the actual send
    latencies: list[float] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def errors(self) -> int:
        return sum(self.errors_by_class.values())

    @property
    def completed(self) -> int:
        return self.ok + self.errors

    def error(self, cls: str, detail: str) -> None:
        self.errors_by_class[cls] += 1
        s = self.samples.setdefault(cls, [])
        if len(s) < SAMPLES:
            s.append(detail[:200])

    def response(self, resp: bytes, expect: frozenset[int], latency: float) -> None:
        st = status_of(resp)
        self.status_counts[st] = self.status_counts.get(st, 0) + 1
        if st in expect:
            self.ok += 1
            self.latencies.append(latency)
        elif st >= 500:
            self.error("server_error", resp[:200].decode("latin-1"))
        else:
            self.error(
                "unexpected_status",
                f"expected {sorted(expect)}: " + resp[:200].decode("latin-1"),
            )

    def merge(self, other: RunStats) -> None:
        self.planned += other.planned
        self.ok += other.ok
        for k, v in other.errors_by_class.items():
            self.errors_by_class[k] += v
        for k, s in other.samples.items():
            mine = self.samples.setdefault(k, [])
            mine.extend(s[: SAMPLES - len(mine)])
        self.latencies.extend(other.latencies)
        for st, c in other.status_counts.items():
            self.status_counts[st] = self.status_counts.get(st, 0) + c

    def check(self) -> None:
        """Every planned request is accounted for exactly once."""
        if self.completed != self.planned:
            raise AssertionError(
                f"harness accounting: ok {self.ok} + errors {self.errors} "
                f"!= planned {self.planned}"
            )

    def quantiles(self) -> dict[str, float | None]:
        lat = sorted(self.latencies)
        if not lat:
            return dict.fromkeys(("p50_ms", "p90_ms", "p99_ms", "p999_ms", "max_ms"))

        def q(x: float) -> float:
            return round(lat[min(len(lat) - 1, int(x * len(lat)))] * 1e3, 4)

        return {
            "p50_ms": q(0.50),
            "p90_ms": q(0.90),
            "p99_ms": q(0.99),
            "p999_ms": q(0.999),
            "max_ms": round(lat[-1] * 1e3, 4),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "completed": self.completed,
            "ok": self.ok,
            "errors": self.errors,
            "errors_by_class": dict(self.errors_by_class),
            "error_samples": {k: v for k, v in self.samples.items() if v},
            "status_counts": {str(k): v for k, v in sorted(self.status_counts.items())},
            "latency_clock": "perf_counter",
        }


def exit_code(*stats: RunStats) -> int:
    """1 when any run had an error (the run is invalid), else 0."""
    return 1 if any(s.errors for s in stats) else 0


# --------------------------------------------------------------------------
# loops


Endpoint = tuple[str, int]


class _Slot:
    """A (lazily connected) connection to one endpoint, replaced after a
    timeout / drop so the loop keeps its concurrency."""

    def __init__(self, ep: Endpoint) -> None:
        self.ep = ep
        self.conn: _Conn | None = None

    async def exchange(
        self,
        raw: bytes,
        expect: frozenset[int],
        stats: RunStats,
        t_ref: float,
        timeout: float,
    ) -> None:
        try:
            if self.conn is None or self.conn.closed:
                self.conn = await asyncio.wait_for(_connect(*self.ep), timeout)
            resp = await asyncio.wait_for(self.conn.send(raw), timeout)
        except TimeoutError:
            self.drop()
            stats.error("timeout", f"no response within {timeout} s")
            return
        except (ConnectionError, OSError) as exc:
            self.drop()
            stats.error("connection", repr(exc))
            return
        stats.response(resp, expect, time.perf_counter() - t_ref)
        if b"\r\nconnection: close" in resp[: resp.find(b"\r\n\r\n")].lower():
            self.drop()

    def drop(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


async def closed_loop(
    endpoints: Sequence[Endpoint],
    workload: Workload,
    start: int,
    n: int,
    conc: int,
    *,
    route: Callable[[int], int] | None = None,
    timeout: float = 30.0,
) -> RunStats:
    """``conc`` workers, each with one connection per endpoint, issue
    requests ``start .. start + (n // conc) * conc - 1`` back to back."""
    per = n // conc
    stats = RunStats(planned=per * conc)
    slots = [[_Slot(ep) for ep in endpoints] for _ in range(conc)]

    async def worker(k: int) -> None:
        base = start + k * per
        for i in range(base, base + per):
            raw, expect = workload.request(i)
            slot = slots[k][route(i) if route else 0]
            await slot.exchange(raw, expect, stats, time.perf_counter(), timeout)

    w0 = time.perf_counter()
    await asyncio.gather(*(worker(k) for k in range(conc)))
    stats.elapsed = time.perf_counter() - w0
    for row in slots:
        for s in row:
            s.drop()
    stats.check()
    return stats


async def open_loop(
    endpoints: Sequence[Endpoint],
    workload: Workload,
    rate: float,
    seconds: float,
    *,
    start: int = 0,
    pool: int = 256,
    route: Callable[[int], int] | None = None,
    timeout: float = 30.0,
) -> RunStats:
    """Open-loop load at a FIXED arrival rate (wrk2-style).

    Request ``i`` is scheduled at ``t0 + i / rate`` regardless of how fast
    earlier ones complete; its latency counts from that SCHEDULED time
    (coordinated-omission corrected: waiting for a free connection because
    the server fell behind is latency). Every task's outcome is recorded —
    nothing is dropped with a finished task.
    """
    free: list[asyncio.Queue[_Slot]] = []
    for ep in endpoints:
        q: asyncio.Queue[_Slot] = asyncio.Queue()
        for _ in range(pool):
            s = _Slot(ep)
            s.conn = await _connect(*ep)
            q.put_nowait(s)
        free.append(q)
    total = int(rate * seconds)
    stats = RunStats(planned=total)
    tasks: list[asyncio.Task[None]] = []

    async def one(i: int, sched: float) -> None:
        q = free[route(i) if route else 0]
        slot = await q.get()
        try:
            raw, expect = workload.request(i)
            await slot.exchange(raw, expect, stats, sched, timeout)
        finally:
            q.put_nowait(slot)

    t0 = time.perf_counter() + 0.05
    for k in range(total):
        sched = t0 + k / rate
        delay = sched - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.ensure_future(one(start + k, sched)))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    stats.elapsed = time.perf_counter() - t0
    for r in results:  # a harness bug, not a server error: never swallow it
        if isinstance(r, BaseException):
            raise r
    for q in free:
        while not q.empty():
            q.get_nowait().drop()
    stats.check()
    return stats


def open_result(stats: RunStats) -> dict[str, Any]:
    return {
        "achieved_rps": stats.completed / stats.elapsed if stats.elapsed else None,
        **stats.quantiles(),
        **stats.as_dict(),
    }


# --------------------------------------------------------------------------
# CLI


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--mix", required=True)
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--n", type=int, default=1000)
    p.add_argument("--conc", type=int, default=8)
    p.add_argument("--rate", type=float, default=0)
    p.add_argument("--seconds", type=float, default=5)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--pool", type=int, default=64)
    a = p.parse_args(argv)
    with open(a.mix) as fh:
        wl = Workload.from_mix(json.load(fh), a.header)
    ep = [(a.host, a.port)]
    if a.rate:
        stats = asyncio.run(
            open_loop(ep, wl, a.rate, a.seconds, pool=a.pool, timeout=a.timeout)
        )
        out = {"mode": "open", "rate": a.rate, **open_result(stats)}
    else:
        stats = asyncio.run(closed_loop(ep, wl, 0, a.n, a.conc, timeout=a.timeout))
        out = {"mode": "closed", **stats.as_dict()}
    print(json.dumps(out), flush=True)
    return exit_code(stats)


if __name__ == "__main__":
    sys.exit(main())
