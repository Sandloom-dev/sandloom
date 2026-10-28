"""argon2 profile (strong vs owasp_min) and the login-free topology premium;
the RPC provider anomaly at a fixed rate. Interleaved rounds.

    INT_SP=<stand root> python ladder/run_owasp.py login|nologin|provider \\
        --rounds 3 --out FILE [--start K]

Stand layout as ``run_int.py`` (whose helpers it reuses: dataset reset +
row-count check before every server start, Redis flush, quiet wait, invalid
runs kept and flagged): ``$INT_SP/b/main/`` = a ``git archive`` export with
its own ``benchmarks/.venv``; ``$INT_SP/int/main.env`` (``mix env-full`` of
the monolith database ``$OWASP_MONO_DB``); ``$INT_SP/int/topo_{mono,split}.json``.

**Hashes are re-made before EVERY run** with the hasher of the profile under
test (``sandloom.core.auth.hashing.hash_password`` of the measured build, run with
that build's interpreter and ``PASSWORD_HASH_PROFILE``) for ALL users of the
run's identity database; the run records the stored ``$argon2id$…$``
parameter prefix, the user count, and whether every stored hash is unchanged
AFTER the run (a rehash-on-login writes a new salt, so "unchanged" proves no
login of the run re-hashed). A wrong prefix or a changed hash makes the run
invalid.

Sections:

* ``login`` — the monolith, profiles mixed / read-heavy / write-heavy,
  ``PASSWORD_HASH_PROFILE`` strong vs owasp_min, closed (conc 64) + open
  (the profile's fixed rate). read-heavy has no login row: the control.
* ``nologin`` — mixed-nologin / read-heavy / write-heavy-nologin, monolith vs
  split (``RPC_TRANSPORT=aiohttp``), closed + open.
* ``provider`` — ``rpc_provider_probe.py`` (split, the RPC-crossing singles
  at a fixed rate): httpx vs aiohttp after the usual conc-64 warm-up, plus
  aiohttp warmed at conc 4 (few pooled connections), no counters / profiler.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys

import asyncpg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rpc_guard  # noqa: E402
import run_int as ri  # noqa: E402


def _foreign_load() -> int:
    """Foreign pytest / soak / bench processes + the local CI runner's lock."""
    return len(rpc_guard.foreign())


ri._foreign_pytest = _foreign_load

MONO_DB = os.environ.get("OWASP_MONO_DB", "bench_oa_mixfull")
SPLIT_PREFIX = os.environ.get("SPLIT_DB_PREFIX", "bench_oa_split")
MONO_REDIS = tuple(int(x) for x in os.environ.get("OWASP_MONO_REDIS", "14,15").split(","))
SPLIT_REDIS = tuple(int(x) for x in os.environ.get("OWASP_SPLIT_REDIS", "9,13").split(","))
SPLIT_DBS = [f"{SPLIT_PREFIX}_{s}" for s in ri.SPLIT]
BUILD = "main"
PY_BUILD = f"{ri.B}/{BUILD}/benchmarks/.venv/bin/python"
HASH_PROFILES = ("strong", "owasp_min")
EXPECT_PREFIX = {
    "strong": "$argon2id$v=19$m=65536,t=3,p=4$",
    "owasp_min": "$argon2id$v=19$m=19456,t=2,p=1$",
}

_HASH_SNIPPET = """
import sys
from sandloom.core.auth.hashing import hash_password, password_needs_rehash
h = hash_password(sys.argv[1])
assert not password_needs_rehash(h), "fresh hash needs rehash?"
print(h)
"""


def _env_file(path: str) -> dict[str, str]:
    import shlex

    out = {}
    for line in open(path):
        if line.startswith("export "):
            k, v = line[7:].strip().split("=", 1)
            out[k] = shlex.split(v)[0] if v else ""
    return out


def _identity_db(topo: str) -> str:
    return MONO_DB if topo == "mono" else f"{SPLIT_PREFIX}_identity"


def _fingerprint(hashes: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(hashes)).encode()).hexdigest()[:16]


async def _read_hashes(db: str) -> list[str]:
    c = await asyncpg.connect(ri.PG + db)
    try:
        return [r[0] for r in await c.fetch("SELECT hashed_password FROM users")]
    finally:
        await c.close()


