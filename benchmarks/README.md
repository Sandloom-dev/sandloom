# Sandloom comparative benchmarks

> **Public source scope.** The maintained HTTP harness, security/correctness
> tests, ladder diagnostics and agent DX tasks are included. Native compiler /
> Rust prototypes, the old dependency-pinning stand, raw runs and competitor
> apps are archived outside the maintained source distribution. Historical measurements mentioned
> below are context, not new performance claims or shipped result artifacts.
> See `docs/contributing/source-export.md` for the reviewed source selection.


A reproducible benchmark suite that measures what Sandloom costs on top of the
stack it is built on, and how ASGI servers / event loops / Python versions
change that. It compares:

| impl | what it is |
| --- | --- |
| `sandloom` | the Sandloom platform composition root (`sandloom.main`) loading one tiny module built with `define_module` (`benchsuite/bench`), run with **production defaults** (`ENVIRONMENT=prod`): every middleware, the request Unit of Work, the real auth guard (`get_current_user_id` → JWT verify + framework revocation check), `publish_event` into the transactional outbox, the Redis event bus and the outbox relay | <!-- stale-ok: names the benchmark app's kernel guard -->
| `fastapi` | bare FastAPI — **the exact FastAPI / Starlette / pydantic / uvicorn / SQLAlchemy / asyncpg / redis / pyjwt versions of `uv.lock`** — with SQLAlchemy async + asyncpg |
| `litestar` | bare Litestar (msgspec structs) with the **same** SQLAlchemy access as `fastapi`, so the difference is the HTTP layer |
| `litestar-asyncpg` | Litestar with **raw asyncpg** (no ORM) for S3/S4 — a clearly labelled ceiling showing what the ORM costs, not an apples-to-apples DB comparison |

This is its own uv project (`benchmarks/pyproject.toml`, `benchmarks/uv.lock`):
granian, litestar, msgspec and psutil never enter the framework's dependencies.
The framework distributions are editable path dependencies under `../packages/`,
so a run measures the checked-out framework code.

### Dependency updates

After changing package requirements, resolve `benchmarks/uv.lock` with the uv
version pinned in CI, then refresh the Cachegrind image's locked export from
the repository root:

```bash
uv lock --project benchmarks
python tools/checks/check_benchmark_dependencies.py --write
python tools/checks/check_benchmark_dependencies.py
```

CI checks the lock and export together. Rebuild the Cachegrind image after an
update. Historical measurements retain their original environments; rerun both
sides of a comparison with the same updated dependencies before claiming a new
performance result.

## Scenarios (identical semantics in every implementation)

| | request | work | response |
| --- | --- | --- | --- |
| S1 | `GET /s1` | none | `200 {"ok": true}` |
| S2 | `GET /s2` + `Authorization: Bearer <HS256 JWT>` | verify signature/exp/nbf, `token_type == "access"`, `iat` present, then **one Redis `GET`** of the subject's revocation mark (`sandloom:auth:tokens_valid_after:<sub>`, Sandloom's key) | `200 {"ok": true, "user_id": "<sub>"}`; 401 without/with a bad token |
| S3 | `GET /s3/{id}` (ids uniform in 1..9999) | one indexed `SELECT` by primary key of a small row | `200 {"id", "name", "value"}`; 404 when absent |
| S4 | `POST /s4 {"name", "value"}` | INSERT a `bench_writes` row **and** an outbox row in **one** transaction | `201 {"id": <new id>}` |

Before measuring, every implementation must pass a contract check (same
statuses and bodies, 401/404 paths included). After S4 the runner verifies
that every written row has exactly one outbox row (`s4_outbox_ok`; a ⚠ in the
table otherwise) and, for Sandloom, how many events were already delivered to the
bus (`s4_outbox_delivered`).

## Server / runtime matrix

