"""Repeated Oracle write-latency A/B with delivery and CPU observations.

Only the isolated qualification app is switched; its original Compose selection
is restored in finally. Both variants mount the same benchmark code and keep
all original quotas/security configuration. No image builds or migrations.
Retired synthetic idempotency keys are removed by exact request prefix between
cells, keeping the fixed security Redis capacity out of the A/B comparison.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import resource
import signal
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from . import outbox_delivery_limit as limit
    from .oracle_perf import Experiment
else:
    import outbox_delivery_limit as limit
    from oracle_perf import Experiment


class CpuProbe:
    snapshot = Experiment.snapshot
    deltas = Experiment.deltas


def host_cpu():
    fields = [
        int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    ]
    # guest time is already included in user/nice; do not count it twice.
    return sum(fields) - fields[3] - fields[4]


def repeated_summary(records):
    result = []
    for rate in sorted({r["rate"] for r in records}):
        sides = {}
        for variant in ("before", "after"):
            rr = [r for r in records if r["rate"] == rate and r["variant"] == variant]
            sides[variant] = {
                "windows": len(rr),
                "p99_ms_windows": [r["p99_ms"] for r in rr],
                "p99_ms_median": statistics.median(r["p99_ms"] for r in rr),
                "p99_ms_min": min(r["p99_ms"] for r in rr),
                "p99_ms_max": max(r["p99_ms"] for r in rr),
                "goodput_rps_median": statistics.median(r["goodput_rps"] for r in rr),
                "max_pending_windows": [r["max_pending"] for r in rr],
                "errors": sum(r["errors"] for r in rr),
            }
        by_round = {
            variant: {
                r["round"]: r
                for r in records
                if r["rate"] == rate and r["variant"] == variant
            }
            for variant in ("before", "after")
        }
        pairs = [
            {
                "round": rnd,
                "before_ms": by_round["before"][rnd]["p99_ms"],
                "after_ms": by_round["after"][rnd]["p99_ms"],
                "ratio": by_round["after"][rnd]["p99_ms"]
                / by_round["before"][rnd]["p99_ms"],
            }
            for rnd in sorted(by_round["before"].keys() & by_round["after"].keys())
        ]
        result.append(
            {
                "rate": rate,
                **sides,
                "median_p99_ratio_after_before": sides["after"]["p99_ms_median"]
                / sides["before"]["p99_ms_median"],
                "paired_p99": pairs,
                "median_paired_p99_ratio": statistics.median(p["ratio"] for p in pairs)
                if pairs
                else None,
            }
        )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", required=True, type=Path)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--selected", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--rates", default="200,250,300")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--warmup-seconds", type=int, default=15)
    p.add_argument("--settle", type=int, default=10)
    p.add_argument("--before", default="sandloom-perf:cb278f8-selected")
    p.add_argument("--after", default="sandloom-release:f21f617c")
    a = p.parse_args()
    rates = [float(v) for v in a.rates.split(",")]
    if min(rates) <= 0 or a.seconds < 5 or a.rounds < 1:
        p.error("positive rates, rounds >= 1, seconds >= 5 required")
    a.out.mkdir(parents=True, mode=0o700, exist_ok=True)
    if (a.out / "results.jsonl").exists():
        p.error("use a fresh result directory")
    # Settings consumed by one_rate / Probe.
    a.host, a.port, a.pool = "127.0.0.1", 18222, 128
    a.sample, a.drain_timeout = 2.0, 180
    a.database, a.db_user = "qualification", "qualification_owner"
    a.channel, a.label = limit.EVENT_CHANNEL, "latency-ab"
    a.bearer_file = None
    sys.path.insert(0, str(a.source / "benchmarks"))
    from benchsuite import loadclient as lc

    with open("/tmp/sandloom-oracle-perf.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = a.selected.read_text().strip()
        os.environ["SANDLOOM_QUALIFICATION_ENV_FILES"] = str(a.fixture / ".state/https.env")
        os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = original
        sys.path.insert(0, str(a.fixture))
        import check
        import run

        original_env, original_data = limit.app_environment()
        assert original_data["HostConfig"]["NanoCpus"] == 2_000_000_000
        assert original_data["HostConfig"]["Memory"] == 1_073_741_824
        original_image = original_data["Image"]
        images = {}
        for variant, image in [("before", a.before), ("after", a.after)]:
            d = json.loads(limit.command(["docker", "image", "inspect", image]))[0]
            images[variant] = {"tag": image, "id": d["Id"]}
        (a.out / "images.json").write_text(json.dumps(images, indent=2) + "\n")
        critical = [
            "INFRA",
            "DATABASE_URL",
            "DATABASE_SYSTEM_URL",
            "SECURITY_CACHE_INSTANCE",
            "PRODUCTION_PROFILE",
            "WEB_CONCURRENCY",
            "AUTH_REVOCATION_FAIL_CLOSED",
            "DB_POOL_SIZE",
            "DB_MAX_OVERFLOW",
            "CONTRACT_DB_POOL_SIZE",
            "CONTRACT_DB_MAX_OVERFLOW",
            "OUTBOX_RELAY_ENABLED",
            "SANDLOOM_ACCEPTED_RISKS",
        ]
        override = a.out / "variant.private.yml"
        probe_cpu = CpuProbe()
        probe = limit.Probe(a, original_env)
        records = []

        def save(record):
            with (a.out / "results.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")
            (a.out / "results.jsonl").chmod(0o600)
            print(
                json.dumps(
                    {
                        k: record[k]
                        for k in [
                            "round",
                            "variant",
                            "rate",
                            "goodput_rps",
                            "p99_ms",
                            "max_pending",
                            "errors",
                            "sustainable",
                        ]
                    }
                ),
                flush=True,
            )

        def retire(prefix):
            # Each key contains the unmodified unique Idempotency-Key. Refuse
            # deletion outside the explicit diagnostic scope and exact prefix.
            import re

            assert re.fullmatch(r"outbox-limit-[0-9a-f]{10}", prefix)
            container = limit.PROJECT + "-security-1"
            raw = limit.command(
                [
                    "docker",
                    "exec",
                    container,
                    "redis-cli",
                    "--scan",
                    "--pattern",
                    "*:" + prefix + "-*",
                ]
            )
            keys = raw.splitlines()
            assert all(
                re.fullmatch(
                    r"(?:.*:)?idem:bench\.dw7:[^\s]*:" + re.escape(prefix) + r"-[0-9]+",
                    k,
                )
                for k in keys
            )
            if keys:
                commands = []
                for i in range(0, len(keys), 128):
                    words = ["UNLINK", *keys[i : i + 128]]
                    commands.append(
                        "*"
                        + str(len(words))
                        + "\r\n"
                        + "".join(
                            "$" + str(len(w.encode())) + "\r\n" + w + "\r\n"
                            for w in words
                        )
                    )
                reply = limit.command(
                    ["docker", "exec", "-i", container, "redis-cli", "--pipe"],
                    input="".join(commands),
                )
                if not re.search(r"errors: 0, replies: [0-9]+", reply):
                    raise RuntimeError("synthetic-key cleanup did not confirm success")
            return len(keys)

        def wait_quiet():
            deadline = time.monotonic() + 180
            while True:
                x = probe.sample()
                time.sleep(2)
                y = probe.sample()
                if (
                    x["pending"] == y["pending"] == 0
                    and x["xlen"] == y["xlen"]
                    and x["total"] == y["total"]
                ):
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError("outbox or stream has not settled")

        def interrupted(_signum, _frame):
            raise KeyboardInterrupt("interrupted; restoring original deployment")

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)

        try:
            for rnd in range(1, a.rounds + 1):
                order = ["before", "after"] if rnd % 2 else ["after", "before"]
                # Both sides see the same rate order in each paired round.
                offset = (rnd - 1) % len(rates)
                ordered_rates = rates[offset:] + rates[:offset]
                for variant in order:
                    wait_quiet()
                    override.write_text(
                        json.dumps(
                            {
                                "services": {
                                    "app": {
                                        "image": images[variant]["tag"],
                                        "volumes": [
                                            str(a.source / "benchmarks")
                                            + ":/benchmarks:ro"
                                        ],
                                    }
                                }
                            }
                        )
                    )
                    override.chmod(0o600)
                    os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = (
                        original + ":" + str(override)
                    )
                    run.compose(
                        "up",
                        "-d",
                        "--no-build",
                        "--no-deps",
                        "--force-recreate",
                        "app",
                        capture_output=True,
                    )
                    token = run.token()
                    check.wait_ready(token, timeout=90)
                    env, data = limit.app_environment()
                    assert data["Image"] == images[variant]["id"]
                    assert data["HostConfig"]["NanoCpus"] == 2_000_000_000
                    assert data["HostConfig"]["Memory"] == 1_073_741_824
                    assert all(env.get(k) == original_env.get(k) for k in critical)
                    assert check.request("/s5plan/5001", token)[0] == 404
                    # Identical warmup after each image restart; not measured.
                    seconds = a.seconds
                    a.seconds = a.warmup_seconds
                    warmup = limit.one_rate(a, probe, lc, token, 150)
                    a.seconds = seconds
                    assert warmup["errors"] == 0 and warmup["drain_s"] is not None
                    wait_quiet()
                    retire(warmup["request_prefix"])
                    print(f"Round {rnd} {variant}: warmed up", flush=True)
                    for rate in ordered_rates:
                        observations = {}

                        async def observed_open_loop(
                            *args, _observations=observations, **kwargs
                        ):
                            before = probe_cpu.snapshot()
                            host0, cpu0 = (
                                host_cpu(),
                                resource.getrusage(resource.RUSAGE_SELF),
                            )
                            t0 = time.monotonic()
                            stats = await lc.open_loop(*args, **kwargs)
                            elapsed = time.monotonic() - t0
                            cpu1, host1 = (
                                resource.getrusage(resource.RUSAGE_SELF),
                                host_cpu(),
                            )
                            after = probe_cpu.snapshot()
                            _observations.update(
                                containers=probe_cpu.deltas(before, after, elapsed),
                                generator_cpu_cores=(
                                    (cpu1.ru_utime + cpu1.ru_stime)
                                    - (cpu0.ru_utime + cpu0.ru_stime)
                                )
                                / elapsed,
                                host_busy_cores=(host1 - host0)
                                / os.sysconf("SC_CLK_TCK")
                                / elapsed,
                                load_observation_seconds=elapsed,
                            )
                            return stats

                        observed = SimpleNamespace(
                            Workload=lc.Workload,
                            Entry=lc.Entry,
                            open_loop=observed_open_loop,
                            open_result=lc.open_result,
                        )
                        record = limit.one_rate(a, probe, observed, token, rate)
                        record.update(
                            round=rnd,
                            variant=variant,
                            image=images[variant]["id"],
                            at=datetime.now(UTC).isoformat(),
                            cpu_observations=observations,
                        )
                        records.append(record)
                        save(record)
                        if (
                            record["errors"]
                            or record["monitor_error"]
                            or record["drain_s"] is None
                            or record["dead_delta"]
                            or record["dlq_delta"]
                        ):
                            raise RuntimeError("invalid window; see retained results")
                        wait_quiet()
                        record["retired_idempotency_keys"] = retire(
                            record["request_prefix"]
                        )
                        time.sleep(a.settle)
            (a.out / "summary.json").write_text(
                json.dumps(repeated_summary(records), indent=2) + "\n"
            )
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = original
            run.compose(
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "app",
                capture_output=True,
            )
            check.wait_ready(run.token(), timeout=90)
            _, restored = limit.app_environment()
            assert restored["Image"] == original_image
            assert restored["HostConfig"]["NanoCpus"] == 2_000_000_000
            assert restored["HostConfig"]["Memory"] == 1_073_741_824
            print("Original image and limits restored", flush=True)


if __name__ == "__main__":
    main()