async def _store_hash(db: str, h: str) -> int:
    c = await asyncpg.connect(ri.PG + db)
    try:
        res = await c.execute("UPDATE users SET hashed_password = $1", h)
        return int(res.split()[-1])
    finally:
        await c.close()


def rehash(topo: str, profile: str) -> dict:
    """Re-hash ALL users of the run's identity DB with ``profile``'s hasher."""
    from benchsuite import mix

    env = {
        **os.environ,
        **_env_file(f"{ri.INT}/{BUILD}.env"),
        "PASSWORD_HASH_PROFILE": profile,
        "LOG_LEVEL": "ERROR",
    }
    h = subprocess.run(
        [PY_BUILD, "-c", _HASH_SNIPPET, mix.PASSWORD],
        env=env, capture_output=True, text=True, check=True,
        cwd=f"{ri.B}/{BUILD}/benchmarks",
    ).stdout.strip().splitlines()[-1]
    db = _identity_db(topo)
    updated = asyncio.run(_store_hash(db, h))
    hashes = asyncio.run(_read_hashes(db))
    prefixes = sorted({x[: x.rfind("$", 0, x.rfind("$")) + 1] for x in hashes})
    return {
        "hash_profile": profile,
        "users": len(hashes),
        "users_rehashed": updated,
        "hash_prefixes": prefixes,
        "hash_prefix_ok": prefixes == [EXPECT_PREFIX[profile]],
        "hash_fp_before": _fingerprint(hashes),
    }


def _prepare(topo: str, out: str, profile: str) -> dict:
    if topo == "mono":
        prep = ri._prepare([MONO_DB], MONO_REDIS, out)
    else:
        prep = ri._prepare(SPLIT_DBS, SPLIT_REDIS, out)
    prep["hash"] = rehash(topo, profile)
    return prep


def _run(cmd: list[str], meta: dict, out: str, prep: dict, topo: str) -> None:
    """``ri._run`` + the hash checks (a run that re-hashed is invalid)."""
    h = prep.pop("hash")
    # ri._run appends the record; re-read it to add the hash verdict
    ri._run(cmd, {**meta, **h}, out, prep)
    hashes = asyncio.run(_read_hashes(_identity_db(topo)))
    after = _fingerprint(hashes)
    with open(out) as fh:
        lines = fh.readlines()
    rec = json.loads(lines[-1])
    rec["hash_fp_after"] = after
    rec["rehashed_during_run"] = after != h["hash_fp_before"]
    reasons = list(rec.get("invalid_reasons", []))
    if not h["hash_prefix_ok"]:
        reasons.append(f"hash prefix {h['hash_prefixes']}")
    if rec["rehashed_during_run"]:
        reasons.append("a login re-hashed a password during the run")
    if reasons:
        rec["invalid"], rec["invalid_reasons"] = True, reasons
    lines[-1] = json.dumps(rec) + "\n"
    with open(out, "w") as fh:
        fh.writelines(lines)


