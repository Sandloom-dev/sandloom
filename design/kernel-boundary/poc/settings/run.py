"""KB-49 settings PoC driver: run scenarios P2-P9, P11, P14, P15 against real
processes of the D2a candidate (``kernel`` worktree = merged baseline
``3806ad0`` + ``d2a-kernel.patch``) and, where a comparison is asked, of the
base (``3806ad0`` unchanged).

    python run.py --py <candidate python> [--base-py <base python>] P2 P3 ...

Every process runs in an empty temporary directory (no ``.env``) with only
PATH/HOME, PYTHONPATH=fixture/ and the scenario's variables, like the
G-CONFIG-07 harness (``core/tests/test_boot_configuration_checks.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture"
INVALID = "[core.config] Invalid configuration: "
STRONG = "9f1c4e7a2b8d6035e1f94c7ab23d58e06f7b1a9c4d2e85f3071b6c9ae4d2f815"

PY = sys.executable
BASE_PY: str | None = None

DEV: dict[str, Any] = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/boot_checks_test",
    "CACHE_URL": "redis://localhost:6379/3",
    "BROKER_URL": "redis://localhost:6379/4",
    "JWT_SECRET": "boot-checks-not-a-real-secret-0123456789",
    "EVENT_BUS_BACKEND": "memory",
}


def prod_base() -> dict[str, Any]:
    out = subprocess.run(
        [
            PY,
            "-c",
            "import json; from sandloom.core.tests.test_production_profile import _BASE;"
            "print(json.dumps(_BASE))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def env_of(values: dict[str, Any]) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
    env["PYTHONPATH"] = str(FIXTURE)
    for name, value in values.items():
        env[name] = value if isinstance(value, str) else json.dumps(value)
    return env


def run(
    argv: list[str], values: dict[str, Any], *, timeout: float = 120, cwd: Path | None = None
) -> tuple[int | None, str, str]:
    with tempfile.TemporaryDirectory(prefix="kb49-") as tmp:
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd or tmp,
                env=env_of(values),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return None, out, err
    return proc.returncode, proc.stdout, proc.stderr


def cli(py: str, *argv: str) -> list[str]:
    code = f"import sys; sys.argv = ['sandloom', *{list(argv)!r}]; import sandloom; sandloom.main()"
    return [py, "-c", code]


def composed(py: str) -> dict[str, list[str]]:
    """G-CONFIG-07's composed entry points (plus doctor, P2)."""
    return {
        "app": [py, "-c", "import sandloom.main"],
        "worker": [py, "-m", "celery", "-A", "sandloom.core.worker", "worker"],
        "beat": [py, "-m", "celery", "-A", "sandloom.core.worker", "beat"],
        "migrate": cli(py, "migrate", "databases"),
        "cli-outbox": cli(py, "outbox", "list-dead"),
        "cli-streams": cli(py, "streams", "consumers"),
        "cli-projection": cli(py, "projection", "list"),
        "cli-sagas": cli(py, "sagas", "list"),
        "cli-backup": cli(py, "backup", "plan"),
    }


def last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def trim(text: str, n: int = 220) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


RESULTS: list[tuple[str, bool, str]] = []


