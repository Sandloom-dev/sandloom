#!/bin/sh
# Run ladder/server_profile.py in the ladder image + py-spy (host with docker).
#   DATABASE_URL=... CACHE_URL=... BROKER_URL=... SECURITY_CACHE_URL=... \
#     ladder/cg/run_server_profile.sh cg uow --k 300 --k2 900
# localhost / 127.0.0.1 in the URLs become host.docker.internal in the container;
# results / stacks go to $SP_OUT (default /tmp).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
docker build -q -t sandloom-ladder-cg:3.13 "$HERE" >/dev/null
printf 'FROM sandloom-ladder-cg:3.13\nRUN pip install --no-cache-dir py-spy==0.4.1 \\\n && apt-get update && apt-get install -y --no-install-recommends linux-perf \\\n && rm -rf /var/lib/apt/lists/*\n' \
  | docker build -q -t sandloom-ladder-prof:3.13 - >/dev/null
fix() { echo "$1" | sed 's/localhost/host.docker.internal/; s/127\.0\.0\.1/host.docker.internal/'; }
exec docker run --rm --privileged \
  -e DATABASE_URL="$(fix "$DATABASE_URL")" \
  -e CACHE_URL="$(fix "$CACHE_URL")" \
  -e BROKER_URL="$(fix "$BROKER_URL")" \
  -e SECURITY_CACHE_URL="$(fix "$SECURITY_CACHE_URL")" \
  -e S5_APP_ROLE="${S5_APP_ROLE:-bench_app}" \
  -e S5_APP_PASSWORD="${S5_APP_PASSWORD:-bench-app-local-password}" \
  -e PYTHONPATH=/w/src:/w/benchmarks \
  -v "$ROOT/src:/w/src:ro" \
  -v "$ROOT/benchmarks:/w/benchmarks:ro" \
  -v "${SP_OUT:-/tmp}:/out" -w /w/benchmarks \
  sandloom-ladder-prof:3.13 sh -c 'echo -1 > /proc/sys/kernel/perf_event_paranoid; exec python /w/benchmarks/ladder/server_profile.py "$@"' sh "$@"
