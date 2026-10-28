"""Write-path stand helper (W1-W6, DIAGNOSTIC ONLY): env, token, smoke.

    # 1. as the DB owner: everything S5 needs + the write-stand tables
    python -m benchsuite.setup_db

    # 2. server env (the S5 "uow" env + BENCH_WRITE_DIAG=1; NON-superuser role)
    python -m benchsuite.writes env > /tmp/w.env

    # 3. an access token for tenant A (subscription claim included)
    python -m benchsuite.writes token

    # 4. in-process contract check of every rung (201/200, 404 cross-tenant,
    #    409 + nothing committed on ?fail=true) — a few requests, no load
    python -m benchsuite.writes smoke

    # 5. the socket-CPU command of a rung (ladder/server_bench.py; run later)
    eval "$(python -m benchsuite.writes bench-cmd W5 /tmp/w.env --n 5000 --reps 5)"

``RUNGS`` maps each rung to ``(method, path, body template, expected status)``
in ``server_bench.py`` syntax (``{i}`` = request index, ``{u}`` = random hex,
``{id}`` = an id in ``1..WRITE_COUNTERS_PER_TENANT`` = tenant A's counters).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys

from benchsuite import common, s5

_BODY = '{"name":"w{u}","value":{i}}'
RUNGS: dict[str, tuple[str, str, str, str]] = {
    "W1": ("POST", "/dw/1", _BODY, "201"),
    "W2": ("POST", "/dw/2", _BODY, "201"),
    "W3": ("POST", "/dw/3", _BODY, "201"),
    "W4o": ("POST", "/dw/4o/{id}", "{}", "200"),
    "W4c": ("POST", "/dw/4c/{id}", "{}", "200"),
    "W5": ("POST", "/dw/5", _BODY, "201"),
    "W6": ("POST", "/dw/6", '{"name":"w{u}","value":{i},"key":"k{u}"}', "201"),
    # 20 mapped rows loaded, 10 attribute assignments on each (spec §10.4)
    "WM": ("POST", "/dw/m/{id}", "{}", "200"),
}


def server_env() -> dict[str, str]:
    env = s5.server_env("uow")
    env["BENCH_WRITE_DIAG"] = "1"
    # the relay is a background poller: its CPU would leak into per-op numbers
    env.setdefault("OUTBOX_RELAY_ENABLED", "off")
    # keep the risks the stand and the caller accept (run_cg.sh passes
    # local_storage_in_prod): s5 hard-sets its own
    risks = {"relay_disabled"}
    for value in (env.get("SANDLOOM_ACCEPTED_RISKS"), os.environ.get("SANDLOOM_ACCEPTED_RISKS")):
        risks.update(r.strip() for r in (value or "").strip("[]").split(","))
    env["SANDLOOM_ACCEPTED_RISKS"] = ",".join(sorted(r.strip('"') for r in risks if r))
    return env


def body_of(rung: str, i: int) -> bytes:
    _m, _p, body, _s = RUNGS[rung]
    return body.replace("{i}", str(i)).replace("{u}", os.urandom(6).hex()).encode()


def path_of(rung: str, i: int) -> str:
    from benchsuite.setup_db import WRITE_COUNTERS_PER_TENANT

    return RUNGS[rung][1].replace("{id}", str(i % WRITE_COUNTERS_PER_TENANT + 1))


def bench_cmd(rung: str, env_file: str = "/tmp/w.env", *extra: str) -> list[str]:
    """The ``ladder/server_bench.py`` command line of ``rung`` (socket CPU)."""
    from benchsuite.setup_db import WRITE_COUNTERS_PER_TENANT

    method, path, body, expect = RUNGS[rung]
    target = (
        ["--path", path, "--ids", str(WRITE_COUNTERS_PER_TENANT)]
        if "{id}" in path
        else ["--path", path, "--no-id"]
    )
    return [
        "uv",
        "run",
        "python",
        "ladder/server_bench.py",
        "--env-file",
        env_file,
        "--method",
        method,
        *target,
        "--body",
        body,
        "--expect",
        expect,
        "--header",
        f"Authorization: Bearer {s5.mint()}",
        "--label",
        rung,
        *extra,
    ]


async def _smoke() -> None:
    import httpx

    from benchsuite.apps.sandloom_app import app
    from benchsuite.setup_db import WRITE_COUNTERS_PER_TENANT

    a, _b = common.S5_TENANTS
    json_ct = {"Content-Type": "application/json"}
    good = {"Authorization": f"Bearer {s5.mint(a)}", **json_ct}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w"
        ) as http,
    ):
        for rung, (_m, _p, _b2, expect) in RUNGS.items():
            r = await http.post(
                path_of(rung, 0), content=body_of(rung, 0), headers=good
            )
            if r.status_code != int(expect):
                raise SystemExit(f"[{rung}] {r.status_code} != {expect}: {r.text}")
            r = await http.post(
                path_of(rung, 0) + "?fail=true", content=body_of(rung, 1), headers=good
            )
            if r.status_code != 409:
                raise SystemExit(f"[{rung}] forced failure: {r.status_code} {r.text}")
            r = await http.post(path_of(rung, 0), content=b"{}", headers=json_ct)
            if r.status_code != 401:
                raise SystemExit(f"[{rung}] no token: {r.status_code}")
        cross = f"/dw/4c/{WRITE_COUNTERS_PER_TENANT + 1}"  # tenant B's counter
        r = await http.post(cross, content=b"{}", headers=good)
        if r.status_code != 404:
            raise SystemExit(f"cross-tenant update: {r.status_code} {r.text}")
    print(f"write stand: contract OK ({len(RUNGS)} rungs)")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "help"
    if cmd == "env":
        for k, v in server_env().items():
            print(f"export {k}={shlex.quote(v)}")
    elif cmd == "token":
        print(s5.mint(argv[1] if len(argv) > 1 else common.S5_TENANTS[0]))
    elif cmd == "smoke":
        os.environ.update(server_env())
        asyncio.run(_smoke())
    elif cmd == "bench-cmd":  # bench-cmd <rung> [env-file] [server_bench args...]
        print(shlex.join(bench_cmd(argv[1], *argv[2:])))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
