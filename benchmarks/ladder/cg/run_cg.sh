#!/bin/sh
# Build the image and run the cachegrind ladder (host with docker).
#   DATABASE_URL=... CACHE_URL=... BROKER_URL=... ladder/cg/run_cg.sh L0 L2 L3 ...
# localhost / 127.0.0.1 in the URLs become host.docker.internal in the container.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
docker build -q -t sandloom-ladder-cg:3.13 "$HERE" >/dev/null
fix() { echo "$1" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/'; }
exec docker run --rm \
  -e DATABASE_URL="$(fix "$DATABASE_URL")" \
  -e CACHE_URL="$(fix "$CACHE_URL")" \
  -e BROKER_URL="$(fix "$BROKER_URL")" \
  -e CG_K="${CG_K:-200}" \
  -e SANDLOOM_ACCEPTED_RISKS="${SANDLOOM_ACCEPTED_RISKS:-local_storage_in_prod}" \
  -e DB_RLS_MERGED_BEGIN="${DB_RLS_MERGED_BEGIN:-true}" \
  -e LADDER_S5_VARIANT="${LADDER_S5_VARIANT:-uow}" \
  -e PLANNER_OPTIMIZED_EXECUTOR="${PLANNER_OPTIMIZED_EXECUTOR:-false}" \
  -e S5_APP_ROLE="${S5_APP_ROLE:-bench_app}" \
  -e S5_APP_PASSWORD="${S5_APP_PASSWORD:-bench-app-local-password}" \
  -e RPC_STAND_COMMAND_POOL="${RPC_STAND_COMMAND_POOL:-$(( ${CG_K:-200} + 50 ))}" \
  -e RPC_STAND_ENVIRONMENT="${RPC_STAND_ENVIRONMENT:-prod}" \
  -e PYTHONPATH=/w/src:/w/benchmarks \
  -v "$ROOT/src:/w/src:ro" \
  -v "$ROOT/benchmarks:/w/benchmarks:ro" \
  -v "${CG_OUT:-/tmp}:/out" -e CG_ANNOTATE="${CG_ANNOTATE:-0}" -e CG_CACHESIM="${CG_CACHESIM:-0}" \
  sandloom-ladder-cg:3.13 python /w/benchmarks/ladder/cg/cgdriver.py "$@"
