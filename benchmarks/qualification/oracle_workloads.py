"""Write and full SaaS workload extensions for oracle_perf (host stdlib only)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

from oracle_perf import APP, PROJECT, command


def request(exp, method, path, jwt=None, body=None, headers=None):
    hdr = {"Content-Type": "application/json", **(headers or {})}
    if jwt:
        hdr["Authorization"] = "Bearer " + jwt
    req = urllib.request.Request(
        exp.check.BASE + path,
        method=method,
        headers=hdr,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with exp.check.HTTP.open(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def sql(exp, query, database="qualification"):
    return command(
        [
            "docker",
            "exec",
            PROJECT + "-db-1",
            "psql",
            "-U",
            "qualification_owner",
            "-d",
            database,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        ]
    )


def environment():
    data = json.loads(command(["docker", "inspect", APP]))[0]
    return dict(s.split("=", 1) for s in data["Config"]["Env"])


def outbox(exp, database="qualification"):
    result = json.loads(
        sql(
            exp,
            """SELECT json_build_object(
        'total',count(*),'pending',count(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),
        'dead',count(*) FILTER (WHERE dead_at IS NOT NULL),
        'oldest_pending_s',COALESCE(EXTRACT(epoch FROM now()-(min(emitted_at) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL))),0),
        'delivered_lag_max_s',COALESCE(max(EXTRACT(epoch FROM delivered_at-emitted_at)) FILTER (WHERE delivered_at > now()-interval '5 seconds'),0),
        'bytes',pg_total_relation_size('outbox_messages')) FROM outbox_messages""",
            database,
        )
    )
    result["at_epoch"] = time.time()
    return result


class Monitor:
    def __init__(self, exp, database="qualification"):
        self.exp, self.database = exp, database
        self.samples = []
        self.stop = threading.Event()
        self.error = None

    def __enter__(self):
        self.samples.append(outbox(self.exp, self.database))
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
        return self

    def run(self):
        while not self.stop.wait(2):
            try:
                self.samples.append(outbox(self.exp, self.database))
            except Exception as e:
                self.error = type(e).__name__
                break

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.samples.append(outbox(self.exp, self.database))


def load(
    exp,
    workload,
    *,
    label,
    n=4096,
    conc=32,
    rate=None,
    database="qualification",
    **meta,
):
    from benchsuite import loadclient as lc

    before = exp.snapshot()
    started = time.monotonic()
    with Monitor(exp, database) as monitor:
        if rate:
            st = asyncio.run(
                lc.open_loop(
                    [("127.0.0.1", 18222)],
                    workload,
                    rate,
                    exp.args.seconds,
                    pool=128,
                    timeout=10,
                )
            )
        else:
            st = asyncio.run(
                lc.closed_loop([("127.0.0.1", 18222)], workload, 0, n, conc, timeout=10)
            )
    after = exp.snapshot()
    elapsed = time.monotonic() - started
    record = {
        "section": label,
        "variant": exp.variant,
        "connections": conc,
        **lc.open_result(st),
        "goodput_rps": st.ok / st.elapsed,
        "outbox": monitor.samples,
        "monitor_error": monitor.error,
        "resources": exp.deltas(before, after, elapsed),
        "passed": not (st.errors or monitor.error),
        **meta,
    }
    return record


def metrics(exp):
    env = environment()
    req = urllib.request.Request(
        exp.check.BASE + "/metrics",
        headers={"X-Internal-Token": env["INTERNAL_API_TOKEN"]},
    )
    # Only the elected relay worker imports/registers these counters. A scrape
    # hitting another Gunicorn worker is absent data, never a zero counter.
    for _ in range(16):
        with exp.check.HTTP.open(req, timeout=10) as r:
            lines = r.read().decode().splitlines()
        selected = [
            s
            for s in lines
            if s.startswith(("outbox_dead_total", "outbox_relay_", "sandloom_outbox_"))
        ]
        if any(s.startswith("outbox_dead_total ") for s in selected):
            return selected
    raise RuntimeError("could not scrape the relay worker's counters")


def writes(exp, variant):
    from benchsuite import loadclient as lc

    exp.switch(variant)
    bearer = exp.run.token()
    prefix = "perf-f-" + uuid.uuid4().hex[:12]

    class Financial(lc.Workload):
        def request(self, i):
            return lc.Workload(
                [
                    lc.Entry(
                        "POST",
                        "/dw/7",
                        json.dumps({"name": prefix + "-{i}", "value": 7}),
                        frozenset([201]),
                    )
                ],
                ["Authorization: Bearer " + bearer, f"Idempotency-Key: {prefix}-{i}"],
            ).request(i)

    wl = Financial([lc.Entry("POST", "/dw/7", "", frozenset([201]))])
    # Negative controls: replay must not insert, conflicting body must be refused,
    # and an explicitly failed transaction must leave no effect/audit/outbox.
    control = "perf-control-" + uuid.uuid4().hex[:12]
    hdr = {"Idempotency-Key": control}
    payload = {"name": control, "value": 7}
    first, body = request(exp, "POST", "/dw/7", bearer, payload, hdr)
    replay, repeated = request(exp, "POST", "/dw/7", bearer, payload, hdr)
    conflict, _ = request(exp, "POST", "/dw/7", bearer, {**payload, "value": 8}, hdr)
    failname = "perf-fail-" + uuid.uuid4().hex[:12]
    rollback_before = sql(
        exp,
        "SELECT (SELECT count(*) FROM audit_log),(SELECT count(*) FROM outbox_messages)",
    )
    failure, _ = request(
        exp,
        "POST",
        "/dw/7?fail=true",
        bearer,
        {"name": failname, "value": 7},
        {"Idempotency-Key": failname},
    )
    assert (
        first == replay == 201
        and body == repeated
        and conflict == 422
        and failure == 409
    )
    assert (
        sql(exp, f"SELECT count(*) FROM bench_tenant_writes WHERE name='{failname}'")
        == "0"
    )
    assert (
        sql(
            exp,
            "SELECT (SELECT count(*) FROM audit_log),(SELECT count(*) FROM outbox_messages)",
        )
        == rollback_before
    )
    before_metrics = metrics(exp)
    record = load(
        exp,
        wl,
        label="financial-write",
        route="/dw/7",
        rate=exp.args.write_rate or None,
    )
    effects = json.loads(
        sql(
            exp,
            f"""SELECT json_build_object(
        'rows',(SELECT count(*) FROM bench_tenant_writes WHERE name LIKE '{prefix}-%'),
        'audit',(SELECT count(*) FROM audit_log a JOIN bench_tenant_writes w ON a.resource_id=w.id::text WHERE w.name LIKE '{prefix}-%' AND a.action='bench.write.created'),
        'outbox',(SELECT count(*) FROM outbox_messages WHERE payload->>'name' LIKE '{prefix}-%'))""",
        )
    )
    record.update(
        effects=effects,
        metrics_before=before_metrics,
        metrics_after=metrics(exp),
        negative_controls={
            "replay_status": replay,
            "conflict_status": conflict,
            "rollback_status": failure,
        },
    )
    record["passed"] &= all(v == record["ok"] for v in effects.values())
    deadline = time.monotonic() + 30
    while outbox(exp)["pending"] and time.monotonic() < deadline:
        time.sleep(1)
    record["outbox_drained"] = outbox(exp)
    record["observed_backlog_growth"] = (
        record["outbox"][-1]["pending"] > record["outbox"][0]["pending"]
    )
    record["rate"] = exp.args.write_rate or None
    record["passed"] &= (
        record["outbox_drained"]["pending"] == 0
        and record["outbox_drained"]["dead"] == record["outbox"][0]["dead"]
    )
    exp.save(record)
    roles(exp, variant)


def roles(exp, variant):
    from real_workload import prepare

    from benchsuite import loadclient as lc

    owner, member = prepare(2)
    org, user = str(uuid.UUID(owner.org_id)), str(uuid.UUID(member.user_id))
    # Fixture-only SQL establishes a real membership before the API load.
    sql(
        exp,
        f"INSERT INTO tenancy_members (id,organization_id,user_id,role,created_at) VALUES ('{uuid.uuid4()}','{org}','{user}','member',now()) ON CONFLICT (organization_id,user_id) DO UPDATE SET role='member'",
    )
    route = f"/api/v1/orgs/{org}/members/{user}"
    wl = lc.Workload(
        [
            lc.Entry("PATCH", route, json.dumps({"role": role}), frozenset([200]))
            for role in ("admin", "member")
        ],
        ["Authorization: Bearer " + owner.jwt],
    )
    audit_before = int(
        sql(
            exp,
            f"SELECT count(*) FROM audit_log WHERE action='tenancy.member.role_changed' AND data->>'user_id'='{user}'",
        )
    )
    record = load(exp, wl, label="role-change", route=route, n=512, conc=1)
    # One connection is deliberate: every operation alternates state, avoiding
    # no-op changes being reported as security-effect throughput.
    audit_after = int(
        sql(
            exp,
            f"SELECT count(*) FROM audit_log WHERE action='tenancy.member.role_changed' AND data->>'user_id'='{user}'",
        )
    )
    status, _ = request(exp, "GET", "/auth/me", member.jwt)
    record.update(
        audit_delta=audit_after - audit_before,
        old_member_token_status=status,
        serialized_single_membership=True,
    )
    record["passed"] &= audit_after - audit_before == record["ok"] and status == 401
    exp.save(record)


SEED = """import asyncio,json,os
from urllib.parse import urlsplit,urlunsplit
from benchsuite import mix,dataset
async def main():
    await mix.seed(full=True)
    import asyncpg
    conn=await asyncpg.connect(mix._asyncpg_dsn(full=True))
    try:
        import uuid
        for column,subject in [('user_id',mix.USER),('organization_id',mix.ORG)]:
            await conn.execute('INSERT INTO billing_customers (id,'+column+') VALUES ($1,$2) ON CONFLICT DO NOTHING',uuid.uuid4(),subject)
        await conn.execute('INSERT INTO billing_credit_balances (customer_id,amount) SELECT id,0 FROM billing_customers ON CONFLICT (customer_id) DO NOTHING')
        await conn.execute("GRANT CONNECT ON DATABASE sandloom_perf_cb_mixfull TO qualification_app")
        await conn.execute("GRANT USAGE ON SCHEMA public TO qualification_app")
        await conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO qualification_app")
        await conn.execute("GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO qualification_app")
        await dataset.reset_one(conn)
        counts=await dataset.counts_one(conn)
    finally: await conn.close()
    print('DATASET '+json.dumps(dict(rows=counts,catalog=mix.CATALOG,profiles=mix.PROFILES,modules=mix.FULL_MODULES)))
