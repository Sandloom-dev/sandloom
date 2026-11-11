#!/bin/sh
# Instructions per request of each authz stand case, monolith, under cachegrind
# (the method of benchmarks/README.md and benchmarks/ladder/cg/cgdriver.py):
#   Ir/op = (Ir(K requests) - Ir(0 requests)) / K
# Both runs do the same imports, app start, 20 warm-up requests and shutdown,
# so all of that cancels out. Uses the ladder image (benchmarks/ladder/cg).
#
#   DATABASE_URL=... CACHE_URL=... ./cg_authz.sh control role.admin ...
# localhost / 127.0.0.1 in the URLs become host.docker.internal.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
K=${CG_K:-200}
IMAGE=${CG_IMAGE:-sandloom-ladder-cg:3.13}
docker build -q -t "$IMAGE" "$ROOT/benchmarks/ladder/cg" >/dev/null
META=$(mktemp -d)
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
    IR=$(docker run --rm \
      -e DATABASE_URL="$(fix "$DATABASE_URL")" \
      -e CACHE_URL="$(fix "$CACHE_URL")" \
      -e PYTHONHASHSEED=0 \
      -e PYTHONPATH=/w/src:/w/benchmarks:/w/meta \
      -v "$ROOT/src:/w/src:ro" \
      -v "$ROOT/benchmarks:/w/benchmarks:ro" \
      -v "$ROOT/migrations:/w/migrations:ro" \
      -v "$HERE:/w/design/kernel-boundary/_evidence/perf-dx:ro" \
      -v "$META:/w/meta:ro" \
      -w /tmp "$IMAGE" \
      valgrind --tool=cachegrind --cache-sim=no --cachegrind-out-file=/dev/null \
      python /w/design/kernel-boundary/_evidence/perf-dx/authz_stand.py cg --case "$CASE" --n "$N" \
      2>&1 | sed -n 's/.*I *refs: *\([0-9,]*\).*/\1/p' | tr -d ,)
    eval "IR_$N=\$IR"
  done
  echo "$CASE $(( (IR_$K - IR_0) / K )) instr/op (K=$K, Ir0=$IR_0)"
done