def _cmd(topo: str, profile: str, mode: str, label: str, env: list[str]) -> list[str]:
    cmd = [
        ri.PY, "ladder/split_bench.py",
        "--topology", f"{ri.INT}/topo_{topo}.json",
        "--mix", f"{ri.PROF}/{profile}.json",
        "--env-file", f"{ri.INT}/{BUILD}.env",
        "--env", "PLANNER_OPTIMIZED_EXECUTOR=true",
        "--header", "Authorization: Bearer " + ri._token(),
        "--python", PY_BUILD,
        "--cwd", f"{ri.B}/{BUILD}/benchmarks",
        "--label", label,
    ]
    for e in env:
        cmd += ["--env", e]
    if mode == "closed":
        cmd += ["--conc", "64", "--n", str(8000 // 64 * 64), "--reps", "3"]
    else:
        cmd += ["--rate", str(ri.RATES[profile]), "--seconds", "30"]
    return cmd


def _alternate(items: list, rnd: int) -> list:
    """Two cells: A,B on even rounds, B,A on odd ones (``ri._order`` rotates
    AND reverses, which for two cells gives the same order every round)."""
    return list(items) if rnd % 2 == 0 else list(items)[::-1]


def _rotate(items: list, rnd: int) -> list:
    k = rnd % len(items)
    return items[k:] + items[:k]


def login(rounds: list[int], out: str) -> None:
    profiles = ["mixed", "read-heavy", "write-heavy"]
    for rnd in rounds:
        for profile in _rotate(profiles, rnd):
            for slot, hp in enumerate(_alternate(list(HASH_PROFILES), rnd)):
                for mode in ("closed", "open"):
                    prep = _prepare("mono", out, hp)
                    _run(
                        _cmd("mono", profile, mode, f"{profile}|{hp}|{mode}",
                             [f"PASSWORD_HASH_PROFILE={hp}"]),
                        {"section": "owasp_login", "round": rnd, "slot": slot,
                         "topology": "mono", "profile": profile, "mode": mode},
                        out, prep, "mono",
                    )


def nologin(rounds: list[int], out: str) -> None:
    profiles = ["mixed-nologin", "read-heavy", "write-heavy-nologin"]
    hp = "owasp_min"  # no login row: the hash profile is not exercised
    for rnd in rounds:
        for profile in _rotate(profiles, rnd):
            for slot, topo in enumerate(_alternate(["mono", "split"], rnd)):
                for mode in ("closed", "open"):
                    prep = _prepare(topo, out, hp)
                    _run(
                        _cmd(topo, profile, mode, f"{profile}|{topo}|{mode}",
                             [f"PASSWORD_HASH_PROFILE={hp}", "RPC_TRANSPORT=aiohttp"]),
                        {"section": "owasp_nologin", "round": rnd, "slot": slot,
                         "topology": topo, "profile": profile, "mode": mode,
                         "transport": "aiohttp"},
                        out, prep, topo,
                    )


PROVIDER_CELLS = [("httpx", 64), ("aiohttp", 64), ("aiohttp", 4)]


def provider(rounds: list[int], out: str, rate: int) -> None:
    for rnd in rounds:
        for single in ("tickets", "subscription"):
            for slot, (tr, wc) in enumerate(ri._order(PROVIDER_CELLS, rnd)):
                ri._prepare(SPLIT_DBS, SPLIT_REDIS, out)
                waited = ri._wait_quiet()
                before = rpc_guard.foreign()
                cmd = [
                    ri.PY, "ladder/rpc_provider_probe.py",
                    "--bench", f"{ri.B}/{BUILD}/benchmarks",
                    "--env-file", f"{ri.INT}/{BUILD}.env",
                    "--topology", f"{ri.INT}/topo_split.json",
                    "--mix", f"{ri.PROF}/single_{single}.json",
                    "--db-prefix", SPLIT_PREFIX,
                    "--transport", tr, "--warmup-conc", str(wc),
                    "--rate", str(rate), "--seconds", "30", "--out", out,
                    "--meta", json.dumps({
                        "section": "provider", "round": rnd, "slot": slot,
                        "single": single, "waited_for_quiet_s": waited,
                        "foreign_before": before,
                    }),
                ]
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   cwd=os.path.dirname(HERE))
                after = rpc_guard.foreign()
                with open(out) as fh:
                    lines = fh.readlines()
                rec = json.loads(lines[-1])
                rec["foreign_after"] = after
                rec["load1_after"] = round(os.getloadavg()[0], 2)
                if p.returncode or after or before:
                    rec["invalid"] = True
                    rec["invalid_reasons"] = [f"exit {p.returncode}"] * bool(
                        p.returncode) + ["foreign load"] * bool(after or before)
                    rec["stderr"] = p.stderr[-2000:]
                lines[-1] = json.dumps(rec) + "\n"
                with open(out, "w") as fh:
                    fh.writelines(lines)
                t = rec["per_op"]["tenancy"]
                print(f"[provider r{rnd} {single} {tr} w{wc}] tenancy "
                      f"{t['user'] + t['sys']:.0f} us vcs {t['vcs']:.2f} "
                      f"conns {rec['conns_after'].get('tenancy')} pg {rec['pg_cpu_us']:.0f}"
                      f" invalid={rec.get('invalid', False)}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("section", choices=["login", "nologin", "provider", "rehash"])
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--out", default="")
    p.add_argument("--rate", type=int, default=250)
    p.add_argument("--topology", default="mono")
    p.add_argument("--profile", default="owasp_min")
    a = p.parse_args()
    ri.write_profiles()
    rr = list(range(a.start, a.start + a.rounds))
    if a.section == "rehash":  # verification only
        print(json.dumps(rehash(a.topology, a.profile)))
    elif a.section == "provider":
        provider(rr, a.out, a.rate)
    else:
        {"login": login, "nologin": nologin}[a.section](rr, a.out)