asyncio.run(main())
"""


def mix_prepare(exp, variant):
    env = environment()
    # This DB number is reserved for this benchmark in the isolated Redis
    # containers. Refuse to flush an existing namespace we do not own.
    for service in ("cache", "security", "broker"):
        cli = ["docker", "exec", PROJECT + "-" + service + "-1", "redis-cli", "-n", "5"]
        owner = command([*cli, "GET", "sandloom:qualification:owner"])
        size = int(command([*cli, "DBSIZE"]))
        if size and owner != "sandloom_perf_cb_mixfull":
            raise RuntimeError("Redis DB 5 is not owned by this full-mix fixture")
        command([*cli, "FLUSHDB"])
        command([*cli, "SET", "sandloom:qualification:owner", "sandloom_perf_cb_mixfull"])
    infra = json.loads(env["INFRA"])
    infra["cache"]["security"]["url"] = "redis://security:6379/5"
    infra["eventbus"]["default"]["url"] = "redis://broker:6379/5"
    secrets = dict(
        line.split("=", 1)
        for line in (exp.fixture / ".state/.env").read_text().splitlines()
        if "=" in line
    )
    env.update(
        ENVIRONMENT="dev",
        PRODUCTION_PROFILE="standard",
        DATABASE_URL="postgresql+asyncpg://qualification_owner:"
        + quote(secrets["DB_PASSWORD"], safe="")
        + "@db:5432/sandloom_perf_cb",
        PYTHONPATH="/benchmarks:/examples/helpdesk:/examples/projecthub:/examples/linkly",
        BILLING_PROVIDER="fake",
        CACHE_URL="redis://cache:6379/5",
        BROKER_URL="redis://broker:6379/5",
        INFRA=json.dumps(infra),
    )
    private = exp.out / "mix-seed.env"
    if any("\n" in v for v in env.values()):
        raise RuntimeError("multiline container env requires explicit serialization")
    private.write_text("".join(k + "=" + v + "\n" for k, v in env.items()))
    private.chmod(0o600)
    seed = exp.out / "mix-seed.py"
    seed.write_text(SEED)
    try:
        result = command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                PROJECT + "_default",
                "--env-file",
                str(private),
                "-v",
                str(exp.source / "benchmarks") + ":/benchmarks:ro",
                "-v",
                str(exp.source / "examples") + ":/examples:ro",
                "-v",
                str(seed) + ":/seed.py:ro",
                f"sandloom-perf:cb278f8-{variant}",
                "python",
                "/seed.py",
            ]
        )
    finally:
        private.unlink(missing_ok=True)
    dataset = json.loads(
        next(s[8:] for s in result.splitlines() if s.startswith("DATASET "))
    )
    parts = urlsplit(environment()["DATABASE_URL"])
    app_url = urlunsplit(parts._replace(path="/sandloom_perf_cb_mixfull"))
    runtime = {
        "DATABASE_URL": app_url,
        "SANDLOOM_MODULES": json.dumps(dataset["modules"]),
        "PYTHONPATH": "/benchmarks:/examples/helpdesk:/examples/projecthub:/examples/linkly",
        "CACHE_URL": "redis://cache:6379/5",
        "BROKER_URL": "redis://broker:6379/5",
        "INFRA": json.dumps(infra),
    }
    return dataset, runtime


def mixed(exp, variant):
    from benchsuite import loadclient as lc

    container = json.loads(command(["docker", "inspect", APP]))[0]
    # Published loopback ports can enter via Coolify's network instead of the
    # compose default network. Trust only these exact host bridge addresses.
    gateways = sorted(
        {
            v["Gateway"]
            for v in container["NetworkSettings"]["Networks"].values()
            if v["Gateway"]
        }
    )

    class Clients(lc.Workload):
        def request(self, i):
            raw, expected = super().request(i)
            # Represent distinct synthetic clients behind the one trusted local
            # Docker ingress; retain production's per-IP admission controls.
            ip = f"198.18.{(i // 254) % 256}.{i % 254 + 1}"
            raw = raw.replace(
                b"\r\n\r\n", b"\r\nX-Forwarded-For: " + ip.encode() + b"\r\n\r\n", 1
            )
            return raw, expected

    dataset, env = mix_prepare(exp, variant)
    for profile in exp.args.mix_profiles.split(","):
        for planner in ("false", "true"):
            # Reset only this isolated full-mix DB while no process serves it.
            exp.run.compose("stop", "app", capture_output=True)
            # Reset through a short owner process; seed is idempotent and also
            # records the exact baseline counts before every measured cell.
            seeded, env = mix_prepare(exp, variant)
            assert seeded["rows"] == dataset["rows"], "dataset baseline drift"
            exp.switch(
                variant,
                extra_env={
                    **env,
                    "PLANNER_OPTIMIZED_EXECUTOR": planner,
                    "TRUSTED_PROXY_IPS": json.dumps(
                        [gateway + "/32" for gateway in gateways]
                    ),
                    "TRUST_PROXY_HEADERS": "true",
                },
            )
            token = command(
                [
                    "docker",
                    "exec",
                    APP,
                    "python",
                    "-c",
                    "from benchsuite.mix import mint;print(mint(hours=24,tenant=True))",
                ]
            )
            entries = [
                {
                    "method": dataset["catalog"][key][0],
                    "path": dataset["catalog"][key][1],
                    "body": dataset["catalog"][key][2],
                    "expect": dataset["catalog"][key][3],
                    "weight": weight,
                }
                for key, weight in dataset["profiles"][profile].items()
            ]
            admission = None
            if profile == "mixed":
                login = json.loads(dataset["catalog"]["login"][2])
                same = [
                    request(
                        exp,
                        "POST",
                        "/auth/login",
                        body=login,
                        headers={"X-Forwarded-For": "198.19.0.1"},
                    )[0]
                    for _ in range(6)
                ]
                other = request(
                    exp,
                    "POST",
                    "/auth/login",
                    body=login,
                    headers={"X-Forwarded-For": "198.19.0.2"},
                )[0]
                admission = {
                    "one_ip_statuses": same,
                    "another_ip_status": other,
                    "trusted_ingresses": gateways,
                }
                (exp.out / ("admission-" + planner + ".json")).write_text(
                    json.dumps(admission, indent=2) + "\n"
                )
                if same != [200] * 5 + [429] or other != 200:
                    raise RuntimeError(
                        "client-IP admission control failed; mixed throughput would be invalid"
                    )
            wl = Clients.from_mix(entries, ["Authorization: Bearer " + token])
            # Exercise every catalog request actually selected by the profile.
            smoke = asyncio.run(
                lc.closed_loop(
                    [("127.0.0.1", 18222)],
                    Clients(
                        [
                            lc.Entry(
                                e["method"],
                                e["path"],
                                e["body"],
                                lc.parse_expect(e["expect"]),
                            )
                            for e in entries
                        ],
                        ["Authorization: Bearer " + token],
                    ),
                    10**8,
                    len(entries),
                    1,
                )
            )
            if smoke.errors:
                exp.save(
                    {
                        "section": "mix-preflight",
                        "profile": profile,
                        "planner": planner,
                        **smoke.as_dict(),
                        "passed": False,
                    }
                )
                raise RuntimeError("full mix endpoint preflight failed")
            record = load(
                exp,
                wl,
                label="mix",
                n=4096,
                conc=64,
                database="sandloom_perf_cb_mixfull",
                profile=profile,
                planner=planner,
                dataset=seeded["rows"],
                simulated_clients=True,
                admission_preflight=admission,
            )
            exp.save(record)
