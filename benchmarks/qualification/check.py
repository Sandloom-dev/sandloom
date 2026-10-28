"""Correctness and failure probes for the isolated qualification compose project."""

from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from run import STATE, compose, token

BASE = "http://127.0.0.1:18222"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RESULTS: list[dict] = []


def response_body(response):
    try:
        return json.load(response)
    except (ValueError, UnicodeDecodeError):
        return {
            "error": "non-JSON response",
            "content_type": response.headers.get("Content-Type"),
        }


def request(path: str, jwt: str | None, body: dict | None = None, headers=None):
    hdrs = {"Authorization": f"Bearer {jwt}"} if jwt else {}
    hdrs.update(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path,
        headers=hdrs,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with HTTP.open(req, timeout=12) as resp:
            return resp.status, response_body(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, response_body(exc)
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return 0, None


def expect(label, path, jwt, status, **kwargs):
    actual, body = request(path, jwt, **kwargs)
    if actual != status:
        raise AssertionError(f"{label}: expected {status}, got {actual}: {body}")
    RESULTS.append({"check": label, "status": actual})
    print(label, actual, flush=True)
    return body


def wait_ready(jwt, timeout=90):
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if request("/s5/1", jwt)[0] == 200 and request("/ready", None)[0] == 200:
            RESULTS.append(
                {"check": "recovered", "seconds": round(time.monotonic() - started, 3)}
            )
            return
        time.sleep(1)
    raise AssertionError("deployment failed to recover")


def sql(query: str) -> str:
    return compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "qualification_owner",
        "-d",
        "qualification",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        query,
        capture_output=True,
        text=True,
    ).stdout.strip()


def counts():
    return sql(
        "SELECT (SELECT count(*) FROM bench_tenant_orders),"
        "(SELECT count(*) FROM bench_tenant_order_lines),"
        "(SELECT count(*) FROM outbox_messages),"
        "(SELECT count(*) FROM audit_log),"
        "(SELECT count(*) FROM contract_command_log)"
    )


def main():
    STATE.joinpath("checks.json").unlink(missing_ok=True)
    jwt = token()
    claims = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
    block_key = "sandloom:auth:blocked:" + claims["sub"]
    wait_ready(jwt)
    expect("own tenant", "/s5/1", jwt, 200)
    expect("foreign tenant", "/s5/5001", jwt, 404)
    expect("unauthenticated", "/s5/1", None, 401)
    expect(
        "forbidden selector",
        "/s5/1",
        jwt,
        403,
        headers={"X-Tenant-Id": "5e000000-0000-4000-8000-00000000000b"},
    )
    role = sql(
        "SELECT rolsuper,rolbypassrls,pg_has_role(rolname,'qualification_owner','member') "
        "FROM pg_roles WHERE rolname='qualification_app'"
    )
    assert role == "f|f|f", role
    assert (
        sql(
            "SELECT count(*) FROM pg_class WHERE relname LIKE 'bench_tenant_%' "
            "AND relkind='r' AND relrowsecurity AND relforcerowsecurity"
        )
        == "5"
    )
    # Exercise RLS with raw SQL as the APP login (not an owner), no ORM filters.
    raw = compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "qualification_app",
        "-d",
        "qualification",
        "-At",
        "-c",
        "SELECT count(*) FROM bench_tenant_items",
        input=(STATE / ".env").read_text().split("APP_PASSWORD=")[1].splitlines()[0]
        + "\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert raw == "0", raw
    RESULTS.append(
        {
            "check": "forced RLS + unprivileged role + raw SQL without tenant",
            "rows": raw,
        }
    )
    body = {
        "name": "qualification-" + uuid.uuid4().hex[:16],
        "value": 7,
        "key": uuid.uuid4().hex,
    }
    before = counts()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request("/dw/6", jwt, body), range(16)))
    assert all(s == 201 for s, _ in responses), responses
    assert len({r["id"] for _, r in responses}) == 1, responses
    after = counts()
    assert [
        int(b) - int(a)
        for a, b in zip(before.split("|"), after.split("|"), strict=True)
    ] == [1, 1, 1, 1, 1], (before, after)
    RESULTS.append(
        {
            "check": "16 concurrent commands, one durable effect",
            "before": before,
            "after": after,
        }
    )
    expect("operation key conflict", "/dw/6", jwt, 409, body={**body, "value": 8})
    before = counts()
    expect(
        "forced rollback",
        "/dw/6?fail=true",
        jwt,
        409,
        body={**body, "key": uuid.uuid4().hex},
    )
    assert counts() == before
    RESULTS.append({"check": "rollback leaves no effect/outbox/audit/dedupe row"})
    # Security records are injected into the synthetic subject's store only.
    compose(
        "exec",
        "-T",
        "security",
        "redis-cli",
        "SET",
        block_key,
        "qualification",
        capture_output=True,
    )
    expect("blocked token", "/s5/1", jwt, 401)
    compose("kill", "-s", "SIGKILL", "security")
    try:
        expect("security Redis outage fails closed", "/s5/1", jwt, 503)
    finally:
        compose("start", "security")
    time.sleep(2)
    expect("block survives abrupt Redis restart", "/s5/1", jwt, 401)
    compose(
        "exec", "-T", "security", "redis-cli", "DEL", block_key, capture_output=True
    )
    wait_ready(jwt)
    compose("stop", "-t", "1", "cache")
    try:
        expect("cache outage does not lose security checks", "/s5/1", jwt, 200)
        expect(
            "write with broker unavailable",
            "/dw/6",
            jwt,
            201,
            body={**body, "key": uuid.uuid4().hex},
        )
        assert (
            int(sql("SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL"))
            >= 1
        )
    finally:
        compose("start", "cache")
    wait_ready(jwt)
    for _ in range(60):
        if (
            sql("SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL")
            == "0"
        ):
            break
        time.sleep(1)
    else:
        raise AssertionError("outbox backlog did not drain")
    RESULTS.append({"check": "outbox recovered after broker outage"})
    compose("stop", "-t", "1", "db")
    try:
        code, _ = request("/s5/1", jwt)
        assert code == 0 or 500 <= code <= 599, code
        RESULTS.append({"check": "DB outage refuses read", "status": code})
    finally:
        compose("start", "db")
    wait_ready(jwt)
    expect("isolation after DB restart", "/s5/5001", jwt, 404)
    sql(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE usename='qualification_app' AND pid<>pg_backend_pid()"
    )
    wait_ready(jwt)
    RESULTS.append({"check": "connection pool recovers from terminated backends"})
    compose("restart", "app")
    wait_ready(jwt)
    expect("durable replay after app restart", "/dw/6", jwt, 201, body=body)
    expect("isolation after app restart", "/s5/5001", jwt, 404)
    assert (
        sql(
            "SELECT count(*) FROM pg_stat_activity WHERE datname='qualification' "
            "AND state='idle in transaction'"
        )
        == "0"
    )
    RESULTS.append({"check": "no idle transactions"})
    STATE.joinpath("checks.json").write_text(json.dumps(RESULTS, indent=2) + "\n")
    print(f"{len(RESULTS)} checks passed", flush=True)


if __name__ == "__main__":
    main()
