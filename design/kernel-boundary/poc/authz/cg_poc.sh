#!/bin/sh
# Instructions per request (cachegrind ladder, K requests) of stand cases, base
# vs candidate, monolith; the method of _evidence/perf-dx/cg_authz.sh:
#   Ir/op = (Ir(K requests) - Ir(0 requests)) / K
#
#   SIDE=base|wt VARIANT=authzstand|authzpa2 [KB_POC_K6=1 ...] ./cg_poc.sh case...
# SIDE is the worktree (under $S) whose src/ and benchmarks/ are mounted.
# localhost / 127.0.0.1 in the URLs become host.docker.internal. ``python -B``:
# no bytecode is written into the mounted (read-only) tree, so both sides
# compile at boot alike (a 3.13 run elsewhere had left .pyc files in one side).
# Each run's output is kept in $CG_LOGS; a run that raised prints ERROR.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$S/$SIDE
K=${CG_K:-200}
IMAGE=${CG_IMAGE:-sandloom-ladder-cg:3.13}
META=$(mktemp -d "$S/cgmeta.XXXXXX")
LOGS=${CG_LOGS:-$S/cglogs}
mkdir -p "$LOGS"
trap 'rm -rf "$META"' EXIT
python3 - "$ROOT" "$META" <<'PY'
import pathlib, sys, tomllib

root, meta = map(pathlib.Path, sys.argv[1:])
groups: dict[str, dict[str, str]] = {}
for pyproject in sorted(root.glob("packages/*/pyproject.toml")):
    eps = tomllib.loads(pyproject.read_text()).get("project", {}).get("entry-points", {})
    for group, entries in eps.items():
        groups.setdefault(group, {}).update(entries)
dist = meta / "sandloom_authz_cg-0.0.0.dist-info"
dist.mkdir()
(dist / "METADATA").write_text("Metadata-Version: 2.1\nName: sandloom-authz-cg\nVersion: 0.0.0\n")
(dist / "entry_points.txt").write_text(
    "".join(
        f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items())
        for group, entries in groups.items()
    )
)
PY
fix() { echo "$1" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/'; }
for CASE in "$@"; do
  for N in 0 "$K"; do
    LOG="$LOGS/$SIDE-$VARIANT-k6${KB_POC_K6:-0}-w${KB_POC_WITNESS:-0}-$CASE-$N.log"
    docker run --rm \
      -e DATABASE_URL="$(fix "$DATABASE_URL")" \
      -e CACHE_URL="$(fix "$CACHE_URL")" \
      -e PYTHONHASHSEED=0 \
      -e PYTHONPATH=/w/src:/w/benchmarks:/w/meta \
      -e KB_REPO=/w -e KB_VARIANT="$VARIANT" \
      -e KB_POC_K5="${KB_POC_K5:-}" -e KB_POC_K6="${KB_POC_K6:-}" -e KB_POC_WITNESS="${KB_POC_WITNESS:-}" \
      -v "$ROOT/src:/w/src:ro" \
      -v "$ROOT/benchmarks:/w/benchmarks:ro" \
      -v "$HERE:/w/poc:ro" \
      -v "$META:/w/meta:ro" \
      -w /tmp "$IMAGE" \
      valgrind --tool=cachegrind --cache-sim=no --cachegrind-out-file=/dev/null \
      python -B /w/poc/stand.py cg --case "$CASE" --n "$N" >"$LOG" 2>&1 || true
    IR=$(sed -n 's/.*I *refs: *\([0-9,]*\).*/\1/p' "$LOG" | tr -d ,)
    if grep -q Traceback "$LOG"; then echo "ERROR $SIDE $CASE N=$N: see $LOG" >&2; IR=0; fi
    eval "IR_$N=\$IR"
  done
  echo "$SIDE $VARIANT k6=${KB_POC_K6:-0} witness=${KB_POC_WITNESS:-0} $CASE $(( (IR_$K - IR_0) / K )) instr/op (K=$K, Ir0=$IR_0)"
done
