#!/usr/bin/env bash
# Run benchsuite.run inside a CI job with the shared settings.
#
#   ci/run_bench.sh <pin: yes|no> <label> [extra benchsuite.run args...]
#
# Shared settings come from the environment (set by the workflow):
#   BENCH_SCENARIOS BENCH_DURATION BENCH_WARMUP BENCH_REPS BENCH_CONNECTIONS
#   BENCH_SERVICE_CONTAINERS (ids of the Postgres/Redis service containers)
# With pin=yes and >= 2 CPUs the server is pinned to CPU 0 and the load
# generator plus the service containers to the remaining CPUs; the load
# generator's thread count (oha --worker-threads, wrk -t) then equals the size
# of that CPU set.
set -euo pipefail

pin="$1"
label="$2"
shift 2
tools="$RUNNER_TEMP/bench-tools"
export JWT_SECRET="$(openssl rand -hex 32)"

pin_args=()
n="$(nproc)"
if [ "$pin" = "yes" ] && [ "$n" -ge 2 ]; then
  pin_args=(--server-cpus 0 --client-cpus "1-$((n - 1))")
  # shellcheck disable=SC2086 # a space-separated list of container ids
  docker update --cpuset-cpus "1-$((n - 1))" ${BENCH_SERVICE_CONTAINERS} >/dev/null
fi
lscpu | head -20 || true

exec .venv/bin/python -m benchsuite.run \
  --scenarios "$BENCH_SCENARIOS" \
  --duration "$BENCH_DURATION" \
  --warmup "$BENCH_WARMUP" \
  --reps "$BENCH_REPS" \
  --connections "$BENCH_CONNECTIONS" \
  --oha "$tools/oha" --wrk "$tools/wrk" --wrk2 "$tools/wrk2" \
  "${pin_args[@]}" \
  --label "$label" \
  --print-json \
  --markdown-out "$GITHUB_STEP_SUMMARY" \
  "$@"