| server | command (single worker) |
| --- | --- |
| `uvicorn-h11` | `uvicorn --loop asyncio --http h11` |
| `uvicorn-httptools` | `uvicorn --loop asyncio --http httptools` |
| `uvicorn-uvloop` | `uvicorn --loop uvloop --http httptools` |
| `granian-asyncio` | `granian --interface asgi --http 1 --no-ws --loop asyncio` |
| `granian-uvloop` | `granian … --loop uvloop` |
| `granian-rloop` | `granian … --loop rloop` |

Every implementation runs on every server with **one worker** (per-core
efficiency). A separate `scaling` job then runs `sandloom` and `fastapi` with
**4 workers** on the two servers where Sandloom did best (geometric mean of its
single-worker rps), or on servers you name.

Python: 3.12 and 3.13 run the full matrix. For 3.14 and 3.14t (free-threaded)
the planner checks the lock for packages without a prebuilt wheel; such a
version gets a single **probe** job that attempts the real install (source
builds included), reports exactly what blocks in the summary, and — if the
install works — measures `uvicorn-uvloop` and `granian-uvloop`. At the time of
writing the lock has no wheel for:

- **3.14**: `asyncpg==0.30.0`, `msgpack==1.1.0`, `pydantic-core==2.33.2`
  (pydantic 2.11.7 is pinned by the framework; 3.14 support needs pydantic ≥ 2.12)
- **3.14t**: the above plus `greenlet==3.2.3`, `orjson==3.11.9`,
  `psycopg2-binary==2.9.13`

`python benchsuite/wheels.py cp314t` prints the current list.

## Methodology and fairness rules

- **Same versions.** The packages Sandloom and the bare apps share are pinned to
  `uv.lock`; `python -m benchsuite.check_versions` fails the job when
  they drift (bump them together with the framework lock).
- **Same backends and pool.** Real Postgres 16 and Redis 7 service containers
  per job. Every app reads the same `DATABASE_URL` / `CACHE_URL` / `JWT_SECRET`
  and uses Sandloom's default pool shape (SQLAlchemy LIFO pool, 10 + 20 overflow,
  no pre-ping; Redis blocking pool of 64, hiredis everywhere).
- **Same tables.** All implementations read `bench_items` and write
  `bench_writes`. The bare apps write their event into `bare_outbox_messages`,
  created as `LIKE outbox_messages INCLUDING ALL` — Sandloom's outbox columns,
  defaults and indexes — so index maintenance is identical. The write tables
  are truncated before every server start.
- **No logging in the hot path.** Access logs are off for every server and
  every app logs at `WARNING`. Sandloom keeps its production JSON log format.
