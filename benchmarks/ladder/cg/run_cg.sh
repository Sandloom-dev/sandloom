#!/bin/sh
# Build the image and run the cachegrind ladder (host with docker).
#   DATABASE_URL=... CACHE_URL=... BROKER_URL=... ladder/cg/run_cg.sh L0 L2 L3 ...
# localhost / 127.0.0.1 in the URLs become host.docker.internal in the container.
#
# The checkout is mounted, not installed: the core migration chain comes from
# ``migrations/`` and the entry points (``sandloom.modules``, the SQL dialect
# renderers) from a dist-info written here from ``packages/*/pyproject.toml``.
# Without them a module's events have no producer (B015) and a build with
# renderers finds none (B024).
#
# The stand boots the STANDARD composition root (``sandloom_standard.main``, the
# ``sandloom-standard`` package mounted from ``packages/sandloom-standard/src``):
# the lean ``sandloom.main`` assigns no claim authorities, so a composition that
# loads the token issuer refuses with SANDLOOM-B035. The rungs import
# ``sandloom_standard.main`` directly; ``SANDLOOM_COMPOSITION_ROOT`` names the same
# root for the kernel's own entry points. A prod-environment stand that loads
# identity / tenancy needs an email notifier (SANDLOOM-B037): the stand sets the
# ``log_notifier_in_prod`` accepted risk (the ``log`` driver delivers no mail; the
# stand sends none). ``SANDLOOM_SERVICE_NAME`` names the process for the rungs that
# read it (R0.rpc); override it from the caller's environment.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
docker build -q -t sandloom-ladder-cg:3.13 "$HERE" >/dev/null
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
dist = meta / "sandloom_ladder_cg-0.0.0.dist-info"
dist.mkdir()
(dist / "METADATA").write_text(
    "Metadata-Version: 2.1\nName: sandloom-ladder-cg\nVersion: 0.0.0\n"
)
(dist / "entry_points.txt").write_text(
    "".join(
        f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items())
        for group, entries in groups.items()
    )
)
PY
fix() { echo "$1" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/'; }
docker run --rm \
  -e DATABASE_URL="$(fix "$DATABASE_URL")" \
  -e CACHE_URL="$(fix "$CACHE_URL")" \
  -e BROKER_URL="$(fix "$BROKER_URL")" \
  -e CG_K="${CG_K:-200}" \
  -e SANDLOOM_COMPOSITION_ROOT="${SANDLOOM_COMPOSITION_ROOT:-sandloom_standard.main}" \
  -e SANDLOOM_SERVICE_NAME="${SANDLOOM_SERVICE_NAME:-ladder}" \
  -e SANDLOOM_ACCEPTED_RISKS="${SANDLOOM_ACCEPTED_RISKS:-local_storage_in_prod,shared_hs_secret_split,relay_disabled,log_notifier_in_prod}" \
  -e DB_RLS_MERGED_BEGIN="${DB_RLS_MERGED_BEGIN:-true}" \
  -e LADDER_S5_VARIANT="${LADDER_S5_VARIANT:-uow}" \
  -e LADDER_CG_GC="${LADDER_CG_GC:-}" \
  -e PLANNER_OPTIMIZED_EXECUTOR="${PLANNER_OPTIMIZED_EXECUTOR:-false}" \
  -e S5_APP_ROLE="${S5_APP_ROLE:-bench_app}" \
  -e S5_APP_PASSWORD="${S5_APP_PASSWORD:-bench-app-local-password}" \
  -e RPC_STAND_COMMAND_POOL="${RPC_STAND_COMMAND_POOL:-$(( ${CG_K:-200} + 50 ))}" \
  -e RPC_STAND_ENVIRONMENT="${RPC_STAND_ENVIRONMENT:-prod}" \
  -e PYTHONPATH=/w/src:/w/standard:/w/benchmarks:/w/meta \
  -v "$ROOT/src:/w/src:ro" \
  -v "$ROOT/benchmarks:/w/benchmarks:ro" \
  -v "$ROOT/migrations:/w/migrations:ro" \
  -v "$ROOT/packages/sandloom-standard/src:/w/standard:ro" \
  -v "$META:/w/meta:ro" \
  -v "${CG_OUT:-/tmp}:/out" -e CG_ANNOTATE="${CG_ANNOTATE:-0}" -e CG_CACHESIM="${CG_CACHESIM:-0}" \
  -w /tmp \
  sandloom-ladder-cg:3.13 python /w/benchmarks/ladder/cg/cgdriver.py "$@"
