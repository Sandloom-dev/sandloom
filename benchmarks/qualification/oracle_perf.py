"""Controlled Oracle qualification experiments; run on the isolated fixture host.

Uses the existing qualification compose project, never a shared Coolify service.
Every configuration change is restored on exit. Credentials stay in memory or
the fixture's existing private files. See oracle-perf.md for invocation/scope.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT = "sandloom-release-qualification"
APP = PROJECT + "-app-1"
PIN = "cb278f8da77bfa9f235b16c8df92236bb6b1b2c5"


def command(args, **kwargs):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, **kwargs
    ).stdout.strip()


def python_choice(records, threshold=0.03):
    """Equal route weights; compare medians, never select a best window."""
    ratios = {}
    for route in ("/s5/", "/s5plan/", "/auth/me"):
        sides = {}
        for variant in ("official", "pbs"):
            rows = [
                r
                for r in records
                if r.get("section") == "ab"
                and r["variant"] == variant
                and r["route"] == route
            ]
            if len(rows) < 3 or not all(r["passed"] for r in rows):
                return {
                    "decision": "inconclusive",
                    "reason": "three clean windows per route/variant required",
                }
            sides[variant] = statistics.median(r["goodput_rps"] for r in rows)
        ratios[route] = sides["pbs"] / sides["official"]
    gain = math.prod(ratios.values()) ** (1 / len(ratios)) - 1
    return {
        "decision": "pbs" if gain >= threshold else "official",
        "threshold": threshold,
        "geometric_mean_gain": gain,
        "route_ratios": ratios,
        "criterion": "geometric mean of per-route median RPS ratios; three alternating rounds",
    }


class Experiment:
    def __init__(self, args):
        self.args = args
        self.source = args.source.resolve()
        self.fixture = args.fixture.resolve()
        self.out = args.out.resolve()
        self.out.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.selected = args.selected.resolve().read_text().strip()
        if not self.selected:
            raise RuntimeError("explicit original compose selection required")
        self.override = self.out / "variant.yml"
        os.environ["SANDLOOM_QUALIFICATION_ENV_FILES"] = str(
            self.fixture / ".state/https.env"
        )
        os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = self.selected
        sys.path.insert(0, str(self.fixture))
        import check
        import run

        self.run, self.check = run, check
        sys.path.insert(0, str(self.source / "benchmarks"))
        self.records = []
        self.startups = []
        self.variant = None
        self.extra_env = {}
        data = json.loads(command(["docker", "inspect", APP]))[0]
        if data["Config"]["Labels"].get("com.docker.compose.project") != PROJECT:
            raise RuntimeError(
                "refusing a container outside the isolated qualification project"
            )
        self.original_runtime = {
            "image": data["Image"],
            "cpus": data["HostConfig"]["NanoCpus"],
            "memory": data["HostConfig"]["Memory"],
        }
        version = subprocess.run(["wrk", "--version"], capture_output=True, text=True)
        self.generator = {
            "python": sys.version,
            "wrk": (version.stdout + version.stderr).splitlines()[0],
        }
        self.verify_images()

    def verify_images(self):
        code = """import hashlib,importlib.metadata as m,json,pathlib,sys,sysconfig
import sandloom
root=pathlib.Path(sandloom.__file__).parent
h=hashlib.sha256()
for p in sorted(root.rglob('*.py')):
    h.update(str(p.relative_to(root)).encode());h.update(p.read_bytes())