- **No rate limiting** (the load generator is a single client IP).
- **TLS negotiation off** for Postgres (`ssl=disable`, applied to all apps):
  the bench Postgres has no TLS, and asyncpg's default `sslmode=prefer`
  crashes on rloop's transport (`NotImplementedError: TCPTransport protocol
  cannot be changed`). This is a real-world finding: granian `--loop rloop`
  cannot serve an asyncpg app whose DSN leaves TLS negotiation on.
- **Sandloom is measured as deployed.** Its numbers include work the bare apps do
  not do: in S4 the post-commit dispatch of the event to the Redis stream and
  the background outbox relay; in every scenario the framework middleware
  (request id, security headers, process time, body-size limit, idempotency
  capture, error masking, CORS, metrics, …). That is the point:
  the ratio is the framework's real overhead.
- **Load generators** (`--loadgen oha|wrk|wrk2|all`, workflow input
  `loadgen`, default `oha,wrk`) — all HTTP/1.1 keep-alive, the same number of
  connections (default 64) and the **same client thread count** (oha
  `--worker-threads` = wrk `-t` = the CPUs left after pinning the server; the
  CPU count − 1 when unpinned):
  - [`oha`](https://github.com/hatoo/oha) **1.16.0** — binary pinned by
    SHA-256. Closed loop.
  - [`wrk`](https://github.com/wg/wrk) **4.2.0** (`a211dd5`) — built from that
    commit in CI. Closed loop; it is the faster client, so it can push a
    server further than oha.
  - [`wrk2`](https://github.com/giltene/wrk2) (`44a94c1`) — built from that
    commit, with the LuaJIT 2.1 of the pinned wrk commit swapped in for its
    bundled LuaJIT 2.0, which cannot allocate on some kernels. **Open loop at a
    fixed rate** (`-R`): latency is measured from the *intended* send time
    (coordinated-omission corrected, HdrHistogram), so it is the honest latency
    comparison.
  - The wrk/wrk2 binaries are cached with `actions/cache`, keyed by both commit
    SHAs. Caches are fine; artifacts are not.
- **Same script for wrk and wrk2** (`benchsuite/lua/bench.lua`):
  - S2 sets the `Authorization` header.
  - S3 draws a random id per request.
  - S4 POSTs a JSON body with `Content-Type` and a per-request **unique**
    name/value.
  - S1/S2 use wrk's static request (no Lua on the hot path).
  - There is no `response()` hook, because it would slow the client. Errors
    come from wrk's own counters: every response ≥ 400 (`errors.status`) plus
    connect/read/write/timeout are reported as errors, and `ok = requests −
    status errors`.
  - oha counts any status other than the expected one (200, or 201 for S4) as
    an error.
  - wrk/wrk2 run with a 10 s request timeout (oha has none), so a slow DB tail
    is not miscounted as errors.
- **Closed-loop measurement.** Each run is a fixed duration (default 10 s,
  whole seconds for wrk) after a discarded warm-up (default 3 s) per scenario.
  With several closed-loop generators they measure the **same server instance**
  back to back; their order rotates per repetition. Requests still in flight at
  the deadline are not counted.
- **Cross-check.** When both oha and wrk ran, the summary has a side-by-side
  table and **flags every row where they disagree by more than 10 %**. A
  disagreement means the client, not the server, may be the limit there, or the
  row is noisy.
- **Fixed-rate latency (wrk2).** It runs in the `latency` jobs on the two
  servers where Sandloom did best (or `latency_servers`), for the headline scenarios
  S2/S3/S4 (`wrk2_scenarios`).
  - The target rate is **70 % of the closed-loop max** measured for that
    (impl, server, scenario) **in the same job**: the job first runs one wrk
    pass, then wrk2.
  - Inputs: `rate_fraction`, a fixed `rate`, and `rate_basis=common`, which
    drives every implementation at 70 % of the slowest one's max (same offered
    load, so latencies compare directly).
  - wrk2 calibrates for ~10 s and then resets its histograms. The runner adds
    that time on top of the measured window, and it doubles as the warm-up.
  - Reported: target vs achieved rate (⚠ when achieved < 97 % of target, i.e.
    the server could not keep up), p50/p90/p99/p99.9, errors, CPU µs/req.
- **Repetitions are interleaved.** Each repetition starts every server fresh,
  with emptied write tables and a contract check, and runs the implementations
  in rotated order (a,b,c / b,c,a / c,a,b), so VM drift spreads evenly. Tables
  report the **median** of the repetitions and the min–max rps range.
- **CPU pinning (Linux).** In single-worker jobs the server is pinned to CPU 0.
  The load generator, Postgres and Redis share the remaining CPUs (`taskset` +
  `docker update --cpuset-cpus`). The 4-worker job is unpinned.
- **CPU per request.** `(user+sys)` CPU of the server's whole process tree
  (psutil; includes granian workers and runtime threads, and Sandloom's background
  tasks), divided by successful requests. It is much less sensitive to runner
  noise than rps, and it is the main per-core efficiency metric.
- **Compare within a job.** All implementations in one row group ran on the
  same VM; different jobs ran on different runner VMs (the `Runners` table
  shows each job's CPU model). Treat cross-job rows (server vs server, Python vs
  Python) as indications, prefer CPU µs/req for them, and re-run before
  drawing conclusions from small differences.

### BENCHJSON schema

One document per job: `{"meta": {...}, "results": [...], "failures": [...]}`
(or `{"meta", "blocked"}` for an interpreter that could not be installed).

- `meta`: label, python, CPU model and count, git SHA, `load_generators`
  (name → version / pinned SHA), package versions, params.
- `results[]` (one per impl × server × workers × scenario × loadgen × rep):
  - identity: `impl`, `server`, `workers`, `rep`, `scenario`, `loadgen`;
  - throughput and latency: `rps`, `ok`, `errors`, `error_detail`,
    `p50_ms`/`p90_ms`/`p99_ms`/`p999_ms`, `elapsed_s`;
  - server cost: `cpu_us_per_req`, `rss_mb`;
  - wrk2 only: `target_rps`, `achieved_rps`;
  - S4 only: `s4_rows`, `s4_outbox_rows`, `s4_outbox_ok`, and
    `s4_outbox_delivered` (Sandloom).

  Rows without `loadgen` (older documents) were measured with oha.

## S5 — the secure SaaS read (Sandloom only, opt-in)

S3 has no JWT, no tenant, no RLS. S5 is the realistic read: `GET /s5/{id}` with
an access JWT (`get_current_user_id`), `TenantScope()`, `require(Subscribed())` <!-- stale-ok: names the benchmark app's kernel guard -->
(claim-cheap), one PK read of the `TenantScoped` table `bench_tenant_items`
under FORCEd PostgreSQL RLS, the app connected as the NON-superuser,
NOBYPASSRLS role `bench_app`. Mounted only with `BENCH_S5=1` (its
`TenantScoped` model turns on the ORM tenant guard's per-statement verdict,
which must not leak into S1–S4).

| variant | route | how the tenant reaches Postgres | round trips (with RLS) |
| --- | --- | --- | --- |
| `uow` | `/s5/{id}` | framework `tenant_rls` hook: `set_config(..., true)` on the request transaction | 4: BEGIN, set_config, SELECT, COMMIT |
| `ro` | `/s5ro/{id}` + `DB_READ_ONLY_SESSIONS=true` | same (reads keep their transaction under `tenant_rls`) | 4 |
| `fast` | `/s5fast/{id}` | raw asyncpg: `BEGIN READ ONLY; SELECT set_config(...)` in one message | 3 |
| `fn` | `/s5fn/{id}` | plpgsql `bench_s5_get(tenant, id)`: sets the GUC, then queries (autocommit) | 1 |
| `plan` | `/s5plan/{id}` | declared `ReadOperation` via the execution planner, GENERAL executor (read-only ORM session; `tenant_rls` hook) | 4: BEGIN READ ONLY, set_config, SELECT, ROLLBACK |
| `planopt` | `/s5plan/{id}` + `PLANNER_OPTIMIZED_EXECUTOR=true` | same declaration, OPTIMIZED executor: `BEGIN READ ONLY; set_config` in one message | 3 |

Plus one Redis `GET` (the revocation mark) per request in every variant.
`plan`/`planopt`: see `docs/design/execution-planner.md` (their executor tests:
`benchmarks/tests/test_planner_executors.py`).
Correctness / security (every variant: cross-tenant 404, RLS alone blocks,
fail-closed without a tenant, no GUC leak on a pool of one, 401/402/403 with
zero DB I/O, read-only write refusal, no leaked connection / idle-in-transaction,
the autocommit and CTE evaluation-order traps):

```bash
docker compose -f deploy/compose/test.yml up -d --wait
PYTHONPATH=../benchmarks:src uv run pytest -c pyproject.toml ../benchmarks/tests -q --no-cov
# parallel, as CI runs it (the load-harness tests test_harness_*.py stay serial):
PYTHONPATH=../benchmarks:src uv run pytest -c pyproject.toml ../benchmarks/tests -q --no-cov \
    -n auto --dist loadfile --ignore=../benchmarks/tests/test_harness_loadclient.py \
    --ignore=../benchmarks/tests/test_harness_dataset.py
