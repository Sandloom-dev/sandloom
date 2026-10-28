"""Real API + RLS/dedupe correctness under bounded infrastructure failures.

Use compose.production.yml. Run external_soak.py from the load-generator host
at the same time for sustained HTTPS load through Cloudflare and Coolify.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import check
import real_workload
from run import STATE, compose, token

SERVICES = ("cache", "broker", "security", "db", "app")


def counts():
    return check.sql(
        "SELECT (SELECT count(*) FROM bench_tenant_orders),"
        "(SELECT count(*) FROM bench_tenant_order_lines),"
        "(SELECT count(*) FROM outbox_messages WHERE channel='bench.item.created.v1'),"
        "(SELECT count(*) FROM audit_log WHERE action='bench.order.placed'),"
        "(SELECT count(*) FROM contract_command_log WHERE contract LIKE 'bench.writediag.v1.%')"
    )


def redis(service, *args):
    return compose(
        "exec",
        "-T",
        service,
        "redis-cli",
        "--raw",
        *args,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--fault-interval", type=int, default=600)
    args = parser.parse_args()
    if not 180 <= args.seconds <= 14400 or args.fault_interval < 15:
        parser.error("duration 180..14400 and fault interval >=15 required")
    if args.seconds <= 5 * args.fault_interval:
        parser.error("duration must cover every service fault")
    output = STATE / "production-soak.json"
    progress = STATE / "soak-progress.json"
    output.unlink(missing_ok=True)
    STATE.joinpath("soak.pid").write_text(str(os.getpid()))
    subjects = real_workload.prepare(4)
    jwts = [token(), token("5e000000-0000-4000-8000-00000000000b")]
    blocked = token()
    claims = json.loads(base64.urlsafe_b64decode(blocked.split(".")[1] + "=="))
    block_key = "sandloom:auth:blocked:" + claims["sub"]
    redis("security", "SET", block_key, "qualification-soak")
    marker = "qualification:persistence:" + uuid.uuid4().hex
    marker_id = redis("broker", "XADD", marker, "*", "fixture", "survives-sigkill")
    before = counts()
    counters = Counter()
    failures = []
    faults = []
    samples = []
    lock = threading.Lock()
    stop = threading.Event()
    cancelled = threading.Event()
    unstable_until = 0.0
    started = time.monotonic()
    wall_started = time.time()
    deadline = started + args.seconds
    report = {
        "passed": False,
        "requested_seconds": args.seconds,
        "started_epoch": wall_started,
        "faults": faults,
        "failures": failures,
        "counts": {},
        "resources": samples,
    }
    initial_container = compose(
        "ps", "-q", "app", capture_output=True, text=True
    ).stdout.strip()
    report["started_image"] = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Image}}", initial_container], text=True
    ).strip()
    planner = json.loads(
        compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            "import urllib.request,json,os; "
            "r=urllib.request.Request('http://127.0.0.1:2222/diagnostics/core',"
            "headers={'X-Internal-Token':os.environ['INTERNAL_API_TOKEN']}); "
            "print(json.dumps(json.load(urllib.request.urlopen(r))['planner']))",
            capture_output=True,
            text=True,
        ).stdout
    )
    assert planner["optimized_executor_enabled"]
    assert any(
        op["name"] == "bench.tenant_item.get" and op["executor"] == "optimized"
        for op in planner["operations"]
    )
    report["planner"] = planner

    def save_progress():
        data = {
            "started_epoch": wall_started,
            "updated_epoch": time.time(),
            "faults": faults,
            "active": not stop.is_set(),
            "elapsed_seconds": time.monotonic() - started,
        }
        temporary = progress.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.replace(progress)

    def cancel(_signal, _frame):
        cancelled.set()
        stop.set()

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)

    def observe(kind, code, expected, body, unstable):
        with lock:
            counters[f"{kind}:{code}"] += 1
            if code not in expected and not (unstable and (code == 0 or code >= 500)):
                # Do not persist API bodies: login responses can contain tokens.
                failures.append(
                    {
                        "kind": kind,
                        "status": code,
                        "expected": expected,
                        "at_epoch": time.time(),
                    }
                )
                stop.set()

    def worker(index):
        subject = subjects[index]
        jwt = jwts[index % 2]
        own, foreign = (1, 5001) if index % 2 == 0 else (5001, 1)
        sequence = 0
        pending = None
        previous_id = None
        next_auth_attempt = time.monotonic() + 300
        while not stop.is_set() and time.monotonic() < deadline:
            calls = [
                ("own", f"/s5/{own}", jwt, (200,)),
                ("foreign", f"/s5/{foreign}", jwt, (404,)),
                ("planned_own", f"/s5plan/{own}", jwt, (200,)),
                ("planned_foreign", f"/s5plan/{foreign}", jwt, (404,)),
                ("blocked", "/s5/1", blocked, (401,)),
                ("identity", f"/api/v1/users/{subject.user_id}", subject.jwt, (200,)),
                ("orgs", "/api/v1/orgs?limit=20", subject.jwt, (200,)),
                (
                    "credits",
                    f"/api/v1/billing/credits?org_id={subject.org_id}",
                    subject.jwt,
                    (200,),
                ),
                (
                    "foreign_credits",
                    f"/api/v1/billing/credits?org_id={subject.foreign_org}",
                    subject.jwt,
                    (403, 404),
                ),
            ]
            for kind, path, bearer, expected in calls:
                unstable = time.monotonic() < unstable_until
                code, body = check.request(path, bearer)
                unstable |= time.monotonic() < unstable_until
                observe(kind, code, expected, body, unstable)
                if (
                    kind in ("own", "planned_own")
                    and code == 200
                    and body.get("id") != own
                ):
                    observe("wrong_tenant_body", 200, (404,), None, False)
            sequence += 1
            if time.monotonic() >= next_auth_attempt:
                unstable = time.monotonic() < unstable_until
                kind = "refresh" if subject.refresh_token is not None else "login"
                code = subject.refresh()
                next_auth_attempt = time.monotonic() + (300 if code == 200 else 30)
                observe(
                    kind,
                    code,
                    (200,),
                    None,
                    unstable or time.monotonic() < unstable_until,
                )
            if pending is None and sequence % 5 == 0:
                pending = {
                    "name": f"soak-{index}-{uuid.uuid4().hex}",
                    "value": 7,
                    "key": uuid.uuid4().hex,
                }
            if pending is not None:
                unstable = time.monotonic() < unstable_until
                code, body = check.request("/dw/6", jwt, pending)
                observe(
                    "command",
                    code,
                    (201,),
                    None,
                    unstable or time.monotonic() < unstable_until,
                )
                if code == 201:
                    if previous_id is None:
                        previous_id = body["id"]
                    else:
                        if previous_id != body["id"]:
                            observe("duplicate_effect", 200, (409,), None, False)
                        pending = None
                        previous_id = None
            stop.wait(0.5)

    try:
        save_progress()
        print("SOAK_STARTED " + json.dumps({"started_epoch": wall_started}), flush=True)
        next_fault = started + args.fault_interval
        next_sample = started
        with ExitStack() as cleanup:
            pool = cleanup.enter_context(ThreadPoolExecutor(max_workers=4))
            cleanup.callback(stop.set)
            futures = [pool.submit(worker, i) for i in range(4)]
            while not stop.is_set() and time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_sample:
                    containers = compose(
                        "ps", "-q", *SERVICES, capture_output=True, text=True
                    ).stdout.split()
                    stats = subprocess.run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            *containers,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    samples.append(
                        {
                            "at_seconds": round(now - started, 3),
                            "containers": [json.loads(s) for s in stats.splitlines()],
                            "postgres": json.loads(
                                check.sql(
                                    "SELECT json_build_object('app_connections',count(*),"
                                    "'active',count(*) FILTER (WHERE state='active'),"
                                    "'idle_in_transaction',count(*) FILTER (WHERE state='idle in transaction'),"
                                    "'lock_waiters',count(*) FILTER (WHERE wait_event_type='Lock')) "
                                    "FROM pg_stat_activity WHERE usename='qualification_app'"
                                )
                            ),
                        }
                    )
                    next_sample = now + 60
                    save_progress()
                if now >= next_fault:
                    service = SERVICES[len(faults) % len(SERVICES)]
                    unstable_until = time.monotonic() + 120
                    fault = {"service": service, "started_epoch": time.time()}
                    faults.append(fault)
                    save_progress()
                    begin = time.monotonic()
                    try:
                        compose("kill", "-s", "SIGKILL", service, capture_output=True)
                        stop.wait(4)
                    finally:
                        compose("up", "-d", "--no-deps", service, capture_output=True)
                    check.wait_ready(jwts[0])
                    assert redis("security", "GET", block_key) == "qualification-soak"
                    assert marker_id in redis("broker", "XRANGE", marker, "-", "+")
                    unstable_until = time.monotonic() + 10
                    fault.update(
                        recovered_epoch=time.time(),
                        tolerated_until_epoch=time.time() + 10,
                        recovery_seconds=round(time.monotonic() - begin, 3),
                    )
                    save_progress()
                    print("SOAK_FAULT " + json.dumps(fault), flush=True)
                    next_fault = time.monotonic() + args.fault_interval
                for future in futures:
                    if future.done():
                        future.result()
                stop.wait(1)
            stop.set()
            for future in futures:
                future.result()
        if cancelled.is_set():
            raise InterruptedError("soak cancelled")
        assert not failures, failures[:5]
        assert time.monotonic() - started >= args.seconds, "soak ended early"
        assert {f["service"] for f in faults} == set(SERVICES), faults
        check.wait_ready(jwts[0])
        for _ in range(90):
            if (
                check.sql(
                    "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL"
                )
                == "0"
            ):
                break
            time.sleep(1)
        else:
            raise AssertionError("outbox did not drain")
        after = counts()
        delta = [
            int(b) - int(a)
            for a, b in zip(before.split("|"), after.split("|"), strict=True)
        ]
        assert len(set(delta)) == 1 and delta[0] > 0, delta
        assert (
            check.sql("SELECT count(*) FROM outbox_messages WHERE dead_at IS NOT NULL")
            == "0"
        )
        assert (
            check.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname='qualification' AND state='idle in transaction'"
            )
            == "0"
        )
        state = compose(
            "ps", "-q", "app", capture_output=True, text=True
        ).stdout.strip()
        info = json.loads(
            subprocess.run(
                ["docker", "inspect", state], check=True, capture_output=True, text=True
            ).stdout
        )[0]
        assert not info["State"]["OOMKilled"]
        assert info["Image"] == report["started_image"], (
            "application image changed during the soak"
        )
        rollout = dict(item.split("=", 1) for item in info["Config"]["Env"])
        assert rollout.get("PLANNER_OPTIMIZED_EXECUTOR") == "true"
        report.update(
            passed=True,
            before=before,
            after=after,
            delta=delta,
            image=info["Image"],
            app_restarts=info["RestartCount"],
            broker_marker_survived=True,
            security_block_survived=True,
            planner_optimized_executor=True,
            application_cpu_limit=info["HostConfig"]["NanoCpus"] / 1_000_000_000,
            workers=int(rollout["WEB_CONCURRENCY"]),
        )
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        stop.set()
        compose("up", "-d", "--no-deps", *SERVICES, capture_output=True)
        redis("security", "DEL", block_key)
        redis("broker", "DEL", marker)
        report.update(
            seconds=round(time.monotonic() - started, 3),
            counts=dict(counters),
            finished_epoch=time.time(),
        )
        output.write_text(json.dumps(report, indent=2) + "\n")
        save_progress()
        print(
            "SOAK_FINISHED "
            + json.dumps({"passed": report["passed"], "seconds": report["seconds"]}),
            flush=True,
        )


if __name__ == "__main__":
    main()
