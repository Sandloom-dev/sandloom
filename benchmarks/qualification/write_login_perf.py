"""Write / login before-after on the isolated Oracle qualification deployment.

The Oracle-runnable half of docs/design/write-login-profile-2026-10-22.md.
It uses oracle_perf.py's Experiment (same fixture, same lock, the same
switch / snapshot / restore and the same refusal to touch anything outside
the qualification compose project) and oracle_workloads.py's load() /
request() helpers. Run it once per image (``--variant`` = the tag suffix of
``sandloom-perf:cb278f8-<variant>``: ``official`` for the current image, a
candidate built from this branch and tagged e.g. ``sandloom-perf:cb278f8-wlafter``),
each into its own new ``--out``, then ``compare`` the two directories.

Sections (``--sections``, default all but ``hash``):

* ``writes``  — closed-loop capacity of /dw/7 (class F) and /dw/3 (class W),
  64 connections; goodput, p99, app CPU per request (cgroup), outbox drain.
* ``tail``    — the app served through ``ladder/tail_probe.py`` (per-request
  waits: pool checkout, BEGIN, statements, COMMIT, each Redis instance,
  event-loop lag), /dw/7 OPEN loop at ``--tail-rates`` (default 200,290 =
  ~55 % / ~80 % of the 360/s write capacity); ``write_tail.py report`` of
  the records: which wait dominates p50 and p99. ``--split-idempotency``
  adds the same rates with the idempotency records moved (probe patch, an
  EXPERIMENT) to Redis DB ``--idem-db`` of the broker instance.
* ``role``    — the member role PATCH on ONE connection (alternating
  admin/member: every request changes state, audits, revokes).
* ``login``   — password logins, ``--login-conc`` connections, one synthetic
  client IP per request (the per-IP limit stays on; trusted local ingress
  like oracle_workloads.mixed()).
* ``refresh`` — refresh-token rotation chains (one chain per connection).
* ``hash``    — ``login`` at each ``--hash-concurrency`` value
  (PASSWORD_HASH_CONCURRENCY; ``default`` = unset).

    sudo python3 benchmarks/qualification/write_login_perf.py run \\
      --variant official --label before \\
      --fixture $FIXTURE/benchmarks/qualification \\
      --source $SOURCE \\
      --selected $FIXTURE/.state/selected-compose-files.txt \\
      --out $OUTPUT/wl-before
    # build + tag the candidate image from this branch, then:
    sudo python3 benchmarks/qualification/write_login_perf.py run --variant wlafter \\
      --label after ... --out $OUTPUT/wl-after
    python3 benchmarks/qualification/write_login_perf.py compare \\
      $OUTPUT/wl-before $OUTPUT/wl-after
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

from oracle_perf import APP, Experiment, command

HERE = Path(__file__).resolve().parent
SECTIONS = ("writes", "tail", "role", "login", "refresh", "hash")


class _OverrideFile(type(Path())):  # type: ignore[misc]
    """The compose override Experiment.switch writes, plus our additions
    (the probe's command / volume) merged into the app service."""

    extra: dict = {}

    def write_text(self, data, *args, **kwargs):  # noqa: ANN001, ANN201
        body = json.loads(data)
        app = body["services"]["app"]
        for key, value in self.extra.items():
            if key == "volumes":
                app.setdefault("volumes", []).extend(value)
            else:
                app[key] = value
        return super().write_text(json.dumps(body), *args, **kwargs)


class WriteLoginExperiment(Experiment):
    def __init__(self, args):
        super().__init__(args)
        self.override = _OverrideFile(self.override)
        self.label = args.label

    def switch_with(self, variant, extra_env=None, extra=None):
        self.override.extra = extra or {}
        try:
            self.switch(variant, extra_env=extra_env or {"SANDLOOM_WL_STAND": "1"})
        finally:
            self.override.extra = {}

    def save(self, record):
        super().save({"label": self.label, **record})


def _gateways():
    data = json.loads(command(["docker", "inspect", APP]))[0]
    return sorted(
        {
            v["Gateway"]
            for v in data["NetworkSettings"]["Networks"].values()
            if v["Gateway"]
        }
    )


def _client_ip_workload(entries, headers):
    from benchsuite import loadclient as lc

    class Clients(lc.Workload):
        def request(self, i):
            raw, expected = super().request(i)
            ip = f"198.18.{(i // 254) % 256}.{i % 254 + 1}"
            raw = raw.replace(
                b"\r\n\r\n", b"\r\nX-Forwarded-For: " + ip.encode() + b"\r\n\r\n", 1
            )
            return raw, expected

    return Clients(entries, headers)


def _trusted_env():
    return {
        "TRUSTED_PROXY_IPS": json.dumps([g + "/32" for g in _gateways()]),
        "TRUST_PROXY_HEADERS": "true",
    }


# --------------------------------------------------------------------------- #
def writes(exp, variant):
    from oracle_workloads import load

    from benchsuite import loadclient as lc

    exp.switch(variant)
    bearer = exp.run.token()
    for route, scope in (("/dw/7", "F"), ("/dw/3", "W")):
        prefix = f"wl-{scope}-" + uuid.uuid4().hex[:10]

        class Writes(lc.Workload):
            def request(self, i, route=route, prefix=prefix):
                return lc.Workload(
                    [
                        lc.Entry(
                            "POST",
                            route,
                            json.dumps({"name": f"{prefix}-{i}", "value": 7}),
                            frozenset([201]),
                        )
                    ],
                    [
                        "Authorization: Bearer " + bearer,
                        f"Idempotency-Key: {prefix}-{i}",
                    ],
                ).request(i)

        rec = load(
            exp,
            Writes([lc.Entry("POST", route, "", frozenset([201]))]),
            label=f"write-{scope}",
            route=route,
            n=exp.args.write_n,
            conc=64,
        )
        app = rec["resources"].get(APP, {})
        rec["cpu_us_per_request"] = app.get("cpu_stat_delta", {}).get(
            "usage_usec", 0
        ) / max(rec["ok"], 1)
        exp.save(rec)


def tail(exp, variant):
    from oracle_workloads import environment

    from benchsuite import loadclient as lc

    sys.path.insert(0, str(exp.source / "benchmarks/ladder"))
    import write_tail

    runs = [("probe", False)] + (
        [("split", True)] if exp.args.split_idempotency else []
    )
    for name, split in runs:
        out = exp.out / f"tail-{name}"
        out.mkdir(mode=0o777, exist_ok=True)
        out.chmod(0o777)
        env = environment()
        extra_env = {
            "TAIL_OUT": "/tail",
            "PYTHONPATH": env.get("PYTHONPATH", "/benchmarks") + ":/benchmarks/ladder",
        }
        if split:
            infra = json.loads(env["INFRA"])
            broker = (infra.get("eventbus", {}).get("default") or {}).get("url") or env[
                "BROKER_URL"
            ]
            base = broker.rsplit("/", 1)[0]
            infra.setdefault("cache", {})["idem"] = {
                "driver": "redis",
                "url": f"{base}/{exp.args.idem_db}",
            }
            extra_env.update(INFRA=json.dumps(infra), TAIL_IDEMPOTENCY_INSTANCE="idem")
        exp.switch_with(
            variant,
            extra_env,
            {
                "command": [
                    "gunicorn",
                    "-c",
                    "python:sandloom.core.cli.gunicorn_conf",
                    "tail_probe:app",
                ],
                "volumes": [f"{out}:/tail"],
            },
        )
        token = exp.run.token()
        phases = {}
        for rate in [float(r) for r in exp.args.tail_rates.split(",")]:
            label = f"r{int(rate)}"
            wl = write_tail._workload(label, token)
            asyncio.run(
                lc.open_loop(
                    [("127.0.0.1", 18222)], wl, rate, exp.args.warmup, pool=128
                )
            )
            before = exp.snapshot()
            stats = asyncio.run(
                lc.open_loop(
                    [("127.0.0.1", 18222)],
                    wl,
                    rate,
                    exp.args.seconds,
                    pool=256,
                    timeout=30,
                )
            )
            res = {
                **lc.open_result(stats),
                "ok": stats.ok,
                "errors": stats.errors,
                "rate": rate,
                "resources": exp.deltas(before, exp.snapshot(), exp.args.seconds),
                "client_latencies_ms": [
                    round(x * 1e3, 3) for x in sorted(stats.latencies)
                ],
            }
            phases[label] = res
            time.sleep(5)
        # Graceful stop flushes every worker's records (atexit).
        exp.run.compose("stop", "app", capture_output=True)
        (out / "client.json").write_text(json.dumps({"phases": phases}) + "\n")
        report = write_tail.report(argparse.Namespace(dir=str(out)))
        exp.save(
            {
                "section": "tail",
                "variant": exp.variant,
                "split_idempotency": split,
                "report": report,
                "passed": all(p["errors"] == 0 for p in phases.values()),
            }
        )


def role(exp, variant):
    from oracle_workloads import roles

    exp.switch(variant)
    roles(exp, variant)


def _subjects(exp):
    """The fixture's synthetic accounts, logged in ONCE per run (every
    prepare() signs each in, and the per-IP login limit is 5 a minute)."""
    if not hasattr(exp, "_wl_subjects"):
        from real_workload import prepare

        exp._wl_subjects = prepare(4)
    return exp._wl_subjects


def _logins(exp, label, **meta):
    from oracle_workloads import load

    from benchsuite import loadclient as lc

    subjects = _subjects(exp)
    entries = [
        lc.Entry(
            "POST",
            "/auth/login",
            json.dumps({"email": s.email, "password": s.password}),
            frozenset([200]),
        )
        for s in subjects
    ]
    rec = load(
        exp,
        _client_ip_workload(entries, []),
        label=label,
        route="/auth/login",
        n=exp.args.login_n,
        conc=exp.args.login_conc,
        **meta,
    )
    app = rec["resources"].get(APP, {})
    rec["cpu_ms_per_login"] = (
        app.get("cpu_stat_delta", {}).get("usage_usec", 0) / 1000 / max(rec["ok"], 1)
    )
    exp.save(rec)


def login(exp, variant):
    exp.switch_with(variant, _trusted_env())
    _logins(exp, "login")


def hash_limits(exp, variant):
    for limit in exp.args.hash_concurrency.split(","):
        env = _trusted_env()
        if limit != "default":
            env["PASSWORD_HASH_CONCURRENCY"] = limit
        exp.switch_with(variant, env)
        _logins(exp, "login-hash", hash_concurrency=limit)


def refresh(exp, variant):
    from oracle_workloads import request

    exp.switch_with(variant, _trusted_env())
    subjects = _subjects(exp)
    conc = exp.args.refresh_conc
    chains = []
    for i in range(conc):
        s = subjects[i % len(subjects)]
        status, body = request(
            exp,
            "POST",
            "/auth/login",
            body={"email": s.email, "password": s.password},
            headers={"X-Forwarded-For": f"198.19.1.{i + 1}"},
        )
        assert status == 200, ("login", status)
        chains.append(body["refresh_token"])
    before = exp.snapshot()
    lat, errors = [], 0
    started = time.monotonic()
    deadline = started + exp.args.seconds

    def chain(k):
        nonlocal errors
        token = chains[k]
        while time.monotonic() < deadline:
            t = time.perf_counter()
            status, body = request(
                exp,
                "POST",
                "/auth/refresh",
                body={"refresh_token": token},
                headers={"X-Forwarded-For": f"198.19.2.{k + 1}"},
            )
            if status != 200:
                errors += 1
                return
            lat.append(time.perf_counter() - t)
            token = body["refresh_token"]

    import threading

    threads = [threading.Thread(target=chain, args=(k,)) for k in range(conc)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.monotonic() - started
    after = exp.snapshot()
    lat.sort()
    exp.save(
        {
            "section": "refresh",
            "variant": exp.variant,
            "connections": conc,
            "goodput_rps": len(lat) / elapsed,
            "p50_ms": lat[len(lat) // 2] * 1e3 if lat else None,
            "p99_ms": lat[int(len(lat) * 0.99)] * 1e3 if lat else None,
            "errors": errors,
            "resources": exp.deltas(before, after, elapsed),
            "passed": errors == 0,
        }
    )


# --------------------------------------------------------------------------- #
def compare(a):
    def load_dir(d):
        rows = [
            json.loads(s) for s in (Path(d) / "results.jsonl").read_text().splitlines()
        ]
        out = {}
        for r in rows:
            key = r.get("section", "?")
            if r.get("hash_concurrency"):
                key += f"[{r['hash_concurrency']}]"
            if key == "tail":
                key += "[split]" if r.get("split_idempotency") else "[probe]"
            out.setdefault(key, []).append(r)
        return out

    before, after = load_dir(a.before), load_dir(a.after)
    lines = [
        "| section | metric | before | after | change |",
        "|---|---|---:|---:|---:|",
    ]
    for key in sorted(set(before) & set(after)):
        for metric in (
            "goodput_rps",
            "p99_ms",
            "cpu_us_per_request",
            "cpu_ms_per_login",
        ):
            b = [r[metric] for r in before[key] if r.get(metric) is not None]
            c = [r[metric] for r in after[key] if r.get(metric) is not None]
            if b and c:
                mb, mc = statistics.median(b), statistics.median(c)
                lines.append(
                    f"| {key} | {metric} | {mb:.1f} | {mc:.1f} | {(mc / mb - 1) * 100:+.1f}% |"
                )
        if key.startswith("tail"):
            for side, rows in (("before", before[key]), ("after", after[key])):
                rep = rows[-1]["report"]
                for label, r in rep.items():
                    if not label.startswith("r") or not isinstance(r, dict):
                        continue
                    lines.append(
                        f"| {key} {label} | {side} p50/p99 server ms | "
                        f"{r['server_ms']['p50']} | {r['server_ms']['p99']} | "
                        f"tail: {r['slowest_1pct']} |"
                    )
    text = "\n".join(lines) + "\n"
    print(text)
    if a.out:
        Path(a.out).write_text(text)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--variant", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--fixture", required=True, type=Path)
    r.add_argument("--source", required=True, type=Path)
    r.add_argument("--selected", required=True, type=Path)
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--sections", default="writes,tail,role,login,refresh")
    r.add_argument("--seconds", type=int, default=30)
    r.add_argument("--warmup", type=int, default=3)
    r.add_argument("--rounds", type=int, default=1)
    r.add_argument("--write-n", type=int, default=8192)
    r.add_argument("--tail-rates", default="200,290")
    r.add_argument("--split-idempotency", action="store_true")
    r.add_argument("--idem-db", type=int, default=6)
    r.add_argument("--login-n", type=int, default=256)
    r.add_argument("--login-conc", type=int, default=8)
    r.add_argument("--refresh-conc", type=int, default=8)
    r.add_argument("--hash-concurrency", default="default,1,2,4")
    # Experiment expects these attributes (unused by these sections).
    r.add_argument("--write-rate", type=int, default=0)
    r.add_argument("--mix-profiles", default="mixed")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--out")
    a = p.parse_args()
    if a.cmd == "compare":
        compare(a)
        return
    sections = a.sections.split(",")
    if not set(sections) <= set(SECTIONS):
        p.error(f"sections: {SECTIONS}")
    if (a.out / "results.jsonl").exists():
        p.error("use a new result directory; measured windows are never overwritten")
    with open("/tmp/sandloom-oracle-perf.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exp = WriteLoginExperiment(a)
        exp.variant = a.variant
        funcs = {
            "writes": writes,
            "tail": tail,
            "role": role,
            "login": login,
            "refresh": refresh,
            "hash": hash_limits,
        }
        try:
            for _round in range(a.rounds):
                for section in sections:
                    funcs[section](exp, a.variant)
        finally:
            exp.restore()
        if any(not rec.get("passed", True) for rec in exp.records):
            raise SystemExit("invalid measurement retained; see results.jsonl")
    os.chmod(a.out / "results.jsonl", 0o600)


if __name__ == "__main__":
    main()
