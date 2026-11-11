"""kb-poc-trust driver: boot real uvicorn processes of a PoC composition root
(``pocapp.roots.<root>:app``) with the fixture modules, run the T-rows over
HTTP and print what each answered. Extends ``poc/replace/run.py`` (same
fixtures, same mono / two-process split over real internal RPC).

    SANDLOOM_POC_PG=postgresql+asyncpg://poc_app:poc_app@127.0.0.1:38533/poc \
    SANDLOOM_POC_REDIS=redis://127.0.0.1:38480 \
    <python of the kernel under test> run.py <experiment> [...]

The interpreter decides the kernel (TA-1 patched wheel, or the base wheel for
``legacy-base-mint``). ``--root ta0`` runs an experiment on the TA-0 control.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt

import sandloom
from sandloom.sdk.security import (
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)

HERE = Path(__file__).resolve().parent
APP = HERE / "app"
LOGS = Path(os.environ.get("SANDLOOM_POC_LOGS") or tempfile.mkdtemp(prefix="kbtrust-"))
PG = os.environ["SANDLOOM_POC_PG"]
REDIS = os.environ["SANDLOOM_POC_REDIS"]
JWT_SECRET = "kbtrust-not-a-real-secret-0123456789abcdef0123"
INTERNAL = "kbtrust-internal-token-0123456789abcdef"

ADMIN = "00000000-0000-4000-8000-0000000ad001"
USER = "00000000-0000-4000-8000-0000000a0002"
T_A = "0a000000-0000-4000-8000-00000000000a"
T_B = "0b000000-0000-4000-8000-00000000000b"
T_FOREIGN = "0f000000-0000-4000-8000-00000000000f"
NAMES = {ADMIN: "ADMIN", USER: "USER", T_A: "T_A", T_B: "T_B", T_FOREIGN: "T_FOREIGN"}

M = "pocapp.modules."
TI, FORGED, GRANTS, NOTES, INV, SHOP, IDENT = (
    f"{M}test_identity",
    f"{M}forged",
    f"{M}grants",
    f"{M}notes",
    f"{M}inventory",
    f"{M}shop",
    f"{M}identity",
)
ROOT = "ta1"  # --root


def short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    for raw, name in NAMES.items():
        text = text.replace(raw, name)
    return text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def show(label: str, value: Any) -> None:
    print(f"  {label}: {short(value)}")


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------


class Proc:
    def __init__(
        self, label: str, modules: list[str], env: dict[str, str], root: str | None = None
    ) -> None:
        self.label = label
        self.modules = modules
        self.root = root or ROOT
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = env
        self.popen: subprocess.Popen[bytes] | None = None
        self.log = LOGS / f"{label}.log"

    def start(self) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SANDLOOM_POC")}
        env.update(self.env)
        env["SANDLOOM_MODULES"] = json.dumps(self.modules)
        with self.log.open("wb") as fh:
            self.popen = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    f"pocapp.roots.{self.root}:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--log-level",
                    self.env.get("POC_UVICORN_LOG", "warning"),
                ],
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=str(HERE),
            )

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.popen is not None
            if self.popen.poll() is not None:
                raise RuntimeError(f"{self.label} exited:\n{self.tail()}")
            try:
                if httpx.get(self.url + "/ready", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"{self.label} not ready:\n{self.tail()}")

    def tail(self, n: int = 4000) -> str:
        return self.log.read_text(errors="replace")[-n:]

    def lines(self, pattern: str) -> list[str]:
        return [
            line
            for line in self.log.read_text(errors="replace").splitlines()
            if re.search(pattern, line)
        ]

    def stop(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(10)
            except subprocess.TimeoutExpired:
                self.popen.kill()


def base_env(redis_db: int = 3) -> dict[str, str]:
    return {
        "PYTHONPATH": str(APP),
        "DATABASE_URL": PG,
        "CACHE_URL": f"{REDIS}/{redis_db}",
        "BROKER_URL": f"{REDIS}/{redis_db + 1}",
        "EVENT_BUS_BACKEND": "memory",
        "ENVIRONMENT": "dev",
        "OUTBOX_RELAY_ENABLED": "off",
        "SANDLOOM_TEST_OWNER": "kb-poc-trust",
        "INTERNAL_API_TOKEN": INTERNAL,
        "RATE_LIMIT_ENABLED": "false",
        "POC_ADMINS": ADMIN,
        "LOG_FORMAT": "text",
        "INFRA": json.dumps({"database": {"default": {"tenant_rls": True}}}),
    }


@contextmanager
def topology(procs: list[Proc]) -> Iterator[list[Proc]]:
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.wait_ready()
        yield procs
    finally:
        for p in procs:
            p.stop()


def refused(proc: Proc, pattern: str = r"SANDLOOM-B0\d\d|Error|refus") -> None:
    """Start ``proc`` expecting a startup refusal; print the refusal lines."""
    proc.start()
    assert proc.popen is not None
    try:
        proc.popen.wait(60)
    except subprocess.TimeoutExpired:
        try:
            ok = httpx.get(proc.url + "/ready", timeout=2).status_code
        except httpx.HTTPError:
            ok = "no answer"
        show(f"{proc.label}", f"STARTED (no refusal), /ready {ok}")
        proc.stop()
        return
    show(f"{proc.label} exit code", proc.popen.returncode)
    seen: list[str] = []
    for line in proc.log.read_text(errors="replace").splitlines():
        if re.search(pattern, line) and "Traceback" not in line:
            text = line.strip()
            text = text if len(text) < 420 else text[:420] + "..."
            if text not in seen:
                seen.append(text)
    for text in seen[-3:]:
        show(f"{proc.label} log", text)


def mono(label: str, modules: list[str], root: str | None = None, **extra: str) -> Proc:
    env = base_env(3)
    env["JWT_SECRET"] = JWT_SECRET
    env.update(extra)
    return Proc(label, modules, env, root)


class Keys:
    def __init__(self) -> None:
        self.jwt = generate_private_key("EdDSA")
        self.rpc = {s: generate_private_key("EdDSA") for s in ("p1", "p2", "p3")}
        self.dh = {s: generate_x25519_key() for s in ("p1", "p2", "p3")}


def split(
    label: str,
    p1_modules: list[str],
    p2_modules: list[str],
    p1_bindings: dict[str, dict[str, Any]],
    *,
    keys: Keys | None = None,
    p1_extra: dict[str, str] | None = None,
    p2_extra: dict[str, str] | None = None,
    p2_root: str | None = None,
) -> tuple[Proc, Proc, Keys]:
    """P1 (issuer + consumer) and P2 (providers), real internal RPC: per-pair
    MAC (X25519), Ed25519 command signatures, service_id pins, allowlist.
    Access tokens EdDSA; only P1 (the issuer) holds the private key."""
    k = keys or Keys()
    p1 = Proc(f"{label}-p1", p1_modules, {})
    p2 = Proc(f"{label}-p2", p2_modules, {}, p2_root)
    bindings = {
        name: {"base_url": p2.url, "service_id": "p2", **extra}
        for name, extra in p1_bindings.items()
    }
    common = verifier_env(k)
    common["RPC_ALLOWED_CALLS"] = json.dumps({"p1": sorted(bindings)})
    p1.env = {
        **common,
        "RPC_SERVICE_ID": "p1",
        "SANDLOOM_SERVICE_NAME": "p1",
        "RPC_PRIVATE_KEY": private_pem(k.rpc["p1"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(k.dh["p1"]),
        "JWT_PRIVATE_KEY": private_pem(k.jwt),
        "SERVICE_BINDINGS": json.dumps(bindings),
        **(p1_extra or {}),
    }
    p2.env = {
        **common,
        "RPC_SERVICE_ID": "p2",
        "SANDLOOM_SERVICE_NAME": "p2",
        "RPC_PRIVATE_KEY": private_pem(k.rpc["p2"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(k.dh["p2"]),
        "SERVICE_BINDINGS": "{}",
        **(p2_extra or {}),
    }
    return p1, p2, k


def verifier_env(k: Keys) -> dict[str, str]:
    return {
        **base_env(3),
        "JWT_ALGORITHM": "EdDSA",
        "JWT_KID": "kbtrust-1",
        "JWT_PUBLIC_KEYS": json.dumps({"kbtrust-1": public_pem(k.jwt)}),
        "JWT_PREVIOUS_SECRETS": "[]",
        "JWT_SECRET": "",
        "JWT_PRIVATE_KEY": "",
        "RPC_COMMANDS_REQUIRE_SIGNATURE": "on",
        "RPC_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps(
            {s: {"k1": public_pem(v)} for s, v in k.rpc.items()}
        ),
        "RPC_ALLOWED_CALLS": "{}",
        "RPC_DH_KEY_ID": "d1",
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {s: {"d1": x25519_public_pem(v)} for s, v in k.dh.items()}
        ),
        "SERVICE_RPC_TIMEOUT_SECONDS": "5",
        "SERVICE_BINDINGS": "{}",
    }


STANDARD_SPLIT = {
    "grants.claims.v1": {},
    "forged.claims.v1": {"critical": False},
    "inventory.facts.v1": {},
}


def std_split(label: str, **kw: Any) -> tuple[Proc, Proc, Keys]:
    return split(label, [TI, NOTES, SHOP], [GRANTS, INV, FORGED], STANDARD_SPLIT, **kw)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def post(url: str, path: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(url + path, json=body, timeout=30)


def mint(url: str, user: str, issuer: str = "test_identity") -> tuple[int, Any]:
    r = post(url, f"/api/v1/{issuer}/mint", {"user_id": user})
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def get(url: str, path: str, token: str | None, **headers: str) -> str:
    h = {k.replace("_", "-"): v for k, v in headers.items()}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = httpx.get(url + path, headers=h, timeout=30)
    text = r.text if len(r.text) < 240 else r.text[:240] + "..."
    return f"{r.status_code} {short(text)}"


KEEP = (
    "roles",
    "permissions",
    "tenant_ids",
    "claims_sources",
    "claims_sources_failed",
    "cpv",
)


def show_mint(label: str, status: int, body: Any) -> str | None:
    if status == 200:
        c = body["claims"]
        keep = {k: c[k] for k in KEEP if k in c}
        show(label, f"mint 200 claims={json.dumps(keep)}")
        return str(body["access_token"])
    show(label, f"mint {status} {body if isinstance(body, str) else json.dumps(body)}")
    return None


GATES = (
    "/api/v1/shop/role-admin",
    "/api/v1/shop/perm-write",
    "/api/v1/shop/role-and-perm",
    "/api/v1/shop/platform",
    "/api/v1/platform/shop/services",
)


def gates(label: str, url: str, token: str | None, paths: tuple[str, ...] = GATES) -> None:
    for path in paths:
        show(f"{label} GET {path}", get(url, path, token))


def tenant_probe(label: str, url: str, tok: str | None) -> None:
    show(f"{label} /shop/tenant no header", get(url, "/api/v1/shop/tenant", tok))
    for t in (T_A, T_FOREIGN):
        show(
            f"{label} /shop/tenant X-Tenant-Id {NAMES[t]}",
            get(url, "/api/v1/shop/tenant", tok, X_Tenant_Id=t),
        )
    show(
        f"{label} /notes/items X-Tenant-Id T_A",
        get(url, "/api/v1/notes/items", tok, X_Tenant_Id=T_A),
    )


def forge(url: str, payload: dict[str, Any]) -> None:
    r = post(url, "/api/v1/forged/set", {"payload": payload})
    assert r.status_code in (200, 404), r.text  # 404: forged not loaded there


def grant(url: str, user: str, tenant: str, op: str = "grant") -> str:
    r = post(url, f"/api/v1/grants/{op}", {"user_id": user, "tenant_id": tenant})
    return f"{r.status_code} {short(r.text)[:160]}"


def metric(url: str, name: str) -> str:
    r = httpx.get(url + "/metrics", headers={"X-Internal-Token": INTERNAL}, timeout=10)
    for line in r.text.splitlines():
        if name in line and not line.startswith("#"):
            return line
    return f"(no sample of {name}; /metrics {r.status_code})"


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------


def t1(issuer: str, provider: str) -> None:
    forge(provider, {})
    grant(provider, ADMIN, T_A)
    tok = show_mint("T-1 ADMIN", *mint(issuer, ADMIN))
    gates("T-1 ADMIN", issuer, tok)
    tenant_probe("T-1 ADMIN (grant T_A)", issuer, tok)
    tok = show_mint("T-1 USER", *mint(issuer, USER))
    gates("T-1 USER", issuer, tok)


def t2(issuer: str, forged_at: str, proc: Proc) -> None:
    grant(forged_at if forged_at != issuer else issuer, USER, T_A)
    before = len(proc.lines(r"claims contributor 'forged' wrote"))
    for payload in (
        {"roles": ["admin"], "permissions": ["perm.poc.write"], "tenant_ids": [T_FOREIGN]},
        {"permissions": ["perm.poc.write"]},
        {"tenant_ids": [T_FOREIGN]},
        {"claims_sources": ["test_identity"]},
        {"cpv": "1:forged"},
        {"tenant_id": T_FOREIGN},
        {"loyalty_tier": "gold"},
    ):
        forge(forged_at, payload)
        tok = show_mint(f"T-2 USER, forged writes {json.dumps(payload)}", *mint(issuer, USER))
        if tok and "roles" in payload:
            gates("T-2 USER", issuer, tok)
            tenant_probe("T-2 USER", issuer, tok)
    forge(forged_at, {})
    after = proc.lines(r"claims contributor 'forged' wrote")
    show("T-2 ERROR lines naming forged (new)", len(after) - before)
    for line in after[before : before + 2]:
        show("T-2 log", line.split("] ", 1)[-1][:300])
    show("T-2 metric", metric(proc.url, "claims_contributor_violations_total"))


def t3(issuer: str, forged_at: str, title: str) -> None:
    forge(forged_at, {"roles": ["admin"]})
    show_mint(f"T-3 {title}: USER, critical forged writes roles", *mint(issuer, USER))
    show_mint(f"T-3 {title}: ADMIN, critical forged writes roles", *mint(issuer, ADMIN))
    forge(forged_at, {})
    show_mint(f"T-3 {title}: USER, critical forged writes only its own key", *mint(issuer, USER))


def t4(issuer: str, forged_at: str, proc: Proc, n: int = 100) -> None:
    forge(forged_at, {"roles": ["admin"], "jti": "x"})
    before = len(proc.lines(r"claims contributor 'forged' wrote"))
    m0 = metric(proc.url, "claims_contributor_violations_total")
    codes: dict[int, int] = {}
    leaked = 0
    for i in range(n):
        user = str(uuid.UUID(int=0x0A000000000000000000000000000000 + i))
        status, body = mint(issuer, user)
        codes[status] = codes.get(status, 0) + 1
        if status == 200 and "admin" in (body["claims"].get("roles") or []):
            leaked += 1
    forge(forged_at, {})
    after = len(proc.lines(r"claims contributor 'forged' wrote"))
    show(f"T-4 {n} logins", f"status counts {codes}; tokens with roles=admin: {leaked}")
    show("T-4 ERROR lines naming forged", after - before)
    show("T-4 metric before", m0)
    show("T-4 metric after", metric(proc.url, "claims_contributor_violations_total"))


def legacy_tokens(sign: Any) -> dict[str, str]:
    now = int(time.time())
    base = {"sub": ADMIN, "token_type": "access", "iat": now, "nbf": now, "exp": now + 900}
    return {
        "L1 roles+claims_sources, no failed (legacy issuer)": sign(
            {**base, "roles": ["admin"], "permissions": ["perm.poc.write"],
             "claims_sources": ["test_identity"], "jti": uuid.uuid4().hex}
        ),
        "L2 roles+permissions, no provenance": sign(
            {**base, "roles": ["admin"], "permissions": ["perm.poc.write"],
             "jti": uuid.uuid4().hex}
        ),
        "L3 roles+claims_sources+failed=[] (skipped collect_claims)": sign(
            {**base, "roles": ["admin"], "permissions": ["perm.poc.write"],
             "claims_sources": ["test_identity"], "claims_sources_failed": [],
             "jti": uuid.uuid4().hex}
        ),
    }


LEGACY_PATHS = (
    "/api/v1/shop/plain",
    "/api/v1/shop/role-admin",
    "/api/v1/shop/perm-write",
    "/api/v1/shop/role-or-always",
    "/api/v1/shop/not-role",
    "/api/v1/shop/platform",
    "/api/v1/platform/shop/services",
)


def t9(issuer: str, tokens: dict[str, str]) -> None:
    for label, tok in tokens.items():
        for path in LEGACY_PATHS:
            show(f"T-9 {label} GET {path}", get(issuer, path, tok))
    tok = show_mint("T-9 after refresh (re-mint) ADMIN", *mint(issuer, ADMIN))
    gates("T-9 refreshed ADMIN", issuer, tok, LEGACY_PATHS)


def t11(issuer: str) -> None:
    for claims in (
        {"roles": ["admin"], "claims_sources": ["test_identity"], "claims_sources_failed": []},
        {"roles": ["admin"]},
        {"cpv": "1:anything"},
        {"tenant_ids": [T_A]},
        {"email": "x@example.com"},
    ):
        r = post(issuer, "/api/v1/test_identity/hand-mint", {"user_id": ADMIN, "claims": claims})
        body = r.json()
        show(f"T-11 create_access_token({json.dumps(claims)})", body.get("refused") or "TOKEN MINTED")
        if "access_token" in body:
            gates("T-11 hand-minted", issuer, body["access_token"])
    for claims in ({"roles": ["admin"]}, {"tenant_id": T_A}, {}):
        r = post(issuer, "/api/v1/test_identity/service-mint", {"user_id": ADMIN, "claims": claims})
        body = r.json()
        show(f"T-11 create_service_token({json.dumps(claims)})", body.get("refused") or "TOKEN MINTED")
        if "access_token" in body:
            tok = body["access_token"]
            gates("T-11 service token", issuer, tok, ("/api/v1/shop/plain", *GATES))
            show(
                "T-11 service token /shop/tenant X-Tenant-Id T_A",
                get(issuer, "/api/v1/shop/tenant", tok, X_Tenant_Id=T_A),
            )


def t13(issuer: str, forged_at: str) -> None:
    forge(forged_at, {"roles": ["admin"]})
    tok = show_mint("T-13 USER, forged writes roles=admin", *mint(issuer, USER))
    show("T-13 forged Role(admin)", get(issuer, "/api/v1/shop/role-admin", tok))
    show("T-13 platform admin, roles not from the authority", get(issuer, "/api/v1/shop/platform", tok))
    show("T-13 kernel platform API, same", get(issuer, "/api/v1/platform/shop/services", tok))
    show("T-13 ~Entitled(missing.fact), USER", get(issuer, "/api/v1/shop/not-missing", tok))
    forge(forged_at, {})
    tok = show_mint("T-13 ADMIN (trusted roles)", *mint(issuer, ADMIN))
    show("T-13 Role(admin) | Entitled(missing.fact)", get(issuer, "/api/v1/shop/role-or-missing", tok))
    show("T-13 ~Entitled(missing.fact), ADMIN", get(issuer, "/api/v1/shop/not-missing", tok))


# --------------------------------------------------------------------------
# T-14: I/O per request (DB statements, Redis commands, RPC requests)
# --------------------------------------------------------------------------

PG_CONTAINER = os.environ.get("POC_PG_CONTAINER", "kbtrust-pg")
REDIS_CONTAINER = os.environ.get("POC_REDIS_CONTAINER", "kbtrust-redis")


def _pg_logging(on: bool) -> None:
    stmts = (
        [
            "ALTER SYSTEM SET log_statement = 'all'",
            "ALTER SYSTEM SET log_line_prefix = '%m [%p] %u@%d '",
        ]
        if on
        else ["ALTER SYSTEM RESET log_statement", "ALTER SYSTEM RESET log_line_prefix"]
    ) + ["SELECT pg_reload_conf()"]
    args = ["docker", "exec", PG_CONTAINER, "psql", "-U", "poc", "-d", "poc", "-q"]
    for stmt in stmts:
        args += ["-c", stmt]
    subprocess.run(args, check=True, capture_output=True)


def _pg_mark(tag: str) -> None:
    subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "poc_app", "-d", "poc", "-qc",
         f"SELECT '{tag}'"],
        check=True,
        capture_output=True,
        env={**os.environ, "PGPASSWORD": "poc_app"},
    )


def _pg_count(start: str, end: str) -> int:
    out = subprocess.run(
        ["docker", "logs", "--since", "10m", PG_CONTAINER],
        capture_output=True, text=True,
    )
    text = out.stdout + out.stderr
    lines = text.splitlines()
    try:
        a = max(i for i, x in enumerate(lines) if start in x)
        b = max(i for i, x in enumerate(lines) if end in x)
    except ValueError:
        return -1
    return sum(
        1
        for x in lines[a + 1 : b]
        if "poc_app@poc" in x and re.search(r"LOG:  (statement|execute)", x)
    )


class RedisMonitor:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli", "MONITOR"],
            stdout=subprocess.PIPE, text=True,
        )
        time.sleep(0.5)

    def lines(self) -> list[str]:
        self.proc.terminate()
        out, _ = self.proc.communicate(timeout=10)
        return out.splitlines()


def _redis_echo(tag: str) -> None:
    subprocess.run(
        ["docker", "exec", REDIS_CONTAINER, "redis-cli", "-n", "3", "ECHO", tag],
        check=True, capture_output=True,
    )


def t14(issuer: str, provider: Proc | None, label: str, m: int = 20) -> None:
    grant(provider.url if provider else issuer, ADMIN, T_A)
    status, body = mint(issuer, ADMIN)
    tok = body["access_token"]
    paths = {
        "plain (control)": ("/api/v1/shop/plain", {}),
        "Role(admin)": ("/api/v1/shop/role-admin", {}),
        "Perm(perm.poc.write)": ("/api/v1/shop/perm-write", {}),
        "Role & Perm": ("/api/v1/shop/role-and-perm", {}),
        "TenantScope": ("/api/v1/shop/tenant", {"X_Tenant_Id": T_A}),
    }
    for name, (path, hdr) in paths.items():
        for _ in range(3):
            get(issuer, path, tok, **hdr)
        tag = uuid.uuid4().hex[:8]
        rpc0 = len(provider.lines(r"/_sandloom/rpc/")) if provider else 0
        mon = RedisMonitor()
        _redis_echo(f"start-{tag}")
        _pg_mark(f"kbmark-start-{tag}")
        codes = set()
        for _ in range(m):
            codes.add(get(issuer, path, tok, **hdr).split(" ", 1)[0])
        _pg_mark(f"kbmark-end-{tag}")
        _redis_echo(f"end-{tag}")
        time.sleep(0.3)
        lines = mon.lines()
        try:
            a = max(i for i, x in enumerate(lines) if f"start-{tag}" in x)
            b = max(i for i, x in enumerate(lines) if f"end-{tag}" in x)
            # only the fixture's cache database (3); other runs share the server
            redis_cmds = [x for x in lines[a + 1 : b] if " [3 " in x]
        except ValueError:
            redis_cmds = []
        kinds: dict[str, int] = {}
        for x in redis_cmds:
            mm = re.search(r'\] "([A-Za-z]+)"', x)
            if mm:
                kinds[mm.group(1).upper()] = kinds.get(mm.group(1).upper(), 0) + 1
        time.sleep(0.5)
        db = _pg_count(f"kbmark-start-{tag}", f"kbmark-end-{tag}")
        rpc = (len(provider.lines(r"/_sandloom/rpc/")) - rpc0) if provider else 0
        per = {k: v / m for k, v in kinds.items()}
        show(
            f"T-14 {label} {name}",
            f"status {sorted(codes)}; per request: DB {db / m:g}, Redis {per or 0}, RPC {rpc / m:g}",
        )


# --------------------------------------------------------------------------
# experiments
# --------------------------------------------------------------------------

STD = [TI, FORGED, GRANTS, NOTES, SHOP]


def run(name: str) -> None:  # noqa: C901, PLR0912, PLR0915
    print(f"\n######## {name}   root={ROOT}   kernel: {os.path.dirname(sandloom.__file__)}")
    if name == "T1-mono":
        with topology([mono("T1-mono", [TI, GRANTS, NOTES, SHOP])]) as (p,):
            t1(p.url, p.url)
    elif name == "T1-split":
        p1, p2, _ = split("T1-split", [TI, NOTES, SHOP], [GRANTS, INV], {"grants.claims.v1": {}})
        with topology([p1, p2]):
            t1(p1.url, p2.url)
    elif name == "T2-mono":
        with topology([mono("T2-mono", STD)]) as (p,):
            t2(p.url, p.url, p)
    elif name == "T2-forged-first":
        with topology([mono("T2-forged-first", [FORGED, *[m for m in STD if m != FORGED]])]) as (p,):
            t2(p.url, p.url, p)
    elif name == "T2-split":
        p1, p2, _ = std_split("T2-split")
        with topology([p1, p2]):
            t2(p1.url, p2.url, p1)
    elif name == "T3-mono":
        with topology([mono("T3-mono", STD, POC_FORGED_CRITICAL="1")]) as (p,):
            t3(p.url, p.url, "forged registered after test_identity")
        order = [FORGED, *[m for m in STD if m != FORGED]]
        with topology([mono("T3-forged-first", order, POC_FORGED_CRITICAL="1")]) as (p,):
            t3(p.url, p.url, "forged registered BEFORE test_identity")
            print("  log lines:")
            for line in p.lines(r"claims contributor .* wrote")[:2]:
                show("T-3 log", line.split("] ", 1)[-1][:300])
    elif name == "T3-split":
        bindings = {**STANDARD_SPLIT, "forged.claims.v1": {}}
        p1, p2, _ = split("T3-split", [TI, NOTES, SHOP], [GRANTS, INV, FORGED], bindings)
        with topology([p1, p2]):
            t3(p1.url, p2.url, "remote forged.claims.v1, critical binding")
    elif name == "T4-mono":
        with topology([mono("T4-mono", STD)]) as (p,):
            t4(p.url, p.url, p)
    elif name == "T4-split":
        p1, p2, _ = std_split("T4-split")
        with topology([p1, p2]):
            t4(p1.url, p2.url, p1)
    elif name == "T5":
        print("== T-5 (a) authority missing in the issuer: issuer 'identity', test_identity/grants neither loaded nor bound")
        refused(mono("T5-missing", [IDENT, SHOP]), r"SANDLOOM-B035")
        print("== T-5 (b) authority registered non-critical")
        refused(mono("T5-noncritical", [TI, GRANTS, SHOP], POC_TEST_IDENTITY_CRITICAL="0"), r"SANDLOOM-B035")
        print("== T-5 (c) path typo")
        refused(mono("T5-typo", [TI, GRANTS, SHOP], root="typo"), r"SANDLOOM-B035")
        print("== T-5 (d) split: grants.claims.v1 bound critical: false")
        p1, p2, _ = split("T5-split", [TI, SHOP], [GRANTS], {"grants.claims.v1": {"critical": False}})
        p2.start()
        try:
            p2.wait_ready()
            refused(p1, r"SANDLOOM-B035")
        finally:
            p2.stop()
    elif name == "T6-mono":
        with topology(
            [mono("T6-mono", [TI, FORGED, NOTES, SHOP], root="none", POC_TEST_IDENTITY_CRITICAL="0")]
        ) as (p,):
            forge(p.url, {"roles": ["admin"], "permissions": ["perm.poc.write"], "tenant_ids": [T_A, T_FOREIGN]})
            tok = show_mint("T-6 ADMIN, no assignment, forged writes roles/tenant_ids", *mint(p.url, ADMIN))
            gates("T-6 ADMIN", p.url, tok)
            tenant_probe("T-6 ADMIN", p.url, tok)
            forge(p.url, {})
        with topology([mono("T6-critical", [TI, NOTES, SHOP], root="none")]) as (p,):
            show_mint("T-6' no assignment, test_identity CRITICAL writes roles", *mint(p.url, ADMIN))
    elif name == "T6-split":
        p1, p2, _ = split(
            "T6-split", [TI, NOTES, SHOP], [FORGED], {"forged.claims.v1": {"critical": False}},
            p1_extra={"POC_TEST_IDENTITY_CRITICAL": "0"},
        )
        p1.root = p2.root = "none"
        with topology([p1, p2]):
            forge(p2.url, {"roles": ["admin"], "tenant_ids": [T_A, T_FOREIGN]})
            tok = show_mint("T-6 split ADMIN, no assignment", *mint(p1.url, ADMIN))
            gates("T-6 split ADMIN", p1.url, tok)
            tenant_probe("T-6 split ADMIN", p1.url, tok)
    elif name == "T7":
        print("== T-7 (a) app-local module named 'identity' (lean), unassigned, non-critical, grants admin to everyone")
        mods = [TI, IDENT, GRANTS, NOTES, SHOP]
        with topology([mono("T7-squat", mods, POC_IDENTITY_CRITICAL="0", POC_IDENTITY_GRANT_ALL="1")]) as (p,):
            tok = show_mint("T-7a USER minted by 'identity'", *mint(p.url, USER, issuer="identity"))
            gates("T-7a USER", p.url, tok)
            for line in p.lines(r"claims contributor 'identity' wrote")[:1]:
                show("T-7a log", line.split("] ", 1)[-1][:300])
        with topology([mono("T7-squat-crit", mods, POC_IDENTITY_GRANT_ALL="1")]) as (p,):
            show_mint("T-7a' the same, 'identity' critical", *mint(p.url, USER, issuer="identity"))
        print("== T-7 (b) forged passes owner='identity' / owner='test_identity'")
        refused(mono("T7-owner-identity", STD, POC_FORGED_OWNER="identity"), r"SANDLOOM-B034")
        refused(mono("T7-owner-ti", STD, POC_FORGED_OWNER="test_identity"), r"SANDLOOM-B034")
        print("== T-7 (c) forged registered first: see T2-forged-first / T3-forged-first")
    elif name == "T8":
        print("== T-8 (a) P2 hosts forged registering grants.claims.v1 / test_identity.claims.v1")
        refused(mono("T8-squat-grants", [FORGED, SHOP], POC_FORGED_SQUAT="grants"), r"SANDLOOM-B034")
        refused(mono("T8-squat-ti", [FORGED, SHOP], POC_FORGED_SQUAT="test_identity"), r"SANDLOOM-B034")
        print("== T-8 (b) P1 binds grants.claims.v1 to P2 where grants is not loaded")
        p1, p2, _ = split("T8-misbound", [TI, NOTES, SHOP], [INV, FORGED], {"grants.claims.v1": {}})
        with topology([p1, p2]):
            show_mint("T-8b ADMIN", *mint(p1.url, ADMIN))
            for line in p1.lines(r"grants")[-2:]:
                show("T-8b P1 log", line.split("] ", 1)[-1][:300])
    elif name == "T9-mono":
        def sign(c: dict[str, Any]) -> str:
            return jwt.encode(c, JWT_SECRET, algorithm="HS256")

        tokens = legacy_tokens(sign)
        base_tok = os.environ.get("POC_BASE_TOKEN")
        if base_tok:
            tokens["L0 minted by the base (pre-change) kernel, ADMIN"] = base_tok
        with topology([mono("T9-mono", [TI, GRANTS, NOTES, SHOP])]) as (p,):
            t9(p.url, tokens)
            for line in p.lines(r"claim policy mismatch")[:3]:
                show("T-9 log", line.split("] ", 1)[-1][:300])
    elif name == "T9-split":
        k = Keys()
        p1, p2, _ = split("T9-split", [TI, NOTES, SHOP], [GRANTS, INV], {"grants.claims.v1": {}}, keys=k)

        def sign_ed(c: dict[str, Any]) -> str:
            return jwt.encode(c, private_pem(k.jwt), algorithm="EdDSA", headers={"kid": "kbtrust-1"})

        with topology([p1, p2]):
            t9(p1.url, legacy_tokens(sign_ed))
    elif name == "legacy-base-mint":
        # Run with the BASE kernel's interpreter and --root base: print a token.
        with topology([mono("legacy-base", [TI, GRANTS, NOTES, SHOP], root="base")]) as (p,):
            status, body = mint(p.url, ADMIN)
            print("BASE_TOKEN=" + body["access_token"])
            show("base kernel claims", {k: body["claims"].get(k) for k in KEEP})
    elif name == "T10":
        k = Keys()
        p1, p2, _ = split("T10", [TI, NOTES, SHOP], [GRANTS, INV], {"grants.claims.v1": {}}, keys=k)
        p3 = Proc("T10-p3", [SHOP], {**verifier_env(k), "RPC_SERVICE_ID": "p3",
                                     "SANDLOOM_SERVICE_NAME": "p3",
                                     "RPC_PRIVATE_KEY": private_pem(k.rpc["p3"]),
                                     "RPC_DH_PRIVATE_KEY": x25519_private_pem(k.dh["p3"])},
                  root="skew" if ROOT == "ta1" else "skew_ta0")
        with topology([p1, p2, p3]):
            tok = show_mint("T-10 ADMIN minted by P1 (map A)", *mint(p1.url, ADMIN))
            gates("T-10 at P1 (map A)", p1.url, tok, GATES[:3])
            gates("T-10 at P3 (map B)", p3.url, tok, ("/api/v1/shop/plain", *GATES[:4]))
            gates("T-10 at P3 again", p3.url, tok, GATES[:1])
            for proc in (p1, p3):
                for line in proc.lines(r"claim trust: cpv=|claim policy mismatch"):
                    show(f"T-10 {proc.label} log", line.split("] ", 1)[-1][:260])
    elif name == "T11-mono":
        with topology([mono("T11-mono", [TI, GRANTS, NOTES, SHOP])]) as (p,):
            t11(p.url)
    elif name == "T11-split":
        p1, p2, _ = split("T11-split", [TI, NOTES, SHOP], [GRANTS, INV], {"grants.claims.v1": {}})
        with topology([p1, p2]):
            t11(p1.url)
    elif name == "T12-prod":
        print("== T-12 the kernel test authority assigned in a composition without the test plugin")
        refused(mono("T12-testauth", [TI, GRANTS, SHOP], root="testauth"), r"SANDLOOM-B035")
    elif name == "T13-mono":
        with topology([mono("T13-mono", STD)]) as (p,):
            t13(p.url, p.url)
    elif name == "T13-split":
        p1, p2, _ = std_split("T13-split")
        with topology([p1, p2]):
            t13(p1.url, p2.url)
    elif name == "T14":
        _pg_logging(True)
        try:
            with topology([mono("T14-mono", [TI, GRANTS, NOTES, SHOP])]) as (p,):
                t14(p.url, None, "mono")
            p1, p2, _ = split(
                "T14-split", [TI, NOTES, SHOP], [GRANTS, INV], {"grants.claims.v1": {}},
                p2_extra={"POC_UVICORN_LOG": "info"},
            )
            with topology([p1, p2]):
                t14(p1.url, p2, "split")
        finally:
            _pg_logging(False)
    elif name == "T16":
        print("== T-16 R2: an issuer without a core.auth.user_blocked.v1 consumer")
        refused(mono("T16-noblock", [TI, GRANTS, SHOP], POC_TEST_IDENTITY_NO_BLOCK_CONSUMER="1"), r"SANDLOOM-B035")
        print("== T-16 R1: the kernel's durable revocation applier, no identity module loaded")
        with topology([mono("T16-r1", [TI, GRANTS, SHOP])]) as (p,):
            r = httpx.get(
                p.url + "/diagnostics/consumers",
                headers={"X-Internal-Token": INTERNAL},
                timeout=10,
            )
            try:
                body = r.json()
            except ValueError:
                body = r.text
            rows = body if isinstance(body, list) else body.get("consumers", body)
            for row in rows if isinstance(rows, list) else [rows]:
                show("T-16 consumer", row)
    else:
        raise SystemExit(f"unknown experiment {name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="ta1")
    ap.add_argument("experiments", nargs="+")
    a = ap.parse_args()
    ROOT = a.root
    for exp in a.experiments:
        run(exp)
