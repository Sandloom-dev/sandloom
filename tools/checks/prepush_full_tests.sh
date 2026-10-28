#!/usr/bin/env bash
# pre-push hook `backend-full-tests`: the full suite, on THIS checkout's own
# test database only.
#
# The full suite TRUNCATEs / drops tables of its test database and FLUSHes its
# Redis indexes. On a machine where several checkouts (agents, benchmarks, a
# developer) share the docker test services, the shared default `sandloom_test`
# (.env.test) may belong to another run right now — so the hook refuses to run
# unless the database is named explicitly. (The suite itself additionally
# refuses a database / Redis index another checkout owns —
# sandloom.core.testing.ownership.)
set -euo pipefail

if [ -z "${SANDLOOM_TEST_DATABASE_URL:-}" ]; then
  cat >&2 <<'MSG'
backend-full-tests: refusing to run the full suite on the shared default test
database (.env.test's sandloom_test): SANDLOOM_TEST_DATABASE_URL is not set.

Name this checkout's own database (created on first use) and Redis index, e.g.

  export SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433/sandloom_test_<you>
  export SANDLOOM_TEST_CACHE_URL=redis://localhost:6380/<n>
  export SANDLOOM_TEST_BROKER_URL=redis://localhost:6380/<n>

then push again (or skip this hook once: SKIP=backend-full-tests git push).
MSG
  exit 1
fi

cd "$(dirname "$0")/../.."
docker compose -f deploy/compose/test.yml up -d --wait

# Parallel like CI (one test FILE per worker), without coverage (CI's
# coverage job measures it). Each xdist worker N gets its own database
# (<db>_gwN) and its own Redis indexes, claimed from the free ones on the
# shared test Redis before the workers start (SANDLOOM_TEST_REDIS_DB_STRIDE=auto,
# sandloom.core.testing.resources.allocate_worker_redis: this owner's indexes from
# earlier runs first, then unmarked empty ones). SANDLOOM_PREPUSH_WORKERS=N forces
# a count (0 = serial); the default `auto` is what the PostgreSQL server's
# free connections afford (sandloom.core.testing.connections: 13 per worker, 40
# kept for the multi-process suites).
workers="${SANDLOOM_PREPUSH_WORKERS:-auto}"
if [ "${workers}" = auto ] || [ "${workers}" -ge 2 ]; then
  echo "backend-full-tests: ${workers} xdist workers (allocated Redis indexes)" >&2
  status=0
  SANDLOOM_TEST_REDIS_DB_STRIDE=auto \
    uv run --locked pytest -q --no-cov -n "${workers}" --dist loadfile "$@" || status=$?
  # Exit 4 = the isolation guard refused: too few free Redis indexes (list /
  # prune stale ones with tools/checks/test_resources.py) or a worker database
  # another run owns. Fall back to the serial run on this checkout's own
  # resources instead of touching someone else's.
  [ "${status}" -ne 4 ] && exit "${status}"
  echo "backend-full-tests: no free Redis indexes / a worker database belongs to another run; retrying serially (see: uv run python tools/checks/test_resources.py list)" >&2
fi
echo "backend-full-tests: serial run" >&2
exec uv run --locked pytest -q --no-cov "$@"
