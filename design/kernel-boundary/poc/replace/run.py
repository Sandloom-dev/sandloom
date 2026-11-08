"""kb-poc-replace driver: boot a topology of real uvicorn processes running
``sandloom.main:app`` with the fixture modules of ``app/pocapp``, then run the
experiments over HTTP. Nothing under ``src/`` is touched.

Usage (from any directory; the interpreter decides which kernel is tested):

    SANDLOOM_POC_PG=postgresql+asyncpg://poc_app:poc_app@127.0.0.1:38433/poc \
    SANDLOOM_POC_REDIS=redis://127.0.0.1:38380 \
    <python> run.py <experiment> [<experiment> ...]

Experiments: A-mono A-forged-first A-noncritical A-split A-squat
             B-mono B-no-grants B-split  C-mono C-split C-edge-alone
             C-squat C-duplicate
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

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
LOGS = Path(
    os.environ.get("SANDLOOM_POC_LOGS") or tempfile.mkdtemp(prefix="kbpoc-logs-")
)
PG = os.environ["SANDLOOM_POC_PG"]
REDIS = os.environ["SANDLOOM_POC_REDIS"]

ADMIN = "00000000-0000-4000-8000-0000000ad001"
USER = "00000000-0000-4000-8000-0000000a0002"
T_A = "0a000000-0000-4000-8000-00000000000a"
T_B = "0b000000-0000-4000-8000-00000000000b"
T_FOREIGN = "0f000000-0000-4000-8000-00000000000f"
NAMES = {ADMIN: "ADMIN", USER: "USER", T_A: "T_A", T_B: "T_B", T_FOREIGN: "T_FOREIGN"}

M = "pocapp.modules."
ALL = [
    f"{M}test_identity",
    f"{M}forged",
    f"{M}grants",
    f"{M}notes",
    f"{M}inventory",
    f"{M}shop",
]


def short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    for raw, name in NAMES.items():
        text = text.replace(raw, name)
    return text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------


class Proc:
    def __init__(self, label: str, modules: list[str], env: dict[str, str]) -> None:
        self.label = label
        self.modules = modules
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
                    "sandloom.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--log-level",
                    "warning",
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
                raise RuntimeError(
                    f"{self.label} exited:\n{self.log.read_text()[-4000:]}"
                )
            try:
                if httpx.get(self.url + "/ready", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"{self.label} not ready:\n{self.log.read_text()[-4000:]}")

    def stop(self) -> None:
        if self.popen is not None and self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(10)
            except subprocess.TimeoutExpired:
                self.popen.kill()


def base_env(redis_db: int) -> dict[str, str]:
    return {
        "PYTHONPATH": str(APP),
        "DATABASE_URL": PG,
        "CACHE_URL": f"{REDIS}/{redis_db}",
        "BROKER_URL": f"{REDIS}/{redis_db + 1}",
        "EVENT_BUS_BACKEND": "memory",
        "ENVIRONMENT": "dev",
        "OUTBOX_RELAY_ENABLED": "off",
        "SANDLOOM_TEST_OWNER": "kb-poc-replace",
        "INTERNAL_API_TOKEN": "kbpoc-internal-token-0123456789abcdef",
        "RATE_LIMIT_ENABLED": "false",
        "POC_ADMINS": ADMIN,
        "LOG_FORMAT": "text",
        # Postgres row security on the default database (the app role poc_app
        # is neither superuser, BYPASSRLS nor the table owner: setup_db.py).
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


def mono(label: str, modules: list[str], **extra: str) -> list[Proc]:
    env = base_env(3)
    env["JWT_SECRET"] = "kbpoc-not-a-real-secret-0123456789abcdef0123"
    env.update(extra)
    return [Proc(label, modules, env)]


def split(
    label: str,
    p1_modules: list[str],
    p2_modules: list[str],
    p1_bindings: dict[str, dict[str, Any]],
) -> list[Proc]:
    """P1 (issuer + consumer) and P2 (providers), real internal RPC: per-pair
    MAC (X25519), Ed25519 command signatures, service_id pins, allowlist.
    Access tokens EdDSA; only P1 (the issuer) holds the private key."""
    jwt_key = generate_private_key("EdDSA")
    rpc = {s: generate_private_key("EdDSA") for s in ("p1", "p2")}
    dh = {s: generate_x25519_key() for s in ("p1", "p2")}
    p1 = Proc(f"{label}-p1", p1_modules, {})
    p2 = Proc(f"{label}-p2", p2_modules, {})
    bindings = {
        name: {"base_url": p2.url, "service_id": "p2", **extra}
        for name, extra in p1_bindings.items()
    }
    common = {
        **base_env(3),
        "JWT_ALGORITHM": "EdDSA",
        "JWT_KID": "kbpoc-1",
        "JWT_PUBLIC_KEYS": json.dumps({"kbpoc-1": public_pem(jwt_key)}),
        "JWT_PREVIOUS_SECRETS": "[]",
        "RPC_COMMANDS_REQUIRE_SIGNATURE": "on",
        "RPC_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps(
            {s: {"k1": public_pem(k)} for s, k in rpc.items()}
        ),
        "RPC_ALLOWED_CALLS": json.dumps({"p1": sorted(bindings)}),
        "RPC_DH_KEY_ID": "d1",
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {s: {"d1": x25519_public_pem(k)} for s, k in dh.items()}
        ),
        "SERVICE_RPC_TIMEOUT_SECONDS": "5",
    }
    p1.env = {
        **common,
        "RPC_SERVICE_ID": "p1",
        "SANDLOOM_SERVICE_NAME": "p1",
        "RPC_PRIVATE_KEY": private_pem(rpc["p1"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(dh["p1"]),
        "JWT_PRIVATE_KEY": private_pem(jwt_key),
        "JWT_SECRET": "",
        "SERVICE_BINDINGS": json.dumps(bindings),
    }
    p2.env = {
        **common,
        "RPC_SERVICE_ID": "p2",
        "SANDLOOM_SERVICE_NAME": "p2",
        "RPC_PRIVATE_KEY": private_pem(rpc["p2"]),
        "RPC_DH_PRIVATE_KEY": x25519_private_pem(dh["p2"]),
        "JWT_PRIVATE_KEY": "",
        "JWT_SECRET": "",
        "SERVICE_BINDINGS": "{}",
    }
    return [p1, p2]


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def post(url: str, path: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(url + path, json=body, timeout=20)


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
    r = httpx.get(url + path, headers=h, timeout=20)
    text = r.text if len(r.text) < 220 else r.text[:220] + "..."
    return f"{r.status_code} {short(text)}"


def show(label: str, value: Any) -> None:
    print(f"  {label}: {short(value)}")


def show_mint(label: str, status: int, body: Any) -> str | None:
    if status == 200:
        c = body["claims"]
        keep = {
            k: c[k]
            for k in (
                "roles",
                "permissions",
                "tenant_ids",
                "claims_sources",
                "claims_sources_failed",
            )
            if k in c
        }
        show(label, f"mint 200 claims={json.dumps(keep)}")
        return str(body["access_token"])
    show(label, f"mint {status} {body if isinstance(body, str) else json.dumps(body)}")
    return None


def gates(label: str, url: str, token: str | None) -> None:
    for path in (
        "/api/v1/shop/role-admin",
        "/api/v1/shop/perm-write",
        "/api/v1/shop/platform",
        "/api/v1/platform/shop/services",
    ):
        show(f"{label} GET {path}", get(url, path, token))


# --------------------------------------------------------------------------
# A: identity replaced by test_identity (claims contributors)
# --------------------------------------------------------------------------


def forge(url: str, payload: dict[str, Any]) -> None:
    r = post(url, "/api/v1/forged/set", {"payload": payload})
    assert r.status_code == 200, r.text


def exp_a(issuer: str, forged_at: str, title: str) -> None:
    print(f"== {title}")
    forge(forged_at, {})
    tok = show_mint("A1 ADMIN, forged answers {}", *mint(issuer, ADMIN))
    gates("A1 ADMIN", issuer, tok)
    tok = show_mint("A1 USER, forged answers {}", *mint(issuer, USER))
    gates("A1 USER", issuer, tok)
    for payload in (
        {"roles": ["admin"], "permissions": ["perm.poc.write"]},
        {"permissions": ["perm.poc.write"]},
        {"claims_sources": ["identity"]},
        {"provenance_authoritative": False},
        {"user_id": ADMIN},
        {"sub": ADMIN},
    ):
        forge(forged_at, payload)
        tok = show_mint(
            f"A2 USER, forged writes {json.dumps(payload)}", *mint(issuer, USER)
        )
        if tok:
            gates("A2 USER", issuer, tok)
    forge(forged_at, {})


def exp_a_noncritical(url: str) -> None:
    print("== A3 test_identity registered NON-critical and down; forged writes roles")
    forge(url, {"roles": ["admin"], "permissions": ["perm.poc.write"]})
    post(url, "/api/v1/test_identity/down", {"on": True})
    tok = show_mint("A3 USER", *mint(url, USER))
    gates("A3 USER", url, tok)
    post(url, "/api/v1/test_identity/down", {"on": False})
    forge(url, {})
    tok = show_mint("A3 USER, test_identity up again", *mint(url, USER))


def exp_a_critical_down(url: str) -> None:
    print("== A4 test_identity critical and down")
    post(url, "/api/v1/test_identity/down", {"on": True})
    show_mint("A4 ADMIN", *mint(url, ADMIN))
    post(url, "/api/v1/test_identity/down", {"on": False})


def exp_a_squat(url: str) -> None:
    print("== A6 the same issuer under the short name 'identity' (app-local)")
    tok = show_mint("A6 ADMIN", *mint(url, ADMIN, issuer="identity"))
    for path in ("/api/v1/shop/platform", "/api/v1/platform/shop/services"):
        show(f"A6 ADMIN GET {path}", get(url, path, tok))


# --------------------------------------------------------------------------
# B: tenancy replaced by an independent tenant-grant provider
# --------------------------------------------------------------------------


def grant(url: str, user: str, tenant: str, op: str = "grant") -> str:
    r = post(url, f"/api/v1/grants/{op}", {"user_id": user, "tenant_id": tenant})
    return f"{r.status_code} {short(r.text)[:160]}"


def tenant_probe(label: str, url: str, tok: str | None) -> None:
    show(f"{label} /shop/tenant no header", get(url, "/api/v1/shop/tenant", tok))
    for t in (T_A, T_B, T_FOREIGN):
        show(
            f"{label} /shop/tenant X-Tenant-Id {NAMES[t]}",
            get(url, "/api/v1/shop/tenant", tok, X_Tenant_Id=t),
        )
    show(
        f"{label} /notes/items X-Tenant-Id T_A",
        get(url, "/api/v1/notes/items", tok, X_Tenant_Id=T_A),
    )
    show(
        f"{label} /notes/items X-Tenant-Id T_B",
        get(url, "/api/v1/notes/items", tok, X_Tenant_Id=T_B),
    )


def exp_b(
    issuer: str, provider: str, title: str, stop_provider: Callable[[], None] | None
) -> None:
    print(f"== {title}")
    forge(provider, {})
    show("B0 grant USER T_A", grant(provider, USER, T_A))
    tok = show_mint("B1 USER", *mint(issuer, USER))
    tenant_probe("B1 USER", issuer, tok)
    show(
        "B2 no token /shop/tenant X-Tenant-Id T_A",
        get(issuer, "/api/v1/shop/tenant", None, X_Tenant_Id=T_A),
    )
    tok_nogrant = show_mint("B2 ADMIN (no grant)", *mint(issuer, ADMIN))
    tenant_probe("B2 ADMIN (no grant)", issuer, tok_nogrant)
    for payload in (
        {"tenant_id": T_FOREIGN},
        {"org_id": T_FOREIGN},
        {"tenant_ids": [T_FOREIGN]},
    ):
        forge(provider, payload)
        t = show_mint(
            f"B3 USER, forged writes {json.dumps(payload)}", *mint(issuer, USER)
        )
        if t:
            tenant_probe("B3 USER", issuer, t)
    forge(provider, {})
    # revocation: a token minted with T_A, then the grant is revoked
    show("B5 revoke USER T_A", grant(provider, USER, T_A, op="revoke"))
    show(
        "B5 old token /shop/tenant X-Tenant-Id T_A",
        get(issuer, "/api/v1/shop/tenant", tok, X_Tenant_Id=T_A),
    )
    show(
        "B5 old token /notes/items X-Tenant-Id T_A",
        get(issuer, "/api/v1/notes/items", tok, X_Tenant_Id=T_A),
    )
    time.sleep(2.5)  # past AUTH_REVOCATION_CLOCK_SKEW_SECONDS (2.0 default)
    tok2 = show_mint("B5 USER re-mint after revoke", *mint(issuer, USER))
    show(
        "B5 new token /shop/tenant X-Tenant-Id T_A",
        get(issuer, "/api/v1/shop/tenant", tok2, X_Tenant_Id=T_A),
    )
    # provider down
    show("B4 grant USER T_B", grant(provider, USER, T_B))
    tok3 = show_mint("B4 USER before provider down", *mint(issuer, USER))
    post(provider, "/api/v1/grants/down", {"on": True})
    show_mint("B4 USER, provider down (fault switch)", *mint(issuer, USER))
    show(
        "B4 token minted before the fault /shop/tenant X-Tenant-Id T_B",
        get(issuer, "/api/v1/shop/tenant", tok3, X_Tenant_Id=T_B),
    )
    forge(provider, {"tenant_ids": [T_FOREIGN]})
    show_mint("B4 USER, provider down + forged writes tenant_ids", *mint(issuer, USER))
    forge(provider, {})
    post(provider, "/api/v1/grants/down", {"on": False})
    if stop_provider is not None:
        stop_provider()
        show_mint("B4 USER, provider PROCESS stopped", *mint(issuer, USER))
        show(
            "B4 token minted before /shop/tenant X-Tenant-Id T_B",
            get(issuer, "/api/v1/shop/tenant", tok3, X_Tenant_Id=T_B),
        )


def exp_b_no_grants(url: str) -> None:
    print("== B6 no tenant-grant provider deployed; forged writes tenant_ids")
    forge(url, {})
    tok = show_mint("B6 USER, forged answers {}", *mint(url, USER))
    tenant_probe("B6 USER", url, tok)
    forge(url, {"tenant_ids": [T_FOREIGN, T_B]})
    tok = show_mint(
        "B6 USER, forged writes tenant_ids [T_FOREIGN, T_B]", *mint(url, USER)
    )
    tenant_probe("B6 USER", url, tok)
    forge(url, {})


# --------------------------------------------------------------------------
# C: billing replaced by an unrelated fact inventory.stock.available
# --------------------------------------------------------------------------


def exp_c(
    url: str, title: str, stop_provider: Callable[[], None] | None = None
) -> None:
    print(f"== {title}")
    status, body = mint(url, USER)
    tok = body["access_token"] if status == 200 else None
    for path in (
        "/api/v1/shop/items/sku-in/buy",
        "/api/v1/shop/items/sku-out/buy",
        "/api/v1/shop/items/sku-in/buy?qty=5",
        "/api/v1/shop/items/sku-in/buy?qty=6",
        "/api/v1/shop/items/sku-none/buy",
    ):
        show(f"C1 GET {path}", get(url, path, tok))
    show(
        "C1 no token GET /api/v1/shop/items/sku-in/buy",
        get(url, "/api/v1/shop/items/sku-in/buy", None),
    )
    if stop_provider is not None:
        stop_provider()
        for path in ("/api/v1/shop/items/sku-in/buy", "/api/v1/shop/items/sku-out/buy"):
            show(f"C2 provider stopped GET {path}", get(url, path, tok))


# --------------------------------------------------------------------------


def run(name: str) -> None:
    print(f"\n######## {name}   kernel: {os.path.dirname(sandloom.__file__)}")
    if name == "A-mono":
        with topology(mono("A-mono", ALL)) as (p,):
            exp_a(p.url, p.url, "A monolith: test_identity, forged registered after it")
            exp_a_critical_down(p.url)
    elif name == "A-forged-first":
        order = [f"{M}forged", f"{M}test_identity", *ALL[2:]]
        with topology(mono("A-forged-first", order)) as (p,):
            exp_a(p.url, p.url, "A monolith: forged registered BEFORE test_identity")
    elif name == "A-noncritical":
        with topology(mono("A-noncritical", ALL, POC_TEST_IDENTITY_CRITICAL="0")) as (
            p,
        ):
            exp_a_noncritical(p.url)
    elif name == "A-split":
        procs = split(
            "A-split",
            [f"{M}test_identity", f"{M}notes", f"{M}shop"],
            [f"{M}grants", f"{M}inventory", f"{M}forged"],
            {
                "grants.claims.v1": {},
                "forged.claims.v1": {"critical": False},
                "inventory.facts.v1": {},
            },
        )
        with topology(procs) as (p1, p2):
            exp_a(
                p1.url,
                p2.url,
                "A split: forged is a REMOTE contributor (forged.claims.v1)",
            )
    elif name == "A-squat":
        with topology(mono("A-squat", [f"{M}identity", f"{M}shop"])) as (p,):
            exp_a_squat(p.url)
    elif name == "B-mono":
        with topology(mono("B-mono", ALL)) as (p,):
            exp_b(p.url, p.url, "B monolith", None)
    elif name == "B-no-grants":
        mods = [f"{M}test_identity", f"{M}forged", f"{M}notes", f"{M}shop"]
        with topology(mono("B-no-grants", mods)) as (p,):
            exp_b_no_grants(p.url)
    elif name == "B-split":
        procs = split(
            "B-split",
            [f"{M}test_identity", f"{M}notes", f"{M}shop"],
            [f"{M}grants", f"{M}inventory", f"{M}forged"],
            {
                "grants.claims.v1": {},
                "forged.claims.v1": {"critical": False},
                "inventory.facts.v1": {},
            },
        )
        with topology(procs) as (p1, p2):
            exp_b(
                p1.url,
                p2.url,
                "B split: grants in P2 over grants.claims.v1 (critical)",
                p2.stop,
            )
    elif name == "C-mono":
        with topology(mono("C-mono", ALL)) as (p,):
            exp_c(p.url, "C monolith: inventory owns the fact, shop gates on it")
    elif name == "C-split":
        procs = split(
            "C-split",
            [f"{M}test_identity", f"{M}notes", f"{M}shop"],
            [f"{M}grants", f"{M}inventory", f"{M}forged"],
            {"grants.claims.v1": {}, "inventory.facts.v1": {}},
        )
        with topology(procs) as (p1, p2):
            exp_c(
                p1.url,
                "C split: shop in P1, inventory in P2 over inventory.facts.v1",
                p2.stop,
            )
    elif name == "C-squat":
        mods = [f"{M}test_identity", f"{M}forged", f"{M}shop"]
        with topology(mono("C-squat", mods, POC_FORGED_FACT="1")) as (p,):
            exp_c(
                p.url,
                "C forged registers inventory.stock.available (owner 'inventory'); "
                "inventory not loaded",
            )
    elif name == "C-duplicate":
        mods = [f"{M}test_identity", f"{M}inventory", f"{M}forged", f"{M}shop"]
        with topology(mono("C-duplicate", mods, POC_FORGED_FACT="1")) as (p,):
            exp_c(p.url, "C inventory AND forged register the fact")
    elif name == "C-edge-alone":
        with topology(mono("C-edge-alone", [f"{M}test_identity", f"{M}shop"])) as (p,):
            exp_c(p.url, "C inventory neither loaded nor bound")
    else:
        raise SystemExit(f"unknown experiment {name}")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        run(arg)