```

CI runs this suite on every PR touching `src/` or `benchmarks/` (job
`benchmarks-security` in `.github/workflows/ci.yml`, `SANDLOOM_E2E_REQUIRED=1`: a
missing database fails instead of skipping). `S5_TEST_ROLE` overrides the
cluster-wide app role when several runs share one PostgreSQL server; under
xdist each worker suffixes it (`<role>_gw<N>`) and gets its own database and
Redis indexes, as in the backend suite ([Testing → Isolation
guards](../docs/guides/testing.md#isolation-guards); on a Redis with few
databases set `SANDLOOM_TEST_REDIS_DB_STRIDE=auto`).

Benchmarking a variant (from `benchmarks/`, `DATABASE_URL` = the OWNER DSN):

```bash
uv run python -m benchsuite.setup_db                 # + S5 table/RLS/function/role
uv run python -m benchsuite.s5 smoke all             # S5 contract, every variant
eval "$(uv run python -m benchsuite.s5 env fast)"    # uow|lazy|ro|fast|fn|plan|planopt
TOKEN=$(uv run python -m benchsuite.s5 token)
uv run uvicorn benchsuite.apps.sandloom_app:app --loop uvloop --http httptools \
    --no-access-log --log-level warning --port 8000 &
oha -z 20s -c 64 -H "Authorization: Bearer $TOKEN" \
    --rand-regex-url 'http://127.0.0.1:8000/s5fast/[1-4][0-9]{0,3}'
