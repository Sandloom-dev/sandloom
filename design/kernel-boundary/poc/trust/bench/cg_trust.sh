#!/bin/sh
# kb-poc-trust T-15: instructions per request / per mint under cachegrind,
# the method of benchmarks/README.md (Ir/op = (Ir(K ops) - Ir(0 ops)) / K;
# both runs do the same imports, app start, 20 warm-ups and shutdown).
#
#   KERNEL=<worktree root> TAG=base DATABASE_URL=... CACHE_URL=... \
#     ./cg_trust.sh control role_and_perm tenant mint
# localhost / 127.0.0.1 become host.docker.internal. Uses the ladder image.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
K=${CG_K:-200}
IMAGE=${CG_IMAGE:-sandloom-ladder-cg:3.13}
META=$(mktemp -d)
trap 'rm -rf "$META"' EXIT
python3 - "$KERNEL" "$META" <<'PY'
import pathlib, sys, tomllib

root, meta = map(pathlib.Path, sys.argv[1:])
groups: dict[str, dict[str, str]] = {}
for pyproject in sorted(root.glob("packages/*/pyproject.toml")):
    eps = tomllib.loads(pyproject.read_text()).get("project", {}).get("entry-points", {})
    for group, entries in eps.items():
        groups.setdefault(group, {}).update(entries)
dist = meta / "sandloom_trust_cg-0.0.0.dist-info"
dist.mkdir()
(dist / "METADATA").write_text("Metadata-Version: 2.1\nName: sandloom-trust-cg\nVersion: 0.0.0\n")
(dist / "entry_points.txt").write_text(
    "".join(
        f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items())
        for group, entries in groups.items()
    )
)
PY
fix() { echo "$1" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/'; }
for CASE in "$@"; do
  KK=$K
  [ "$CASE" = mint ] && KK=${CG_K_MINT:-100}
  for N in 0 "$KK"; do
    IR=$(docker run --rm \
      -e DATABASE_URL="$(fix "$DATABASE_URL")" \
      -e CACHE_URL="$(fix "$CACHE_URL")" \
      -e BROKER_URL="$(fix "${BROKER_URL:-$CACHE_URL}")" \
      -e PYTHONHASHSEED=0 \
      -e PYTHONPATH=/w/src:/w/meta:/w/bench/stand \
      -v "$KERNEL/src:/w/src:ro" \
      -v "$HERE:/w/bench:ro" \
      -v "$META:/w/meta:ro" \
      -w /tmp "$IMAGE" \
      valgrind --tool=cachegrind --cache-sim=no --cachegrind-out-file=/dev/null \
      python /w/bench/trust_stand.py cg --case "$CASE" --n "$N" \
      2>&1 | sed -n 's/.*I *refs: *\([0-9,]*\).*/\1/p' | tr -d ,)
    eval "IR_$N=\$IR"
  done
  eval "A=\$IR_$KK"
  echo "$TAG $CASE $(( (A - IR_0) / KK )) Ir/op (K=$KK, Ir0=$IR_0, IrK=$A)"
done
