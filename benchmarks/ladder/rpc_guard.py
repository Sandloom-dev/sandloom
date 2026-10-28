"""Run one measurement command; tag its JSON line with foreign load.

    RPC_GUARD_DB=<your bench db> RPC_GUARD_CACHE=:6390/ \
        python ladder/rpc_guard.py <out.jsonl> <extra-json> -- <cmd...>

Waits for a quiet window (no foreign pytest / soak for QUIET s), records
foreign processes + load average before and after, and appends the command's
last JSON stdout line merged with that metadata.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
)
import ci_lock  # noqa: E402  (the lock local CI holds; tools/ci/ci_lock.py)

QUIET = float(os.environ.get("GUARD_QUIET", "20"))
PAT = ("pytest", "soak.py", "soak_app", "run_int", "server_bench")


def foreign() -> list[str]:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                         text=True).stdout
    me = str(os.getpid())
    hits = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid == me or "guard.py" in cmd or "grep" in cmd or cmd.startswith(("/bin/zsh", "zsh ", "/bin/sh", "bash ")):
            # shell wrappers: the real test / soak process is listed on its own
            continue
        if any(p in cmd for p in PAT):
            hits.append(f"{pid} {cmd[:120]}")
    if ci_lock.is_held():  # docker CI runner (VM, invisible) or a CPU benchmark
        hits.append(f"lock {ci_lock.LOCK_FILE}")
    return hits


def main() -> None:
    out, extra = sys.argv[1], json.loads(sys.argv[2])
    want_db = os.environ.get("RPC_GUARD_DB", "sandloom_bench_rpcdecomp")
    want_cache = os.environ.get("RPC_GUARD_CACHE", ":6390/")
    if want_db not in os.environ.get("DATABASE_URL", "") or want_cache not in (
        os.environ.get("CACHE_URL", "")
    ):
        sys.exit("guard: wrong DATABASE_URL / CACHE_URL in env")
    cmd = sys.argv[sys.argv.index("--") + 1 :]
    quiet_since = time.time() if not foreign() else None
    while quiet_since is None or time.time() - quiet_since < QUIET:
        time.sleep(2)
        if foreign():
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
    before, la0 = foreign(), os.getloadavg()
    r = subprocess.run(cmd, capture_output=True, text=True)
    after, la1 = foreign(), os.getloadavg()
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode or not lines:
        print(r.stderr[-2000:], file=sys.stderr)
        sys.exit(1)
    rec = json.loads(lines[-1])
    rec.update(extra)
    rec.update(foreign_before=before, foreign_after=after,
               loadavg_before=la0[0], loadavg_after=la1[0],
               interfered=bool(before or after), ts=time.strftime("%H:%M:%S"))
    with open(out, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps({k: rec.get(k) for k in ("rung", "tenant", "cpu_us",
          "client_cpu_us", "server_cpu_us", "interfered")}), flush=True)


if __name__ == "__main__":
    main()