# or wrk: BENCH_SCENARIO=s5 BENCH_S5_ROUTE=/s5fast/ BENCH_TOKEN=$TOKEN \
#   wrk -t2 -c64 -d20s -s benchsuite/lua/bench.lua http://127.0.0.1:8000
```

### Comparing builds: interleaved A/B over the socket (`ladder/stage_ab.py`)

Measures the same S5 variants (`uow`, `ro`, `fast`, `fn`) plus the S3 control
(`s3`: no JWT / tenant / RLS) on several checkouts, alternating them back to
back (and reversing the order every round) so host drift hits every build
alike. Server CPU µs/request and rps at conc 64 (uvicorn uvloop + httptools,
1 worker, `LingeringQueuePool`), or p50/p99 at fixed open-loop rates.

```bash
SP=/some/scratch && cd <repo>
for x in base:<commit-a> new:<commit-b>; do
  mkdir -p $SP/${x%%:*} && git archive ${x##*:} | tar -x -C $SP/${x%%:*}
  (cd $SP/${x%%:*}/benchmarks && uv sync -q)
done
cd benchmarks
export DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433/<own_db> \
       CACHE_URL=redis://localhost:6380/9 BROKER_URL=redis://localhost:6380/8
uv run python -m benchsuite.setup_db          # S5 table, RLS policy, bench_s5_get, role
uv run python -m benchsuite.s5 smoke all      # contract of every variant
export STAGES=base=$SP/base,new=$SP/new
uv run python ladder/stage_ab.py socket --rounds 3            # ~15 min per build
uv run python ladder/stage_ab.py latency --rounds 3 --variants uow --rates 540,865
uv run python ladder/stage_render.py socket; uv run python ladder/stage_render.py latency
# instructions (cachegrind, Linux container) per build, S5 and S3 in-process:
(cd $SP/base && DATABASE_URL=... benchmarks/ladder/cg/run_cg.sh L6S5 L6S5f L6)
# the RLS hook alone (1 connection, 1 open transaction, prebuilt SELECT):
benchmarks/ladder/cg/run_cg.sh L2Wp L2Wn L2Wo L2Wns L2Wos
```

Then the security suites on the measured build (security is part of the result):
`uv run pytest src/sandloom/core/tests/test_tenant_rls.py
src/sandloom/core/tests/test_tenant_rls_paths.py -q` and the S5 suite above. NOTE:
`setup_db` (re)creates the RLS policy from `sandloom.core.sql_alchemy.rls`, so after a
policy change re-run it before measuring; numbers are only comparable for the
same policy.

### Whole-workload integration runs (`ladder/run_int.py`)

`server_bench.py --mix` / `split_bench.py` drive a weighted workload
(`benchsuite.mix` profiles) against one or several uvicorn processes. Their
client is `benchsuite.loadclient`, whose accounting is pinned by negative
controls in `tests/test_harness_loadclient.py` (a fake server answering wrong
statuses, 5xx, timeouts and dropped connections at known rates; run in CI by
the `benchmarks-security` job):

* every planned request is `ok` or exactly one error class
  (`unexpected_status`, `server_error`, `timeout`, `connection`) — counted in
  the closed AND the open loop, warm-ups included;
* each request is checked against its OWN workload entry's `expect`;
* latency is `time.perf_counter` (uvloop's `loop.time()` is 1 ms-quantised);
* CPU per operation divides by SUCCESSFUL operations, and any error makes the
  script exit 1 after printing its JSON.

`run_int.py` interleaves configurations per round and, before every server
start, resets the run's databases to the seeded state
(`python -m benchsuite.dataset reset`), flushes its Redis dbs and records every
table's row count: a run whose counts differ from the database's first
snapshot is recorded `"invalid": true` (`tests/test_harness_dataset.py` pins
the reset). `ladder/agg_int.py` turns the JSONL into tables; the login (argon2)
cost is measured by the `login` section and the `*-nologin` profiles, not
subtracted as a constant.

## Diagnostic stands (RPC cost decomposition, write path without argon2, soak)

Never-production stands are ready to measure: one contract call
decomposed layer by layer (client codec / signing / httpx / server fast path
/ contract runtime / UoW / planner / command dedupe; rungs `R*`, socket
`ladder/rpc_stand.py`), and the write path W1-W6 on the secure S5 stack
(INSERT, + outbox, + audit, UPDATE ORM vs Core, a two-table transaction,
the same as a command contract; rungs `L6W*`), and a fault-injection soak
of the merged BEGIN (`DB_RLS_MERGED_BEGIN` ON vs OFF side by side: random
client aborts turned into handler cancellations, `pg_terminate_backend`,
continuous invariants, then a CPU / p99 A/B; `ladder/soak.py`). Rungs, pinned round trips,
the exact cpu / cachegrind / socket commands and the list of suspected
costs: [`ladder/STANDS.md`](ladder/STANDS.md).

## Running it on GitHub Actions

Manual only (`workflow_dispatch`); it never runs on push or pull requests.

```bash
# full default matrix (closed loop with oha + wrk, wrk2 on the best 2 servers)
gh workflow run benchmarks.yml --ref <branch>

# a quicker, focused run
gh workflow run benchmarks.yml --ref <branch> \
  -f python=3.13 -f scenarios=s1,s4 \
  -f servers=uvicorn-uvloop,granian-uvloop -f duration=10 -f reps=3

# push the throttle: wrk only, more connections
gh workflow run benchmarks.yml --ref <branch> -f loadgen=wrk -f connections=256

# every generator in every bench job (expensive), and a common wrk2 rate
gh workflow run benchmarks.yml --ref <branch> -f loadgen=all -f rate_basis=common

# fixed wrk2 rate, chosen servers; or skip the extra jobs
gh workflow run benchmarks.yml --ref <branch> -f rate=2000 -f latency_servers=granian-uvloop
gh workflow run benchmarks.yml --ref <branch> -f scaling_servers=none -f latency_servers=none

gh run watch            # then open the run page
```

Inputs: `scenarios`, `python`, `servers`, `impls`, `loadgen`, `duration`,
`warmup`, `reps`, `connections`, `scaling_servers` / `latency_servers`
(`auto` / list / `none`), `latency_impls`, `wrk2_scenarios`, `rate`,
`rate_fraction`, `rate_basis`. The runner label comes from the repository
variable `BENCH_RUNNER` (default `ubuntu-24.04`).

Where the results are:

- every job's **step summary** has its own tables. The **`report` job's
  summary** has the combined tables for the whole run: headline Sandloom ÷ FastAPI,
  the oha-vs-wrk cross-check, wrk2 fixed-rate latency, then per-scenario detail
  with a loadgen column.
- the raw numbers are printed to each job's log as `BENCHJSON` lines, and the
  `report` job log has the merged JSON. Nothing is uploaded as an artifact
  (the account's artifact storage quota is exhausted), so results travel
  between jobs through the run's logs (`ci/fetch_job_logs.sh`).

Render locally from a run:

```bash
cd benchmarks
gh run view <run-id> --log | uv run python -m benchsuite.render -
gh run view <run-id> --log | uv run python -m benchsuite.render - --json-out results.json
```

Runtime and cost: the default dispatch is 14 bench jobs (12 full + 2 probes),
then 2 scaling jobs and 2 latency jobs, all parallel within each stage.

| stage | duration | why |
| --- | --- | --- |
| bench job (`oha,wrk`) | ~20 min | 14 impl×scenario runs × 3 reps × (3 s warm-up + 2 × 10 s) + setup |
| scaling job | ~12 min | |
| latency job | ~14 min | wrk calibration + 3 reps × 9 runs × ~22 s |
| **total wall time** | **≈ 35–40 min** | |

Private repositories run on 2-vCPU `ubuntu-24.04` runners: there the
4-worker job is CPU-starved (it mostly shows contention, not scaling), and the
client gets a single CPU. Set the `BENCH_RUNNER` variable to a larger runner
label if your organisation has one.

## Running locally (smoke only)

Local numbers on a busy development machine are **not** trustworthy — use
this to check that everything starts and produces numbers, nothing more.
`wrk` is used when it is on `PATH` (e.g. `brew install wrk`), otherwise it is
skipped with a note; wrk2 has no maintained macOS package — build it with
`ci/build_wrk.sh` on Linux.

```bash
cd benchmarks
uv sync
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sandloom_bench
export CACHE_URL=redis://localhost:6379/5 BROKER_URL=redis://localhost:6379/6
uv run python -m benchsuite.check_versions
uv run python -m benchsuite.setup_db           # creates the DB if missing
uv run python -m benchsuite.run --impls sandloom,fastapi \
  --servers uvicorn-uvloop,granian-uvloop --scenarios s1 --loadgen oha,wrk \
  --duration 3 --warmup 1 --reps 1 --oha /path/to/oha --json-out smoke.json
uv run python -m benchsuite.render smoke.json
```

## Layout

```
benchsuite/
  common.py            shared env/config + scenario contract
  bench/               the Sandloom module (define_module: api/, events.py, models, services)
  apps/sandloom_app.py      Sandloom entry point (sandloom.main:app)
  apps/fastapi_app.py  bare FastAPI
  apps/litestar_app.py bare Litestar (SQLAlchemy)
  apps/litestar_asyncpg_app.py  Litestar + raw asyncpg
  bare/                auth + SQLAlchemy shared by the bare apps
  setup_db.py          core migrations + scenario tables + seed
  loadgen.py           oha / wrk / wrk2 drivers (one result shape)
  lua/bench.lua        wrk/wrk2 script: headers, bodies, unique ids, WRKJSON
  run.py               the runner (servers, phases, psutil, contract checks)
  render.py            JSON / CI logs → Markdown (stdlib only)
  check_versions.py    shared-version + key-layout fairness guard
  wheels.py            which locked packages lack a wheel for a Python tag
ci/
  plan.py              workflow matrix (full jobs vs probe jobs)
  build_wrk.sh         build wrk / wrk2 from pinned commits
  run_bench.sh         shared CI invocation (pinning, tools, settings)
  blocked.py           report an environment that could not be installed
  fetch_job_logs.sh    collect BENCHJSON from this run's job logs
```

## Local release qualification

For strict Docker deployment checks, fault injection, wrk load measurements and
a clean-database restore exercise, see [qualification/README.md](qualification/README.md).