print(json.dumps(dict(python=list(sys.version_info[:3]),build=sys.version,source_sha256=h.hexdigest(),
packages={d.metadata['Name'].lower():d.version for d in m.distributions() if d.metadata['Name'].lower() not in {'pip','setuptools','wheel'}},
config={k:sysconfig.get_config_var(k) for k in ['CONFIG_ARGS','PY_CFLAGS','Py_ENABLE_SHARED']})))
"""
        images = {}
        for variant in ("official", "pbs"):
            image = f"sandloom-perf:cb278f8-{variant}"
            data = json.loads(command(["docker", "image", "inspect", image]))[0]
            if data["Config"]["Labels"].get("org.opencontainers.image.revision") != PIN:
                raise RuntimeError(
                    "image revision does not match the fixed experiment commit"
                )
            metadata = json.loads(
                command(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--entrypoint",
                        "python",
                        image,
                        "-c",
                        code,
                    ]
                )
            )
            images[variant] = {
                "image": data["Id"],
                "size_bytes": data["Size"],
                **metadata,
            }
        for key in ("python", "source_sha256", "packages"):
            if images["official"][key] != images["pbs"][key]:
                raise RuntimeError("Python A/B is confounded: different " + key)
        (self.out / "images.json").write_text(json.dumps(images, indent=2) + "\n")

    def save(self, record):
        record = {"at": datetime.now(UTC).isoformat(), **record}
        self.records.append(record)
        with (self.out / "results.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        (self.out / "results.jsonl").chmod(0o600)
        summary = {
            k: record[k]
            for k in (
                "section",
                "variant",
                "round",
                "route",
                "connections",
                "goodput_rps",
                "p99_ms",
                "passed",
                "rate",
            )
            if k in record
        }
        print(json.dumps(summary), flush=True)

    def snapshot(self):
        names = [
            PROJECT + "-" + n + "-1"
            for n in ("app", "db", "cache", "security", "broker")
        ]
        # Discover only the shared proxy's name; it is observed, never mutated.
        for line in command(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}}"]
        ).splitlines():
            name, image = line.split(" ", 1)
            if image.startswith("traefik:"):
                names.append(name)
        result = {}
        for data in json.loads(command(["docker", "inspect", *names])):
            pid = data["State"]["Pid"]
            if not pid:
                continue
            relative = (
                Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].strip()
            )
            group = Path("/sys/fs/cgroup") / relative.lstrip("/")
            cpu = {
                k: int(v)
                for k, v in (
                    line.split()
                    for line in (group / "cpu.stat").read_text().splitlines()
                )
            }
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            rss = []
            for child in children:
                try:
                    rss.append(
                        int(Path(f"/proc/{child}/statm").read_text().split()[1])
                        * os.sysconf("SC_PAGE_SIZE")
                    )
                except FileNotFoundError:
                    pass
            result[data["Name"].lstrip("/")] = {
                "cpu": cpu,
                "memory_current": int((group / "memory.current").read_text()),
                "worker_rss_bytes": rss,
                "image": data["Image"],
                "cpu_quota": data["HostConfig"]["NanoCpus"] / 1e9,
            }
        return result

    def deltas(self, before, after, seconds):
        return {
            name: {
                "cpu_cores": (
                    a["cpu"]["usage_usec"] - before[name]["cpu"]["usage_usec"]
                )
                / seconds
                / 1e6,
                "cpu_stat_delta": {
                    k: a["cpu"][k] - before[name]["cpu"][k] for k in a["cpu"]
                },
                "memory_current": a["memory_current"],
                "worker_rss_bytes": a["worker_rss_bytes"],
            }
            for name, a in after.items()
            if name in before
        }

    def switch(self, variant, workers=2, cpus=2, extra_env=None):
        image = f"sandloom-perf:cb278f8-{variant}"
        env = {"WEB_CONCURRENCY": str(workers), **(extra_env or {})}
        body = {
            "services": {
                "app": {
                    "image": image,
                    "cpus": cpus,
                    "environment": env,
                    "volumes": [
                        str(self.source / "benchmarks") + ":/benchmarks:ro",
                        str(self.source / "examples") + ":/examples:ro",
                    ],
                }
            }
        }
        # JSON is a YAML subset; no credential-bearing config is printed.
        self.override.write_text(json.dumps(body))
        self.override.chmod(0o600)
        os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = (
            self.selected + os.pathsep + str(self.override)
        )
        started = time.monotonic()
        self.run.compose(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            "app",
            capture_output=True,
        )
        deadline = time.monotonic() + 120
        while self.check.request("/ready", None)[0] != 200:
            state = json.loads(command(["docker", "inspect", APP]))[0]["State"]
            if time.monotonic() > deadline or state["Status"] in ("exited", "dead"):
                logs = subprocess.run(
                    ["docker", "logs", APP], capture_output=True, text=True
                )
                log = self.out / "startup-failed.private.log"
                log.write_text(logs.stdout + logs.stderr)
                log.chmod(0o600)
                raise RuntimeError(
                    "variant failed readiness; original configuration will be restored"
                )
            time.sleep(0.25)
        elapsed = time.monotonic() - started
        self.variant, self.extra_env = variant, extra_env or {}
        snap = self.snapshot()
        if snap[APP]["cpu_quota"] != cpus:
            raise RuntimeError("unexpected CPU quota")
        self.startups.append(
            {
                "variant": variant,
                "workers": workers,
                "cpu_quota": cpus,
                "compose_to_ready_s": elapsed,
                "snapshot": snap,
                "worker_boot": self.worker_boot(),
            }
        )
        (self.out / "startups.json").write_text(
            json.dumps(self.startups, indent=2) + "\n"
        )
        if not extra_env:
            jwt = self.run.token()
            for path, bearer, code in [
                ("/s5plan/1", jwt, 200),
                ("/s5plan/5001", jwt, 404),
                ("/s5plan/1", None, 401),
            ]:
                if self.check.request(path, bearer)[0] != code:
                    raise RuntimeError("tenant/auth preflight failed")

    def worker_boot(self):
        result = subprocess.run(
            ["docker", "logs", "--timestamps", APP], capture_output=True, text=True
        )
        workers = {}
        for line in (result.stdout + result.stderr).splitlines():
            if not any(
                marker in line
                for marker in (
                    "Booting worker with pid:",
                    "sandloom worker booted:",
                    "Application startup complete.",
                )
            ):
                continue
            pid = re.search(r"\]\s+\[(\d+)\]\s+\[", line)
            if not pid:
                continue
            try:
                at = datetime.fromisoformat(
                    line.split(" ", 1)[0].replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                continue
            marker = (
                "fork_epoch"
                if "Booting worker" in line
                else "imports_epoch"
                if "sandloom worker booted:" in line
                else "ready_epoch"
            )
            workers.setdefault(pid[1], {})[marker] = at
        for phases in workers.values():
            if "fork_epoch" in phases:
                for phase in ("imports", "ready"):
                    if phase + "_epoch" in phases:
                        phases[phase + "_after_fork_s"] = (
                            phases[phase + "_epoch"] - phases["fork_epoch"]
                        )
        return workers

    def wrk(
        self,
        route,
        seconds,
        *,
        connections=64,
        jwt=None,
        subject=None,
        label="window",
        **meta,
    ):
        jwt = jwt or self.run.token()
        env = dict(
            os.environ,
            SANDLOOM_QUALIFICATION_TOKEN=jwt,
            SANDLOOM_QUALIFICATION_READ_ROUTE=route,
            SANDLOOM_QUALIFICATION_PROFILE="me" if subject else "read",
            SANDLOOM_QUALIFICATION_SUBJECT=subject or "",
        )
        before = self.snapshot()
        start = time.monotonic()
        lua = self.source / "benchmarks/qualification/oracle_perf.lua"
        out = command(
            [
                "wrk",
                "-t",
                "1",
                "-c",
                str(connections),
                "-d",
                f"{seconds}s",
                "--timeout",
                "10s",
                "--latency",
                "-s",
                str(lua),
                self.check.BASE,
            ],
            env=env,
        )
        elapsed = time.monotonic() - start
        after = self.snapshot()
        path = self.out / f"{label}.txt"
        path.write_text(out + "\n")
        path.chmod(0o600)
        sample = json.loads(
            next(
                line.split(" ", 1)[1]
                for line in out.splitlines()
                if line.startswith("QUALIFICATION ")
            )
        )
        return {
            **meta,
            **sample,
            "route": route,
            "connections": connections,
            "seconds": seconds,
            "variant": self.variant,
            "raw": path.name,
            "resources": self.deltas(before, after, elapsed),
            "passed": not (
                sample["http_errors"]
                or sample["socket_errors"]
                or sample["invalid_bodies"]
            ),
        }

    def identity(self):
        from real_workload import prepare

        person = prepare(1)[0]
        return person.jwt, person.user_id

    def ab(self):
        for rnd in range(self.args.rounds):
            for variant in ("official", "pbs") if rnd % 2 == 0 else ("pbs", "official"):
                self.switch(variant)
                jwt, subject = self.identity()
                routes = ["/s5/", "/s5plan/", "/auth/me"]
                routes = routes[rnd % 3 :] + routes[: rnd % 3]
                for route in routes:
                    name = f"ab-r{rnd}-{variant}-{route.strip('/').replace('/', '-')}"
                    kw = {"jwt": jwt, "subject": subject} if route == "/auth/me" else {}
                    warm = self.wrk(
                        route, self.args.warmup, label=name + "-warmup", **kw
                    )
                    sample = self.wrk(
                        route,
                        self.args.seconds,
                        label=name,
                        section="ab",
                        round=rnd,
                        **kw,
                    )
                    sample["warmup"] = warm
                    sample["passed"] &= warm["passed"]
                    self.save(sample)
        decision = python_choice(self.records)
        (self.out / "python-decision.json").write_text(
            json.dumps(decision, indent=2) + "\n"
        )
        print(json.dumps(decision), flush=True)
        return decision

    def scaling(self, variant):
        for workers, cpus in [(2, 2), (3, 2), (4, 2), (4, 4)]:
            self.switch(variant, workers, cpus)
            label = f"scaling-w{workers}-cpu{cpus}"
            warm = self.wrk("/s5plan/", self.args.warmup, label=label + "-warmup")
            sample = self.wrk(
                "/s5plan/",
                self.args.seconds,
                label=label,
                section="scaling",
                workers=workers,
                cpus=cpus,
                warmup=warm,
            )
            sample["passed"] &= warm["passed"]
            self.save(sample)

    def open_loop(self, variant):
        from benchsuite import loadclient as lc

        self.switch(variant)
        clean = [
            r["goodput_rps"]
            for r in self.records
            if r.get("section") == "ab"
            and r["variant"] == variant
            and r["route"] == "/s5plan/"
            and r["passed"]
        ]
        if len(clean) < 3:
            raise RuntimeError(
                "open-loop requires three clean capacity windows in this run"
            )
        capacity = statistics.median(clean)
        wl = lc.Workload(
            [lc.Entry("GET", "/s5plan/1", "", frozenset([200]))],
            ["Authorization: Bearer " + self.run.token()],
            target=lambda e, i: "/s5plan/" + str(i % 5000 + 1),
        )
        for fraction in (0.5, 0.7, 0.9):
            rate = round(capacity * fraction)
            warm = asyncio.run(
                lc.open_loop(
                    [("127.0.0.1", 18222)],
                    wl,
                    rate,
                    self.args.warmup,
                    pool=128,
                    timeout=10,
                )
            )
            before = self.snapshot()
            start = time.monotonic()
            stats = asyncio.run(
                lc.open_loop(
                    [("127.0.0.1", 18222)],
                    wl,
                    rate,
                    self.args.seconds,
                    pool=128,
                    timeout=10,
                )
            )
            elapsed = time.monotonic() - start
            after = self.snapshot()
            latency = sorted(stats.latencies)
            self.save(
                {
                    "section": "open-loop",
                    "variant": variant,
                    "route": "/s5plan/",
                    "rate": rate,
                    "capacity_rps": capacity,
                    "fraction": fraction,
                    **lc.open_result(stats),
                    "p95_ms": latency[min(len(latency) - 1, int(len(latency) * 0.95))]
                    * 1000
                    if latency
                    else None,
                    "goodput_rps": stats.ok / stats.elapsed,
                    "warmup": warm.as_dict(),
                    "resources": self.deltas(before, after, elapsed),
                    "passed": not (warm.errors or stats.errors),
                }
            )

    def restore(self):
        os.environ["SANDLOOM_QUALIFICATION_COMPOSE_FILES"] = self.selected
        self.run.compose(
            "up",
            "-d",
            "--no-build",
            "--no-deps",
            "--force-recreate",
            "app",
            capture_output=True,
        )
        self.check.wait_ready(self.run.token(), timeout=120)
        data = json.loads(command(["docker", "inspect", APP]))[0]
        current = {
            "image": data["Image"],
            "cpus": data["HostConfig"]["NanoCpus"],
            "memory": data["HostConfig"]["Memory"],
        }
        restored = current == self.original_runtime
        (self.out / "restoration.json").write_text(
            json.dumps({"passed": restored, "runtime": current}, indent=2) + "\n"
        )
        if not restored:
            raise RuntimeError(
                "restored runtime differs from original; see restoration.json"
            )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("section", choices=["ab", "core", "all", "writes", "mix", "memory"])
    p.add_argument(
        "--variant",
        choices=["official", "pbs"],
        help="required for standalone writes/mix",
    )
    p.add_argument("--fixture", required=True, type=Path)
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--selected", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument(
        "--write-rate",
        type=int,
        default=0,
        help="writes: fixed arrival rate; 0 = closed-loop",
    )
    p.add_argument("--mix-profiles", default="read-heavy,mixed")
    a = p.parse_args()
    if min(a.seconds, a.warmup, a.rounds) < 1:
        p.error("durations and rounds must be positive")
    if a.write_rate < 0 or not set(a.mix_profiles.split(",")) <= {
        "read-heavy",
        "mixed",
    }:
        p.error("invalid write rate or mix profile")
    if a.section in ("writes", "mix") and not a.variant:
        p.error("standalone workload requires an explicit --variant")
    if (a.out / "results.jsonl").exists():
        p.error("use a new result directory; measured windows are never overwritten")
    with open("/tmp/sandloom-oracle-perf.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(
            signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        experiment = Experiment(a)
        try:
            if a.section == "memory":
                for variant in ("official", "pbs"):
                    experiment.switch(variant)
                    sample = experiment.wrk(
                        "/s5plan/", 5, label="memory-" + variant, section="memory"
                    )
                    experiment.save(sample)
            elif a.section in ("writes", "mix"):
                from oracle_workloads import mixed, writes

                experiment.variant = a.variant
                (writes if a.section == "writes" else mixed)(experiment, a.variant)
            else:
                decision = experiment.ab()
                if a.section in ("core", "all"):
                    variant = decision["decision"]
                    if variant == "inconclusive":
                        raise RuntimeError("Python A/B failed validation")
                    experiment.open_loop(variant)
                    experiment.scaling(variant)
                    if a.section == "all":
                        from oracle_workloads import mixed, writes

                        writes(experiment, variant)
                        mixed(experiment, variant)
        finally:
            experiment.restore()
        if any(not record["passed"] for record in experiment.records):
            raise SystemExit("invalid measurement retained; see results.jsonl")


if __name__ == "__main__":
    main()
