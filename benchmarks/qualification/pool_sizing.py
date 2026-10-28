"""ABBA pool sizing comparison with fixed image, workers, CPU and workload."""

from __future__ import annotations

import json
import threading
import time

import check
from load import run_wrk
from run import STATE, compose, token
from tune import configure, cpu_stats, profiles


def main():
    override = STATE / "pool-sizing.compose.json"
    report = {
        "passed": False,
        "samples": [],
        "scope": "server-local ORM, 2 workers, 2 CPU, c128, 20s",
    }
    try:
        for index, (size, overflow) in enumerate(((5, 5), (10, 10), (10, 10), (5, 5))):
            profile = dict(
                profiles()["baseline"],
                cpus=2,
                connections=128,
                pool=size,
                overflow=overflow,
            )
            container, image, cpu_max = configure(profile, override)
            jwt = token()
            check.wait_ready(jwt)
            run_wrk(jwt, "read", 128, 5, f"-pool-{index}-warm")
            observations = []
            stop = threading.Event()

            def observe(stop, observations):
                while not stop.wait(5):
                    observations.append(
                        check.sql(
                            "SELECT json_build_object('connections',count(*),"
                            "'active',count(*) FILTER (WHERE state='active'),"
                            "'lock_waiters',count(*) FILTER (WHERE wait_event_type='Lock')) "
                            "FROM pg_stat_activity WHERE usename='qualification_app'"
                        )
                    )

            observer = threading.Thread(target=observe, args=(stop, observations))
            observer.start()
            before = cpu_stats(container)
            started = time.monotonic()
            try:
                sample = run_wrk(jwt, "read", 128, 20, f"-pool-{index}")
            finally:
                stop.set()
                observer.join(timeout=15)
                assert not observer.is_alive(), "database observer stalled"
            after = cpu_stats(container)
            sample.update(
                pool_size=size,
                max_overflow=overflow,
                image=image,
                cpu_max=cpu_max,
                elapsed=time.monotonic() - started,
                cpu_delta={k: after[k] - before[k] for k in after},
                postgres=[json.loads(s) for s in observations],
            )
            report["samples"].append(sample)
            print(json.dumps(sample), flush=True)
            check.expect("pool-sizing isolation", "/s5/5001", jwt, 404)
        report["passed"] = all(
            not (s["socket_errors"] or s["http_errors"] or s["invalid_bodies"])
            for s in report["samples"]
        )
    finally:
        compose("up", "-d", "--no-deps", "--force-recreate", "app", capture_output=True)
        check.wait_ready(token())
        STATE.joinpath("pool-sizing.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        override.unlink(missing_ok=True)
    if not report["passed"]:
        raise SystemExit("pool comparison recorded errors")


if __name__ == "__main__":
    main()