def record(row: str, ok: bool, detail: str) -> None:
    RESULTS.append((row, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {row}: {detail}", flush=True)


def parallel(jobs: list[tuple[str, Callable[[], Any]]], workers: int = 6) -> dict[str, Any]:
    with ThreadPoolExecutor(workers) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs}
        return {name: f.result() for name, f in futures.items()}


# ---------------------------------------------------------------- P2, P5 ----


def p2_cases(prod: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        "fixture-invalid-value": (
            {**DEV, "SANDLOOM_MODULES": ["inventory"], "INVENTORY_RESERVE_TTL_SECONDS": "0"},
            "module inventory: INVENTORY_RESERVE_TTL_SECONDS: Input should be greater than or equal to 1",
        ),
        "fixture-oversell-in-prod": (
            {**prod, "SANDLOOM_MODULES": ["inventory"], "INVENTORY_ALLOW_OVERSELL": "true"},
            "module inventory: INVENTORY_ALLOW_OVERSELL=true is not allowed with ENVIRONMENT=prod",
        ),
        "billing-provider-unset-in-prod": (
            {**prod, "SANDLOOM_MODULES": ["sandloom_billing"]},
            "module sandloom_billing: BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod",
        ),
        "identity-weak-totp-key": (
            {**prod, "SANDLOOM_MODULES": ["sandloom_identity"], "TWOFA_ENCRYPTION_KEY": "short"},
            "module sandloom_identity: TWOFA_ENCRYPTION_KEY[0] is",
        ),
        "duplicate-env-name": (
            {**DEV, "SANDLOOM_MODULES": ["inventory", "inventory_dup"]},
            "SANDLOOM-B036: INVENTORY_RESERVE_TTL_SECONDS is declared by module inventory and module inventory_dup",
        ),
    }


def scenario_p2() -> None:
    prod = prod_base()
    cases = p2_cases(prod)
    points = composed(PY)
    jobs = []
    for case, (values, _reason) in cases.items():
        for entry, argv in points.items():
            jobs.append((f"{case}|{entry}", lambda a=argv, v=values: run(a, v)))
        jobs.append(
            (
                f"{case}|doctor",
                lambda v=values: run(
                    cli(PY, "doctor", "--json", "--skip", "infra,migrations,consumers,rls,sagas"), v
                ),
            )
        )
    out = parallel(jobs)
    for key, (rc, stdout, stderr) in out.items():
        case, entry = key.split("|")
        reason = cases[case][1]
        if entry == "doctor":
            try:
                report = json.loads(stdout)
            except ValueError:
                record(f"P2 {key}", False, f"rc={rc} no json: {trim(last_line(stderr))}")
                continue
            fails = [c for c in report.get("checks", []) if c.get("status") == "fail"]
            hit = [c for c in fails if reason in str(c.get("message"))]
            record(
                f"P2 {key}",
                bool(hit) and rc != 0,
                f"rc={rc} FAIL {hit[0]['section']}/{hit[0]['name']}: {trim(str(hit[0]['message']), 160)}"
                if hit
                else f"rc={rc} fails={trim(str(fails), 200)}",
            )
            continue
        last = last_line(stderr)
        ok = rc == 1 and last.startswith(INVALID) and reason in last
        record(f"P2 {key}", ok, f"rc={rc} {trim(last)}")
    # controls: each case's environment boots once the value is fixed
    controls = {
        "fixture-invalid-value": {**DEV, "SANDLOOM_MODULES": ["inventory"]},
        "fixture-oversell-in-prod": {**prod, "SANDLOOM_MODULES": ["inventory"]},
        "billing-provider-unset-in-prod": {
            **prod,
            "SANDLOOM_MODULES": ["sandloom_billing"],
            "SANDLOOM_ACCEPTED_RISKS": ["billing.fake_psp_in_prod"],
        },
        "identity-weak-totp-key": {
            **prod,
            "SANDLOOM_MODULES": ["sandloom_identity"],
            "TWOFA_ENCRYPTION_KEY": STRONG,
        },
        "duplicate-env-name": {**DEV, "SANDLOOM_MODULES": ["inventory"]},
    }
    for case, values in controls.items():
        rc, stdout, stderr = run([PY, "-c", "import sandloom.main; print('built')"], values)
        record(f"P2 control {case}", rc == 0 and stdout.strip().endswith("built"), f"rc={rc} {trim(last_line(stdout or stderr))}")


def scenario_p5() -> None:
    values = {
        **DEV,
        "SANDLOOM_MODULES": ["inventory"],
        "MODULE_REQUIRED": [],
        "INVENTORY_RESERVE_TTL_SECONDS": "99999",
    }
    rc, _out, err = run([PY, "-c", "import sandloom.main; print('built')"], values)
    last = last_line(err)
    record(
        "P5 optional (MODULE_REQUIRED=[]) invalid value",
        rc == 1 and last.startswith(INVALID) and "INVENTORY_RESERVE_TTL_SECONDS" in last,
        f"rc={rc} {trim(last)}",
    )
    rc, out, err = run(
        [
            PY,
            "-c",
            "import sandloom.main as m; print(sorted(m.app.state.core.loaded_modules))",
        ],
        {**DEV, "SANDLOOM_MODULES": ["inventory"], "MODULE_REQUIRED": []},
    )
    record("P5 control: optional fixture loads when valid", rc == 0 and "inventory" in out, f"rc={rc} {trim(last_line(out or err))}")


# --------------------------------------------------------------------- P3 ----

DIAG = r"""
import json
from fastapi.testclient import TestClient
import sandloom.main as m
from sandloom.core.observability import metrics
client = TestClient(m.app)
body = client.get('/diagnostics/core', headers={'X-Internal-Token': %r}).json()
cfg = body.get('configuration', body)
risks = cfg.get('accepted_risks') if isinstance(cfg, dict) else None
sample = [s for f in metrics.accepted_risk_active.collect() for s in f.samples if s.labels.get('risk')]
print(json.dumps({'accepted_risks': risks, 'metric': [(s.labels, s.value) for s in sample]}))
"""


def scenario_p3() -> None:
    prod = prod_base()
    token = "5c0d2e9a81f74b36a2c95e07d1f4b8a3e6c2d90f17ab45e8c3d6f0b29e71a4c5"
    values = {
        **prod,
        "SANDLOOM_MODULES": ["inventory"],
        "INVENTORY_ALLOW_OVERSELL": "true",
        "SANDLOOM_ACCEPTED_RISKS": ["inventory.oversell_in_prod"],
        "INTERNAL_API_TOKEN": token,
    }
    warn = "ACCEPTED RISK: inventory.oversell_in_prod"
    jobs = [(e, lambda a=a: run(a, values, timeout=25)) for e, a in composed(PY).items()]
    for entry, (rc, _out, err) in parallel(jobs).items():
        started = INVALID not in err
        logged = warn in err
        record(
            f"P3 {entry}",
            started and logged,
            f"rc={rc if rc is not None else 'running@25s'} config-refusal={'yes' if not started else 'no'} warning={'yes' if logged else 'no'}",
        )
    rc, out, err = run([PY, "-c", DIAG % token], values)
    try:
        data = json.loads(last_line(out))
    except ValueError:
        record("P3 diagnostics+metric", False, f"rc={rc} {trim(last_line(err))}")
    else:
        ids = [r.get("risk") for r in (data["accepted_risks"] or [])]
        metric = [m for m in data["metric"] if m[0].get("risk") == "inventory.oversell_in_prod"]
        record(
            "P3 /diagnostics/core lists it, metric set",
            "inventory.oversell_in_prod" in ids and bool(metric) and metric[0][1] == 1.0,
            f"accepted_risks={ids} metric={metric}",
        )
    rc, out, err = run(cli(PY, "doctor", "--json", "--skip", "infra,migrations,consumers,rls,sagas"), values)
    try:
        checks = json.loads(out)["checks"]
    except (ValueError, KeyError):
        record("P3 doctor lists it", False, f"rc={rc} {trim(last_line(err))}")
    else:
        hit = [c for c in checks if "inventory.oversell_in_prod" in str(c.get("message"))]
        record("P3 doctor lists it", bool(hit), f"{hit[0]['status']} {hit[0]['section']}/{hit[0]['name']}" if hit else "absent")


# --------------------------------------------------------------------- P4 ----

P4_ENTRIES = {
    "app": lambda py: [py, "-c", "import sandloom.main; print('built')"],
    "worker": lambda py: [py, "-c", "from sandloom.core.worker import get_worker_app; get_worker_app(); print('built')"],
    "migrate": lambda py: cli(py, "migrate", "databases"),
}


def scenario_p4() -> None:
    prod = prod_base()
    token = "5c0d2e9a81f74b36a2c95e07d1f4b8a3e6c2d90f17ab45e8c3d6f0b29e71a4c5"
    anchor = {**prod, "INTERNAL_API_TOKEN": token, "BILLING_PROVIDER": "fake"}
    services = {
        "billing-svc": {**anchor, "SANDLOOM_MODULES": ["sandloom_billing", "inventory"]},
        "tenancy-svc": {**anchor, "SANDLOOM_MODULES": ["sandloom_tenancy"]},
    }
    variants = {
        "a-applies": ["billing.fake_psp_in_prod"],
        "b-unknown-namespace": ["billing.fake_psp_in_prod", "bilingg.fake_psp_in_prod"],
        "c-undeclared-id": ["billing.fake_psp_in_prod", "billing.free_money"],
        "d-flat-old-id": ["fake_psp_in_prod"],
    }
    expect: dict[tuple[str, str], tuple[str, str]] = {
        ("a-applies", "billing-svc"): ("start", ""),
        ("a-applies", "tenancy-svc"): ("start", ""),
        ("b-unknown-namespace", "billing-svc"): ("refuse", "no module 'bilingg' is known"),
        ("b-unknown-namespace", "tenancy-svc"): ("refuse", "no module 'bilingg' is known"),
        ("c-undeclared-id", "billing-svc"): ("refuse", "'billing.free_money', which module billing does not declare"),
        # rule (2): billing is known (K1) but not loaded: inert, not checkable here
        ("c-undeclared-id", "tenancy-svc"): ("start", ""),
        ("d-flat-old-id", "billing-svc"): ("refuse", "use 'billing.fake_psp_in_prod'"),
        ("d-flat-old-id", "tenancy-svc"): ("refuse", "use 'billing.fake_psp_in_prod'"),
    }
    jobs = []
    for variant, ids in variants.items():
        for svc, env in services.items():
            for entry, argv in P4_ENTRIES.items():
                values = {**env, "SANDLOOM_ACCEPTED_RISKS": ids}
                jobs.append((f"{variant}|{svc}|{entry}", lambda a=argv(PY), v=values: run(a, v)))
    out = parallel(jobs)
    lasts: dict[tuple[str, str], set[str]] = {}
    for key, (rc, stdout, stderr) in out.items():
        variant, svc, entry = key.split("|")
        want, text = expect[(variant, svc)]
        last = last_line(stderr)
        block = stderr[stderr.find(INVALID):] if INVALID in stderr else ""
        if want == "start":
            ok = INVALID not in stderr and rc == 0
            if entry in ("migrate", "worker"):
                # no database / broker host here: past the composition is enough
                ok = INVALID not in stderr
        else:
            # a module-check refusal is one line; a kernel Settings refusal is
            # pydantic's block (today's format): match the reason in the block
            ok = rc == 1 and bool(block) and text in block
            lasts.setdefault((variant, svc), set()).add(block.strip())
        shown = " ".join(block.split())[:240] if want == "refuse" else (last_line(stdout) or last)
        record(f"P4 {variant} {svc} {entry}", ok, f"rc={rc} {trim(shown, 260)}")
    for (variant, svc), seen in sorted(lasts.items()):
        record(f"P4 {variant} {svc} identical across entry points", len(seen) == 1, f"{len(seen)} distinct message(s)")
    # (a) in the tenancy service: inert and listed "not applicable"
    values = {**services["tenancy-svc"], "SANDLOOM_ACCEPTED_RISKS": variants["a-applies"]}
    rc, outp, err = run([PY, "-c", DIAG % token], values)
    try:
        data = json.loads(last_line(outp))
        rows = data["accepted_risks"] or []
        na = [r for r in rows if r.get("not_applicable")]
        record("P4 a tenancy-svc /diagnostics/core: not applicable", [r["risk"] for r in na] == ["billing.fake_psp_in_prod"], f"rows={rows}")
    except (ValueError, KeyError, TypeError):
        record("P4 a tenancy-svc /diagnostics/core: not applicable", False, f"rc={rc} {trim(last_line(err))}")
    rc, outp, err = run(cli(PY, "doctor", "--json", "--skip", "infra,migrations,consumers,rls,sagas"), values)
    try:
        hit = [c for c in json.loads(outp)["checks"] if "billing.fake_psp_in_prod" in str(c.get("message"))]
        record("P4 a tenancy-svc doctor: not applicable", bool(hit) and "not applicable" in hit[0]["message"], trim(str(hit[0]["message"]) if hit else "absent"))
    except (ValueError, KeyError):
        record("P4 a tenancy-svc doctor: not applicable", False, f"rc={rc} {trim(last_line(err))}")


# --------------------------------------------------------------------- P6 ----


def scenario_p6() -> None:
    values = {
        **DEV,
        "SANDLOOM_MODULES": ["sandloom_billing"],
        "INVENTORY_RESERVE_TTL_SECONDS": "0",  # invalid for inventory, which is not loaded
        "INVENTORY_API_TOKEN": "tok",
    }
    rc, out, err = run([PY, "-c", "import sandloom.main; print('built')"], values)
    record("P6 runtime: unloaded module's variables are inert", rc == 0 and out.strip().endswith("built"), f"rc={rc} {trim(last_line(out or err))}")
    rc, out, err = run(cli(PY, "config", "dump", "--json"), values)
    try:
        names = [e["name"] for e in json.loads(out)]
        record("P6 config dump omits them", not any(n.startswith("INVENTORY_") for n in names), f"rc={rc} INVENTORY_* listed={[n for n in names if n.startswith('INVENTORY_')]}")
    except (ValueError, KeyError):
        record("P6 config dump omits them", False, f"rc={rc} {trim(last_line(err))}")
    services = {
        "shop": {"ENVIRONMENT": "prod", "SANDLOOM_MODULES": json.dumps(["inventory"]), "INVENTORY_RESERVE_TTL_SECONDS": "0"},
        "pay": {"ENVIRONMENT": "prod", "SANDLOOM_MODULES": json.dumps(["sandloom_billing", "invntory"]), "INVENTORY_API_TOKEN": "${INVENTORY_API_TOKEN:?set it}"},
    }
    args = ["check", "settings", "--modules", "billing,identity,tenancy,inventory"]
    for name, env in services.items():
        args += ["--service", f"{name}:{json.dumps(env)}"]
    rc, out, err = run(cli(PY, *args), {})
    text = out
    checks = {
        "wrong value for the topology that loads it": "shop: module inventory: INVENTORY_RESERVE_TTL_SECONDS" in text,
        "wrong module name in SANDLOOM_MODULES": "pay: SANDLOOM_MODULES names unknown module 'invntory'" in text,
        "variable set where the module is not loaded": "pay: sets ['INVENTORY_API_TOKEN'] for module inventory" in text,
    }
    for label, ok in checks.items():
        record(f"P6 sandloom check settings: {label}", ok and rc == 1, f"rc={rc}")
    print(text.strip())


# --------------------------------------------------------------------- P7 ----

SPY = r"""
import json, sys
from sandloom.core.config import module_settings as ms
import dotenv.main as dm
calls = {'dotenv_values': 0}
orig = dm.dotenv_values
def spy(*a, **k):
    calls['dotenv_values'] += 1
    return orig(*a, **k)
dm.dotenv_values = spy
import dotenv; dotenv.dotenv_values = spy
import os
listed = {'listdir': 0, 'scandir': 0}
_ld, _sd = os.listdir, os.scandir
def ld(p='.'):
    if 'secrets' in str(p): listed['listdir'] += 1
    return _ld(p)
def sd(p='.'):
    if 'secrets' in str(p): listed['scandir'] += 1
    return _sd(p)
os.listdir, os.scandir = ld, sd
import sandloom.main as m
from sandloom.core.config.settings import settings
from sandloom_billing.settings import billing_settings
from inventory.settings import InventorySettings
from sandloom.sdk.module import settings_of
inv = settings_of(InventorySettings)
lookups = dict(ms.STATS['secrets_lookups'])
print(json.dumps({
  'reserve_ttl': inv.reserve_ttl_seconds, 'reserve_origin': ms.STATE.records['inventory'].origins['INVENTORY_RESERVE_TTL_SECONDS'],
  'warehouse': inv.warehouse, 'warehouse_origin': ms.STATE.records['inventory'].origins['INVENTORY_WAREHOUSE'],
  'token': inv.api_token.get_secret_value() if inv.api_token else None, 'token_origin': ms.STATE.records['inventory'].origins['INVENTORY_API_TOKEN'],
  'allow_oversell': inv.allow_oversell, 'oversell_origin': ms.STATE.records['inventory'].origins['INVENTORY_ALLOW_OVERSELL'],
  'stripe_webhook': billing_settings().stripe_webhook_secret.get_secret_value() if billing_settings().stripe_webhook_secret else None,
  'log_level': settings.log_level,
  'dotenv_parses': ms.STATS['dotenv_parses'], 'dotenv_values_calls': calls['dotenv_values'],
  'secrets_lookups_max_per_alias': max(lookups.values()) if lookups else 0, 'secrets_lookups': len(lookups),
  'secrets_dir_listed': listed,
}))
"""


def scenario_p7() -> None:
    with tempfile.TemporaryDirectory(prefix="kb49-p7-") as tmp:
        cwd = Path(tmp)
        secrets = cwd / "secrets"
        secrets.mkdir()
        (secrets / "INVENTORY_API_TOKEN").write_text("from-secrets\n")
        (secrets / "INVENTORY_WAREHOUSE").write_text("secrets-wh\n")  # shadowed by .env
        (secrets / "INVENTORY_RESERVE_TTL_SECONDS").write_text("7\n")  # shadowed by env
        (secrets / "STRIPE_WEBHOOK_SECRET").write_text("whsec_from_secrets\n")
        (cwd / ".env").write_text(
            "INVENTORY_WAREHOUSE=dotenv-wh\nINVENTORY_RESERVE_TTL_SECONDS=8\nINVENTORY_ALLOW_OVERSELL=true\nLOG_LEVEL=debug\n"
        )
        values = {
            **DEV,
            "SANDLOOM_MODULES": ["inventory", "sandloom_billing"],
            "INVENTORY_RESERVE_TTL_SECONDS": "9",
            "SECRETS_DIR": str(secrets),
        }
        rc, out, err = run([PY, "-c", SPY], values, cwd=cwd)
    try:
        d = json.loads(last_line(out))
    except ValueError:
        record("P7 sources", False, f"rc={rc} {trim(last_line(err))}")
        return
    print(json.dumps(d, indent=1))
    record("P7 env > .env > secrets (ttl: env 9 over .env 8 over secrets 7)", d["reserve_ttl"] == 9 and d["reserve_origin"] == "env", f"{d['reserve_ttl']} [{d['reserve_origin']}]")
    record("P7 .env over secrets (warehouse)", d["warehouse"] == "dotenv-wh" and d["warehouse_origin"] == "dotenv", f"{d['warehouse']} [{d['warehouse_origin']}]")
    record("P7 secrets supply a value (token)", d["token"] == "from-secrets" and d["token_origin"] == "secrets_dir", f"[{d['token_origin']}]")
    record("P7 .env supplies a module value and a kernel value", d["allow_oversell"] is True and d["log_level"] == "DEBUG", f"oversell={d['allow_oversell']} LOG_LEVEL={d['log_level']}")
    record("P7 billing secret from SECRETS_DIR", d["stripe_webhook"] == "whsec_from_secrets", "")
    record(
        "P7 spy: one .env parse, one secrets lookup per declared alias, no listing",
        d["dotenv_parses"] == 1 and d["dotenv_values_calls"] == 1 and d["secrets_lookups_max_per_alias"] == 1 and not any(d["secrets_dir_listed"].values()),
        f"dotenv parses={d['dotenv_parses']} dotenv_values calls={d['dotenv_values_calls']} lookups={d['secrets_lookups']} aliases, max per alias={d['secrets_lookups_max_per_alias']} listed={d['secrets_dir_listed']}",
    )


# --------------------------------------------------------------------- P8 ----

TIERS = r"""
import json
from sandloom.core.config import tiers
from sandloom.core.config import module_settings as ms
from sandloom_billing.settings import BillingSettings
from sandloom_identity.config import IdentitySettings
print(json.dumps({'kernel_semantic': len(tiers.SEMANTIC), 'billing_semantic': len(ms.declared_semantic(BillingSettings)),
  'identity_semantic': len(ms.declared_semantic(IdentitySettings)),
  'unclassified_kernel': sorted({str(f.alias or n).upper() for n, f in __import__('sandloom.core.config.settings', fromlist=['Settings']).Settings.model_fields.items()} - {s.name for s in tiers.SEMANTIC} - set(tiers.OPS))}))
"""


def scenario_p8() -> None:
    rc, out, err = run([PY, "-c", TIERS], {})
    cand = json.loads(last_line(out))
    base = None
    if BASE_PY:
        rc_b, out_b, _ = run([BASE_PY, "-c", "import json; from sandloom.core.config import tiers; print(json.dumps({'kernel_semantic': len(tiers.SEMANTIC)}))"], {})
        base = json.loads(last_line(out_b))
    total = cand["kernel_semantic"] + cand["billing_semantic"] + cand["identity_semantic"]
    record(
        "P8 standard-install semantic total unchanged",
        base is not None and total == base["kernel_semantic"],
        f"base kernel={base and base['kernel_semantic']}; candidate kernel={cand['kernel_semantic']} + billing={cand['billing_semantic']} + identity={cand['identity_semantic']} = {total}; unclassified kernel names={cand['unclassified_kernel']}",
    )
    values = {**DEV, "SANDLOOM_MODULES": ["inventory"], "INVENTORY_ALLOW_OVERSELL": "true"}
    rc, out, err = run([PY, "-c", "import sandloom.main; print('built')"], values)
    line = [l for l in err.splitlines() if "CONFIG OVERRIDE: INVENTORY_ALLOW_OVERSELL" in l]
    record("P8 fixture semantic override logged at INFO in dev", bool(line) and "[INFO]" in line[0], trim(line[0] if line else last_line(err)))
    values = {**DEV, "SANDLOOM_MODULES": ["inventory"], "INVENTORY_LEGACY_SYNC": "true"}
    rc, out, err = run([PY, "-c", "import sandloom.main; print('built')"], values)
    last = last_line(err)
    record(
        "P8 removed fixture setting refused with today's message shape",
        rc == 1 and "INVENTORY_LEGACY_SYNC was removed in 0.2 and INVENTORY_LEGACY_SYNC=true is no longer supported: stock sync is always event-driven now; drop it from the environment" in last,
        trim(last),
    )
    values = {**DEV, "SANDLOOM_MODULES": ["inventory"], "INVENTORY_LEGACY_SYNC": "false"}
    rc, out, err = run([PY, "-c", "import sandloom.main; print('built')"], values)
    record("P8 removed fixture setting at its inert value warns", rc == 0 and "INVENTORY_LEGACY_SYNC was removed in 0.2" in err, f"rc={rc}")


# --------------------------------------------------------------------- P9 ----

P9_PROFILE = """
x-common-env: &common
  ENVIRONMENT: prod
  EVENT_BUS_BACKEND: streams
  INTERNAL_API_TOKEN: ${INTERNAL_API_TOKEN:?set it}
services:
  shop:
    image: app
    command: ["gunicorn"]
    environment:
      <<: *common
      SANDLOOM_MODULES: '["inventory"]'
      INVENTORY_WAREHOUSE: north
  stock:
    image: app
    command: ["gunicorn"]
    environment:
      <<: *common
      SANDLOOM_MODULES: '["inventory"]'
      INVENTORY_WAREHOUSE: south
"""


def scenario_p9() -> None:
    code = (
        "import json, sys; from pathlib import Path\n"
        "from sandloom.core.testing.deploy_profile import _shared_settings, app_processes, load_profile\n"
        "procs = app_processes(load_profile(Path(sys.argv[1])))\n"
        "out = []\n_shared_settings(procs, out.append)\nprint(json.dumps(out))\n"
    )
    with tempfile.TemporaryDirectory(prefix="kb49-p9-") as tmp:
        path = Path(tmp) / "microservices.yml"
        path.write_text(P9_PROFILE)
        rc, out, err = run([PY, "-c", code, str(path)], {})
        try:
            problems = json.loads(last_line(out))
        except ValueError:
            record("P9 T014 shared inventory value differs", False, f"rc={rc} {trim(last_line(err))}")
        else:
            hit = [p for p in problems if p.startswith("INVENTORY_WAREHOUSE differs")]
            record("P9 T014 shared inventory value differs between services", bool(hit), trim(hit[0] if hit else str(problems)))
            path.write_text(P9_PROFILE.replace("INVENTORY_WAREHOUSE: south", "INVENTORY_WAREHOUSE: north"))
            rc, out, err = run([PY, "-c", code, str(path)], {})
            problems = json.loads(last_line(out))
            record("P9 control: equal shared values pass", not [p for p in problems if "INVENTORY_" in p], trim(str(problems)))
    # static: a name declared by two modules never loaded together; an unprefixed new name
    rc, out, err = run(cli(PY, "check", "settings", "--modules", "inventory,inventory_dup,inventory_unprefixed"), {})
    record("P9 check settings: collision across modules not loaded together", rc == 1 and "SANDLOOM-B036: INVENTORY_RESERVE_TTL_SECONDS is declared by module inventory and module inventory_dup" in out, trim(out.strip().splitlines()[0] if out.strip() else last_line(err)))
    record("P9 check settings: unprefixed new name", rc == 1 and "inventory_unprefixed: new setting RESERVE_LIMIT must start with INVENTORY_UNPREFIXED_" in out, "")
    rc, out, err = run(cli(PY, "check", "settings", "--modules", "billing,identity,tenancy,inventory"), {})
    record("P9 control: bundled + fixture clean (STRIPE_*/TWOFA_* grandfathered by snapshot)", rc == 0, trim(last_line(out)))
    print(out.strip())


# -------------------------------------------------------------------- P11 ----

SOCKET_SPY = r"""
import socket, sys, json, time
connects = []
_orig = socket.socket.connect
def connect(self, addr):
    connects.append(repr(addr)); return _orig(self, addr)
socket.socket.connect = connect
_cc = socket.create_connection
def cc(addr, *a, **k):
    connects.append('create_connection ' + repr(addr)); return _cc(addr, *a, **k)
socket.create_connection = cc
import asyncio
_oc = asyncio.open_connection
async def oc(*a, **k):
    connects.append('asyncio ' + repr(a)); return await _oc(*a, **k)
asyncio.open_connection = oc
lifespans = []
import contextlib
import sandloom.core.app_factory.lifecycle as lc
_ml = lc._make_lifespan
def ml(*a, **k):
    real = _ml(*a, **k)
    @contextlib.asynccontextmanager
    async def wrapped(app):
        lifespans.append('entered')
        async with real(app) as s:
            yield s
    return wrapped
lc._make_lifespan = ml
t0 = time.perf_counter()
sys.argv = ['sandloom', 'config', 'dump'] + sys.argv[1:]
import sandloom
try:
    rc = sandloom.main()
except SystemExit as e:
    rc = e.code
print('SPY ' + json.dumps({'rc': rc, 'connects': connects, 'lifespans': lifespans, 'wall_s': round(time.perf_counter() - t0, 3)}), file=sys.stderr)
"""


def scenario_p11() -> None:
    values = {
        **DEV,
        "SANDLOOM_MODULES": ["inventory", "sandloom_billing", "sandloom_identity", "sandloom_tenancy"],
        "INVENTORY_API_TOKEN": "tok-secret",
        "STRIPE_WEBHOOK_SECRET": "whsec_x",
        "BILLING_PROVIDER": "stripe",
        "STRIPE_SECRET_KEY": "sk_test_x",
    }
    rc, out, err = run([PY, "-c", SOCKET_SPY, "--json"], values)
    spy = json.loads([l for l in err.splitlines() if l.startswith("SPY ")][-1][4:])
    data = json.loads(out)
    rows = {e["name"]: e for e in data}
    record("P11 module values with provenance", rows.get("INVENTORY_API_TOKEN", {}).get("source") == "env module=inventory" and rows.get("BILLING_PROVIDER", {}).get("value") == "stripe", f"INVENTORY_API_TOKEN={rows.get('INVENTORY_API_TOKEN')} BILLING_PROVIDER={rows.get('BILLING_PROVIDER')}")
    record("P11 redaction (declared SecretStr + name rule)", rows["INVENTORY_API_TOKEN"]["value"] == "<redacted>" and rows["STRIPE_SECRET_KEY"]["value"] == "<redacted>" and rows["STRIPE_WEBHOOK_SECRET"]["value"] == "<redacted>", "")
    record("P11 socket spy: no connection during the dump (billing builds its Stripe client in setup)", spy["connects"] == [] and spy["rc"] in (0, None), f"connects={spy['connects']} rc={spy['rc']}")
    record("P11 no lifespan entered", spy["lifespans"] == [], f"lifespans={spy['lifespans']}")
    # invalid module value: diagnosed, exit 0
    bad = {**values, "INVENTORY_RESERVE_TTL_SECONDS": "0"}
    rc, out, err = run([PY, "-c", SOCKET_SPY, "--json"], bad)
    spy = json.loads([l for l in err.splitlines() if l.startswith("SPY ")][-1][4:])
    try:
        json.loads(out)  # still a list
        probs = [l[len("PROBLEM: "):] for l in err.splitlines() if l.startswith("PROBLEM: ")]
    except ValueError:
        probs = []
    record("P11 invalid module value diagnosed without exiting 1", spy["rc"] in (0, None) and any("INVENTORY_RESERVE_TTL_SECONDS" in p for p in probs), f"rc={spy['rc']} problems={[trim(p, 140) for p in probs]}")
    # --kernel-only against today's dump (base), names that stayed in the kernel
    if BASE_PY:
        moved = {"BILLING_PROVIDER", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "BILLING_DENY_ON_PAST_DUE", "BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS", "BILLING_INTENT_RECONCILE_AFTER_MINUTES", "BILLING_INTENT_RECONCILE_MAX_ATTEMPTS", "BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES", "TWOFA_ENCRYPTION_KEY"}
        kv = {**DEV, "SANDLOOM_MODULES": ["sandloom_billing"], "LOG_LEVEL": "debug", "INVENTORY_API_TOKEN": "x"}
        rc_c, out_c, _ = run(cli(PY, "config", "dump", "--kernel-only"), kv)
        rc_b, out_b, _ = run(cli(BASE_PY, "config", "dump"), kv)
        base_lines = [l for l in out_b.splitlines() if l.split(" ", 1)[0] not in moved]
        cand_lines = out_c.splitlines()
        width_note = ""
        if base_lines != cand_lines:
            # the text renderer pads names to the longest one; compare unpadded too
            norm = lambda ls: [" ".join(l.split()) for l in ls]  # noqa: E731
            width_note = " (equal after whitespace normalisation)" if norm(base_lines) == norm(cand_lines) else ""
        record(
            "P11 --kernel-only = today's dump minus the moved names",
            base_lines == cand_lines,
            f"base lines={len(out_b.splitlines())} (moved {len(out_b.splitlines()) - len(base_lines)}), candidate={len(cand_lines)}{width_note}",
        )
        rc_c, out_cj, _ = run(cli(PY, "config", "dump", "--kernel-only", "--json"), kv)
        rc_b, out_bj, _ = run(cli(BASE_PY, "config", "dump", "--json"), kv)
        bj = [e for e in json.loads(out_bj) if e["name"] not in moved]
        record("P11 --kernel-only --json byte-identical to today's minus moved entries", json.dumps(bj, indent=2) == out_cj.strip(), f"{len(bj)} entries")
        # wall time: today's dump vs the candidate dump (composition built), 10 alternating runs
        times: dict[str, list[float]] = {"base": [], "kernel-only": [], "full": []}
        tv = {**DEV, "SANDLOOM_MODULES": ["sandloom_billing", "sandloom_identity", "sandloom_tenancy", "inventory"]}
        for _ in range(10):
            for label, argv in (("base", cli(BASE_PY, "config", "dump")), ("kernel-only", cli(PY, "config", "dump", "--kernel-only")), ("full", cli(PY, "config", "dump"))):
                t0 = time.perf_counter()
                run(argv, tv)
                times[label].append(time.perf_counter() - t0)
        med = {k: round(sorted(v)[len(v) // 2], 3) for k, v in times.items()}
        record("P11 wall time (process, median of 10)", True, f"today={med['base']}s candidate --kernel-only={med['kernel-only']}s candidate full={med['full']}s (+{round(med['full'] - med['base'], 3)}s)")


# -------------------------------------------------------------------- P14 ----


def scenario_p14() -> None:
    code = (
        "import json, sys\nfrom sandloom.core.testing import deploy_profile as d\n"
        "from pathlib import Path\n"
        "text = Path(sys.argv[1]).read_text()\n"
        "shell = d.dev_shell(text)\n"
        "print(json.dumps({k: (len(v) if 'KEY' in k or 'SECRET' in k else v) for k, v in shell.items() if k in ('TWOFA_ENCRYPTION_KEY','BILLING_PROVIDER','SANDLOOM_ACCEPTED_RISKS')}))\n"
    )
    root = Path(PY).absolute().parents[2]
    profile = root / "deploy" / "compose" / "microservices.yml"
    rc, out, err = run([PY, "-c", code, str(profile)], {})
    try:
        shell = json.loads(last_line(out))
    except ValueError:
        record("P14 dev env from declarations", False, f"rc={rc} {trim(last_line(err))}")
    else:
        record(
            "P14 dev env values come from declarations",
            shell.get("BILLING_PROVIDER") == "fake" and shell.get("TWOFA_ENCRYPTION_KEY") == 64 and "billing.fake_psp_in_prod" in str(shell.get("SANDLOOM_ACCEPTED_RISKS")),
            json.dumps(shell),
        )
    text = (root / "src/sandloom/core/testing/deploy_profile.py").read_text()
    left = [n for n in ("BILLING_DENY_ON_PAST_DUE", "TWOFA_ENCRYPTION_KEY", "BILLING_PROVIDER") if n in text]
    record("P14 deploy_profile.py names no module setting", not left, f"left={left}")
    # boot each service of the shipped profile with the dev env (registration
    # validates from it; no production check fires): T014's boot_problems
    code = (
        "import json, sys\nfrom pathlib import Path\nfrom sandloom.core.testing import deploy_profile as d\n"
        "deploy = Path(sys.argv[1])\ntext = (deploy / d.PROFILE).read_text()\nshell = d.dev_shell(text)\n"
        "print(json.dumps(d.boot_problems(deploy, shell, python=sys.executable)))\n"
    )
    rc, out, err = run([PY, "-c", code, str(root / "deploy")], {}, timeout=600)
    try:
        boot = json.loads(last_line(out))
    except ValueError:
        record("P14 shipped profile: every service boots with the dev env", False, f"rc={rc} {trim(last_line(err))}")
    else:
        bad = {k: v for k, v in boot.items() if v}
        record("P14 shipped profile: every service boots with the dev env (no prod check fires)", not bad, f"services={sorted(boot)} problems={trim(json.dumps(bad), 400)}")
    code = (
        "import json, sys\nfrom pathlib import Path\nfrom sandloom.core.testing import deploy_profile as d\n"
        "r = d.profile_problems(Path(sys.argv[1]))\nprint(json.dumps(r.problems))\n"
    )
    rc, out, err = run([PY, "-c", code, str(root / "deploy")], DEV, timeout=600)
    record("P14 T014 profile_problems on the shipped profile", rc == 0 and last_line(out) == "[]", trim(last_line(out) or last_line(err)))


# -------------------------------------------------------------------- P15 ----


def scenario_p15() -> None:
    root = Path(PY).absolute().parents[2]
    audit = root / "tools" / "checks" / "import_audit.py"
    for label, mods, want_fail in (
        ("fixture + ported modules", "inventory,sandloom_billing,sandloom_identity", False),
        ("module-level settings_of", "inventory_bad_import", True),
    ):
        # -I ignores PYTHONPATH: put the fixture on sys.path, then run the audit
        boot = (
            f"import sys, runpy; sys.path.insert(0, {str(FIXTURE)!r}); "
            f"sys.argv = [{str(audit)!r}, *{mods.split(',')!r}]; "
            f"runpy.run_path({str(audit)!r}, run_name='__main__')"
        )
        rc, out, err = run([PY, "-I", "-c", boot], {})
        text = (out + err).strip()
        ok = (rc != 0 and "SettingsNotBoundError" in text) if want_fail else (rc == 0)
        record(f"P15 import audit (python -I): {label}", ok, f"rc={rc} {trim(last_line(text), 260)}")


CONSOLE = r"""
import json
import sandloom.main as m
from sandloom.core.admin.api import platform as p
rows = [r.key for r in p._module_console_settings(m.app.state.core)]
kernel = [k for k in p._SAFE_SETTINGS if hasattr(m.app.state.core.settings, k)]
print(json.dumps({"module_rows": rows, "billing_provider_in_kernel_list": "billing_provider" in kernel}))
"""


def scenario_p10_console() -> None:
    for label, modules, want in (
        ("mono (identity, tenancy, billing)", ["sandloom_identity", "sandloom_tenancy", "sandloom_billing"], ["billing_provider"]),
        ("without billing", ["sandloom_identity", "sandloom_tenancy"], []),
    ):
        rc, out, err = run([PY, "-c", CONSOLE], {**DEV, "SANDLOOM_MODULES": modules})
        try:
            d = json.loads(last_line(out))
        except ValueError:
            record(f"P10 console list {label}", False, f"rc={rc} {trim(last_line(err))}")
            continue
        record(f"P10 console list {label}", d["module_rows"] == want and not d["billing_provider_in_kernel_list"], json.dumps(d))


SCENARIOS = {
    "P2": scenario_p2,
    "P3": scenario_p3,
    "P4": scenario_p4,
    "P5": scenario_p5,
    "P6": scenario_p6,
    "P7": scenario_p7,
    "P8": scenario_p8,
    "P9": scenario_p9,
    "P10": scenario_p10_console,
    "P11": scenario_p11,
    "P14": scenario_p14,
    "P15": scenario_p15,
}


def main() -> int:
    global PY, BASE_PY
    parser = argparse.ArgumentParser()
    parser.add_argument("--py", required=True)
    parser.add_argument("--base-py")
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    args = parser.parse_args()
    PY, BASE_PY = args.py, args.base_py
    for name in args.scenarios:
        print(f"===== {name}", flush=True)
        SCENARIOS[name]()
    failed = [r for r in RESULTS if not r[1]]
    print(f"===== {len(RESULTS) - len(failed)}/{len(RESULTS)} rows pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
