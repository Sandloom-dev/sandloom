"""Bounded two-tenant correctness soak with repeated isolated Docker failures."""

from __future__ import annotations

import argparse
import base64
import json
import signal
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import check
from run import STATE, compose, token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if not 180 <= args.seconds <= 14400:
        parser.error("use 180..14400 seconds (the synthetic JWTs last six hours)")
    STATE.joinpath("soak.json").unlink(missing_ok=True)
    jwts = [token(), token("5e000000-0000-4000-8000-00000000000b")]
    blocked = token()
    claims = json.loads(base64.urlsafe_b64decode(blocked.split(".")[1] + "=="))
    block_key = "sandloom:auth:blocked:" + claims["sub"]
    compose(
        "exec",
        "-T",
        "security",
        "redis-cli",
        "SET",
        block_key,
        "soak",
        capture_output=True,
    )
    prefix = "soak-" + uuid.uuid4().hex[:12] + "-"
    before = check.counts()
    counters = Counter()
    failures = []
    faults = []
    lock = threading.Lock()
    stop = threading.Event()
    cancelled = threading.Event()

    def cancel(_signal, _frame):
        cancelled.set()
        stop.set()

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)
    unstable_until = 0.0
    started = time.monotonic()
    deadline = started + args.seconds

    def observe(kind, code, expected, body, unstable):
        with lock:
            counters[f"{kind}:{code}"] += 1
            if (
                code != expected
                and not (code == 0 or code >= 500)
                or (code != expected and not unstable)
            ):
                failures.append(
                    {"kind": kind, "status": code, "expected": expected, "body": body}
                )
                stop.set()

    def worker(index):
        jwt = jwts[index % 2]
        own = 1 if index % 2 == 0 else 5001
        foreign = 5001 if index % 2 == 0 else 1
        sequence = 0
        pending = None
        last = None
        while not stop.is_set() and time.monotonic() < deadline:
            for kind, path, expected in [
                ("own", f"/s5/{own}", 200),
                ("foreign", f"/s5/{foreign}", 404),
                ("blocked", "/s5/1", 401),
            ]:
                unstable = time.monotonic() < unstable_until
                code, body = check.request(path, blocked if kind == "blocked" else jwt)
                unstable = unstable or time.monotonic() < unstable_until
                observe(kind, code, expected, body, unstable)
                if kind == "own" and code == 200 and body.get("id") != own:
                    with lock:
                        failures.append({"kind": "wrong tenant body", "body": body})
                    stop.set()
            sequence += 1
            if pending is None and sequence % 5 == 0:
                pending = {
                    "name": f"{prefix}{index}-{sequence}",
                    "value": 7,
                    "key": uuid.uuid4().hex,
                }
            if pending is not None:
                unstable = time.monotonic() < unstable_until
                code, result = check.request("/dw/6", jwt, pending)
                unstable = unstable or time.monotonic() < unstable_until
                observe("command", code, 201, result, unstable)
                if code == 201:
                    if last and last[0] == pending["key"] and last[1] != result["id"]:
                        with lock:
                            failures.append({"kind": "duplicate command effect"})
                        stop.set()
                    # Half the successes are replayed once, including across failures.
                    if last and last[0] == pending["key"]:
                        pending = None
                        last = None
                    else:
                        last = (pending["key"], result["id"])
            stop.wait(0.1)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(8)]
            fault_index = 0
            while not stop.is_set() and time.monotonic() < deadline:
                if stop.wait(min(30, max(0, deadline - time.monotonic()))):
                    break
                if time.monotonic() >= deadline:
                    break
                service = ["cache", "security", "db", "app"][fault_index % 4]
                unstable_until = time.monotonic() + 120
                begin = time.monotonic()
                try:
                    compose("kill", "-s", "SIGKILL", service, capture_output=True)
                    stop.wait(4)
                finally:
                    compose("up", "-d", "--no-deps", service, capture_output=True)
                check.wait_ready(jwts[0])
                unstable_until = time.monotonic() + 10
                fault = {
                    "service": service,
                    "at_seconds": round(begin - started, 3),
                    "recovery_seconds": round(time.monotonic() - begin, 3),
                }
                faults.append(fault)
                print(json.dumps(fault), flush=True)
                fault_index += 1
            stop.set()
            for future in futures:
                future.result()
        if cancelled.is_set():
            raise InterruptedError("soak cancelled; no passing report written")
        if failures:
            raise AssertionError(failures[:5])
        assert {f["service"] for f in faults} == {"cache", "security", "db", "app"}, (
            faults
        )
        check.wait_ready(jwts[0])
        for _ in range(60):
            if (
                check.sql(
                    "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL"
                )
                == "0"
            ):
                break
            time.sleep(1)
        else:
            raise AssertionError("outbox failed to drain")
        after = check.counts()
        delta = [
            int(b) - int(a)
            for a, b in zip(before.split("|"), after.split("|"), strict=True)
        ]
        assert len(set(delta)) == 1 and delta[0] > 0, delta
        assert (
            check.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='qualification' "
                "AND state='idle in transaction'"
            )
            == "0"
        )
        result = {
            "seconds": round(time.monotonic() - started, 3),
            "workers": 8,
            "counts": dict(counters),
            "faults": faults,
            "before": before,
            "after": after,
            "delta": delta,
            "failures": failures,
        }
        STATE.joinpath("soak.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)
    finally:
        stop.set()
        compose(
            "up",
            "-d",
            "--no-deps",
            "db",
            "cache",
            "security",
            "app",
            capture_output=True,
        )
        compose(
            "exec", "-T", "security", "redis-cli", "DEL", block_key, capture_output=True
        )


if __name__ == "__main__":
    main()
