"""Load generators: oha, wrk (closed loop) and wrk2 (open loop, fixed rate).

Each generator turns a :class:`LoadSpec` into one measurement dict with the
same keys (``rps``, ``p50_ms`` … ``p999_ms``, ``ok``, ``errors``,
``error_detail``, ``elapsed_s``), so the runner and the renderer never care
which tool produced a row.

* ``oha`` / ``wrk`` — closed loop: N connections send back-to-back, the
  maximum throughput the server sustains. Their latency suffers from
  coordinated omission (a slow server slows the client down).
* ``wrk2`` — open loop at a fixed rate (``-R``): requests are scheduled on a
  timeline and latency is measured from the INTENDED send time (coordinated
  omission corrected, HdrHistogram). The honest latency comparison. wrk2 uses
  the first ~10 s (+5 ms per connection per thread) to calibrate and then
  resets its histograms; the runner adds that time on top of the measured
  window, so it also serves as wrk2's warm-up.

Errors are surfaced for every tool: oha's per-status distribution, wrk's
``errors.status`` (every response >= 400) plus connect/read/write/timeout.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchsuite import common

HOST = "127.0.0.1"
S4_BODY = '{"name":"bench","value":1}'
EXPECTED_STATUS = {"s1": 200, "s2": 200, "s3": 200, "s4": 201}
LUA_SCRIPT = Path(__file__).parent / "lua" / "bench.lua"
#: wrk/wrk2 request timeout (their 2 s default would turn a slow DB tail into
#: "timeout" errors; oha has no timeout by default).
WRK_TIMEOUT = "10s"
CLOSED_LOOP = ("oha", "wrk")
ALL = ("oha", "wrk", "wrk2")


@dataclass(frozen=True)
class LoadSpec:
    port: int
    scenario: str
    token: str
    seconds: float
    connections: int
    threads: int
    client_cpus: str | None = None
    rate: int | None = None  # wrk2 only


def pin(cpus: str | None) -> list[str]:
    """``taskset -c <cpus>`` prefix (Linux only; ignored elsewhere)."""
    if not cpus or not os.uname().sysname == "Linux" or not shutil.which("taskset"):
        return []
    return ["taskset", "-c", cpus]


def cpu_list_size(cpus: str | None) -> int | None:
    """Number of CPUs in a taskset list like ``1-3,5``."""
    if not cpus:
        return None
    total = 0
    for part in cpus.split(","):
        lo, _, hi = part.partition("-")
        total += (int(hi) - int(lo) + 1) if hi else 1
    return total


def _version(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0] if text else "unknown"


class LoadGen:
    name = ""

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def available(self) -> bool:
        return bool(shutil.which(self.binary) or Path(self.binary).is_file())

    def version(self) -> str:
        return _version([self.binary, "--version"])

    def run(self, spec: LoadSpec) -> dict[str, Any]:
        raise NotImplementedError


class Oha(LoadGen):
    name = "oha"

    def argv(self, spec: LoadSpec) -> list[str]:
        base = f"http://{HOST}:{spec.port}"
        argv = [
            self.binary, "--no-tui", "--output-format", "json",
            "-z", f"{spec.seconds}s", "-c", str(spec.connections),
            "--worker-threads", str(spec.threads), "--disable-compression",
        ]  # fmt: skip
        if spec.scenario == "s1":
            return [*argv, f"{base}/s1"]
        if spec.scenario == "s2":
            return [*argv, "-H", f"Authorization: Bearer {spec.token}", f"{base}/s2"]
        if spec.scenario == "s3":
            # ids uniformly from 1..9999 (all seeded).
            return [*argv, "--rand-regex-url", f"{base}/s3/[1-9][0-9]{{0,3}}"]
        if spec.scenario == "s4":
            return [
                *argv, "-m", "POST", "-T", "application/json", "-d", S4_BODY,
                f"{base}/s4",
            ]  # fmt: skip
        raise ValueError(spec.scenario)

    def run(self, spec: LoadSpec) -> dict[str, Any]:
        out = subprocess.run(
            [*pin(spec.client_cpus), *self.argv(spec)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if out.returncode != 0:
            raise RuntimeError(f"oha failed ({out.returncode}): {out.stderr[-2000:]}")
        return self.parse(json.loads(out.stdout), EXPECTED_STATUS[spec.scenario])

    @staticmethod
    def parse(report: dict[str, Any], expected: int) -> dict[str, Any]:
        statuses = {
            str(k): int(v) for k, v in report.get("statusCodeDistribution", {}).items()
        }
        ok = statuses.get(str(expected), 0)
        bad_status = sum(v for k, v in statuses.items() if k != str(expected))
        errs = {
            k: int(v)
            for k, v in report.get("errorDistribution", {}).items()
            if "deadline" not in k  # in-flight requests cut by -z are not errors
        }
        elapsed = float(report["summary"]["total"])
        pct = report.get("latencyPercentiles", {})

        def ms(key: str) -> float | None:
            val = pct.get(key)
            return None if val is None else round(float(val) * 1000, 3)

        return {
            "rps": round(ok / elapsed, 1) if elapsed else 0.0,
            "p50_ms": ms("p50"),
            "p90_ms": ms("p90"),
            "p99_ms": ms("p99"),
            "p999_ms": ms("p99.9"),
            "ok": ok,
            "errors": bad_status + sum(errs.values()),
            "error_detail": {
                **{f"HTTP {k}": v for k, v in statuses.items() if k != str(expected)},
                **errs,
            },
            "elapsed_s": round(elapsed, 3),
        }


class Wrk(LoadGen):
    """wrk 4.x — closed loop. Also the base of :class:`Wrk2` (same script)."""

    name = "wrk"
    #: Env var carrying the pinned commit the binary was built from (CI).
    sha_env = "WRK_SHA"

    def version(self) -> str:
        # wrk has no --version; -v prints "wrk <version> [epoll] ..." (the
        # version is empty when built from a tarball), so add the pinned SHA.
        first = _version([self.binary, "-v"]).split(" Copyright")[0]
        sha = os.environ.get(self.sha_env, "")
        first = " ".join(first.split())
        return f"{self.name} @{sha[:12]} ({first})" if sha else first

    def duration_s(self, spec: LoadSpec) -> int:
        return max(1, round(spec.seconds))

    def argv(self, spec: LoadSpec) -> list[str]:
        threads = max(1, min(spec.threads, spec.connections))
        return [
            self.binary, "-t", str(threads), "-c", str(spec.connections),
            "-d", f"{self.duration_s(spec)}s", "--timeout", WRK_TIMEOUT,
            "-s", str(LUA_SCRIPT), f"http://{HOST}:{spec.port}/{spec.scenario}",
        ]  # fmt: skip

    def run(self, spec: LoadSpec) -> dict[str, Any]:
        env = {
            **os.environ,
            "BENCH_SCENARIO": spec.scenario,
            "BENCH_TOKEN": spec.token,
            "BENCH_SEED_ROWS": str(common.SEED_ROWS),
        }
        out = subprocess.run(
            [*pin(spec.client_cpus), *self.argv(spec)],
            capture_output=True, text=True, check=False, env=env,
        )  # fmt: skip
        line = next(
            (ln for ln in out.stdout.splitlines() if ln.startswith("WRKJSON ")), None
        )
        if out.returncode != 0 or line is None:
            raise RuntimeError(
                f"{self.name} failed ({out.returncode}): "
                f"{(out.stderr or out.stdout)[-2000:]}"
            )
        return self.parse(json.loads(line[len("WRKJSON ") :]))

    @staticmethod
    def parse(report: dict[str, Any]) -> dict[str, Any]:
        err = {k: int(v) for k, v in report["errors"].items()}
        elapsed = report["duration_us"] / 1e6
        ok = max(0, int(report["requests"]) - err["status"])
        lat = report["latency_us"]

        def ms(key: str) -> float | None:
            val = lat.get(key)
            return None if val is None else round(float(val) / 1000, 3)

        detail = {
            ("HTTP >=400" if k == "status" else k): v for k, v in err.items() if v
        }
        return {
            "rps": round(ok / elapsed, 1) if elapsed else 0.0,
            "p50_ms": ms("p50"),
            "p90_ms": ms("p90"),
            "p99_ms": ms("p99"),
            "p999_ms": ms("p99.9"),
            "ok": ok,
            "errors": sum(err.values()),
            "error_detail": detail,
            "elapsed_s": round(elapsed, 3),
        }


class Wrk2(Wrk):
    """wrk2 — open loop at ``spec.rate`` req/s, CO-corrected latency."""

    name = "wrk2"
    sha_env = "WRK2_SHA"

    @staticmethod
    def calibration_s(spec: LoadSpec) -> int:
        """wrk2's calibration window (CALIBRATE_DELAY_MS + 5 ms/conn/thread)."""
        per_thread = math.ceil(spec.connections / max(1, spec.threads))
        return 10 + math.ceil(per_thread * 5 / 1000) + 1

    def duration_s(self, spec: LoadSpec) -> int:
        return self.calibration_s(spec) + max(1, round(spec.seconds))

    def argv(self, spec: LoadSpec) -> list[str]:
        if not spec.rate:
            raise ValueError("wrk2 needs a rate")
        argv = super().argv(spec)
        return [*argv[:-1], "-R", str(int(spec.rate)), argv[-1]]

    def run(self, spec: LoadSpec) -> dict[str, Any]:
        row = super().run(spec)
        # Requests completed over the whole run (calibration included) / time:
        # the achieved rate, to compare with the target.
        row["target_rps"] = int(spec.rate or 0)
        row["achieved_rps"] = row["rps"]
        return row


def make(name: str, binaries: dict[str, str]) -> LoadGen:
    cls = {"oha": Oha, "wrk": Wrk, "wrk2": Wrk2}[name]
    return cls(binaries[name])
