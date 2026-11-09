# Diagnostic stands: RPC cost decomposition and the write path without argon2

Two **diagnostic, never-production** stands, prepared so that measurement can
start as soon as a machine is free. Nothing here changes framework behaviour:
the code lives in `benchsuite` (the `bench` module, mounted / registered only
under `BENCH_RPC_DIAG=1` / `BENCH_WRITE_DIAG=1`) and in `ladder/`.

Context (main `4021d0d`): one internal RPC call costs ~1.4 ms of CPU in total
in the microservices split, and the split costs +22 % CPU per operation
compared with the monolith. The write-heavy profile costs 9.47 ms of total CPU
per operation, a large share of it argon2 login. Postgres CPU per operation is
3× that of the read-heavy profile, and the planner does not help writes.

| | stand A: RPC | stand B: writes |
|---|---|---|
| code | `benchsuite/rpcstand.py`, `benchsuite/bench/rpc_diag.py`, `ladder/stands.py`, `ladder/rpc_stand.py` | `benchsuite/writes.py`, `benchsuite/bench/{write_models,write_diag}.py`, `benchsuite/bench/api/diag_write.py`, `ladder/stands.py` |
| functional tests | `tests/test_rpc_stand.py` (59) | `tests/test_write_stand.py` (23) |
| in-process rungs | `R0raw`, `R<m>[t].<layer>` (97 rungs) | `L6W1 … L6W6` |
| socket CPU | `ladder/rpc_stand.py` (caller CPU + provider CPU) | `ladder/server_bench.py` (+ PG / Redis container CPU) |

All commands run from `benchmarks/` with `DATABASE_URL` / `CACHE_URL` /
`BROKER_URL` exported, the same way as for `ladder.py`. Use a database of
your own. The role is cluster-wide, so set your own `S5_APP_ROLE` when other
people share the server.

## A. RPC cost decomposition (one contract call, layer by layer)

### Rungs: what the provider runs (`benchsuite.rpcstand.METHODS`)

Every method has the wire signature of `tenancy.membership.v1.get_role`,
`(organization_id: UUID, user_id: UUID) -> str | None`. Codec, signing and
transport work is therefore identical on every rung. Only the provider body
changes.

| rung | provider body | adds on top of the previous rung | PG RT | Redis |
|---|---|---|---|---|
| `R0raw` | the bare coroutine | floor: no contract machinery | 0 | – |
| `R0` | `bench.rpcdiag.v1.noop`, `planned=True` | the contract runtime: bind and validate arguments, read-only scope; **no UoW, no DB** | 0 | – |
| `R2` | `uow_noop`: plain read contract | the provider's read UoW: session, `session.info` stash, tenant scope, rollback by exception. Sends nothing (the connection is lazy) | 0 | – |
| `R2q` | `uow_select` | + pool checkout, `BEGIN`, `SELECT 1`, `ROLLBACK` | 3 | – |
| `R3` | **real** `tenancy.membership.v1.get_role`, planner GENERAL executor | the real JoinRead (`BEGIN READ ONLY` / SELECT / ROLLBACK) | 3 | – |
| `R3o` | the same, `PLANNER_OPTIMIZED_EXECUTOR=true` | optimized executor: autocommit, one statement | 1 | – |
| `R3u` | `role_uow`: the same SQL in a plain read UoW | the pre-planner form, for comparison | 3 | – |
| `R4` | `touch`: a COMMAND (`operation_key`) with a fresh key | framework dedupe: reserve (`SET NX`) and terminal record (`EVALSHA` CAS) | 0 | SET, EVALSHA |
| `R4r` | `touch` with the same key every call | the dedupe hit: replay, the body never runs | 0 | SET |

The RT / Redis columns are pinned by `test_round_trips_per_call`: they are
identical for the local path and the RPC path, and zero on the client-only
layers. A `t` suffix (`R0t.*`, `R3t.*`, …) binds a tenant. The call then
carries the **signed RPC context**: JSON + base64 + HMAC-SHA256 on the
client, then verification + `bind_tenant` on the server. Without a tenant the
proxy sends no context and no signature
(`test_signed_context_only_on_tenant_rungs`).

### Layers: which side of the wire is measured (`benchsuite.rpcstand.LAYERS`)

| layer | measures |
|---|---|
| `loc` | local `core.get_service(...)` (the monolith path, outside any request read scope, so no memo) |
| `enc` | **client only, no transport**: the proxy's encode, context + HMAC, envelope decode and result decode, against a recorded provider reply (`_post` stubbed) |
| `cli` | `enc` + httpx client machinery (`MockTransport` returning the recorded reply, no socket) |
| `srv` | **server only**: a prebuilt, signed request driven straight into the ASGI app (`RpcFastPathMiddleware` → token / signature verification → decode → contract runtime → encode). Commands use 40 000 prebuilt distinct keys (`RPC_STAND_COMMAND_POOL`) |
| `rpc` | client + server in one process (httpx `ASGITransport`, no socket) |
| `rpcn` | `rpc` with a **new proxy per call**: the remote `core.get_service()` builds one on every call |

Attribution, per call:

```
contract runtime (local)      = R0.loc  - R0raw
client side                   = R0.enc            (codec + context + envelope)
httpx client (no socket)      = R0.cli  - R0.enc
server side                   = R0.srv
glue / ASGITransport          = R0.rpc  - (R0.cli + R0.srv)
proxy construction per call   = R0.rpcn - R0.rpc
signed tenant context         = R0t.<layer> - R0.<layer>      (enc: sign, srv: verify+bind)
provider read UoW             = R2.srv  - R0.srv
DB round trips (SELECT 1)     = R2q.srv - R2.srv
real membership read          = R3.srv / R3o.srv / R3u.srv - R0.srv
command dedupe                = R4.srv  - R2.srv ;   replay = R4r.srv
socket + uvicorn + kernel     = rpc_stand.py client/server CPU - (R*.cli / R*.srv)
```

### Commands

```bash
# 0. database: the tenancy schema + one org / owner (owner DSN; the ENV of
#    the stand = benchsuite.rpcstand.server_env: ENVIRONMENT=prod,
#    SANDLOOM_MODULES=[sandloom_tenancy, benchsuite.bench], BENCH_RPC_DIAG=1)
python -m benchsuite.platform seed
python -m benchsuite.rpcstand smoke                # every method x layer x tenant
uv run python ladder/ladder.py list | grep '^R'    # the 97 rungs

# 1. protocol-level round trips per call (functional; no CPU measurement)
for r in R0.rpc R2.rpc R2q.rpc R3.rpc R3o.rpc R3u.rpc R4.rpc R4r.rpc; do
  uv run python ladder/ladder.py rt $r --n 50 --warmup 20; done

# 2. CPU per call in-process (one rung per process; median of reps)
for r in R0raw R0.loc R0.enc R0.cli R0.srv R0.rpc R0.rpcn \
         R0t.enc R0t.srv R0t.rpc R2.srv R2q.srv R3.srv R3o.srv R3u.srv \
         R4.srv R4r.srv R3.rpc R3o.rpc R4.rpc; do
  uv run python ladder/ladder.py cpu $r --n 5000 --reps 5 --warmup 500 --loop uvloop
done | tee ladder/results/rpc_cpu.jsonl
#    R4.srv: (reps + 1) * n must stay below RPC_STAND_COMMAND_POOL (40 000)

# 3. instructions per call (cachegrind, Linux container; Ir(K) - Ir(0))
CG_K=200 ladder/cg/run_cg.sh R0raw R0.loc R0.enc R0.cli R0.srv R0.rpc R0.rpcn \
  R0t.enc R0t.srv R2.srv R2q.srv R3.srv R3u.srv R4.srv R4r.srv
PLANNER_OPTIMIZED_EXECUTOR=true CG_K=200 ladder/cg/run_cg.sh R3o.srv R3o.rpc
CG_ANNOTATE=1 CG_OUT=/tmp/cg ladder/cg/run_cg.sh R0.enc R0.srv   # per-function Ir
#    (run_cg.sh sets RPC_STAND_COMMAND_POOL = CG_K + 50: both runs prebuild
#    the same pool, so it cancels)

# 4. Python-level hot spots (cProfile / call counts / allocations)
uv run python ladder/ladder.py prof  R0.srv --n 3000
uv run python ladder/ladder.py calls R0.enc --n 1000
uv run python ladder/ladder.py alloc R0.rpc --n 2000

# 5. over a REAL loopback socket: caller CPU vs provider CPU per call
#    (provider = uvicorn sandloom_standard.main:app, one worker, with the stand env; the
#    caller = RemoteServiceProxy on the shared pooled httpx client, conc 1)
export LADDER_PG_CONTAINER=sandloom_postgres_test LADDER_REDIS_CONTAINER=sandloom_redis_test
for r in R0 R2 R2q R3 R3o R3u R4 R4r; do
  uv run python ladder/rpc_stand.py $r --n 5000 --reps 5; done | tee ladder/results/rpc_socket.jsonl
uv run python ladder/rpc_stand.py R0 --tenant --n 5000 --reps 5
uv run python ladder/rpc_stand.py R0 --http h11       # provider HTTP parser
#    smoke only: --n 3 --reps 1 --warmup 2 --no-containers

# 6. transport A/B (the caller's RPC_TRANSPORT; only the client changes)
#    --conc N: N concurrent callers; new_connections_per_call is reported.
#    A build without RPC_TRANSPORT (main before the switch) runs httpx only:
#    run THIS checkout's stand with that build's interpreter
#    (PYTHONPATH=$PWD; the provider runs the build's sandloom).
for t in httpx aiohttp lean; do
  RPC_GUARD_DB=<your db> RPC_GUARD_CACHE=:6390/ uv run python ladder/rpc_guard.py \
    ladder/results/rpc_transport_socket.jsonl '{"build": "branch"}' -- \
    uv run python ladder/rpc_stand.py R3o --conc 64 --transport $t; done
#    RPC_STAND_PROFILE=/tmp/p.prof RPC_STAND_PROFILE_CPU=1: a cProfile of one
#    extra, unmeasured rep. rpc_guard.py waits for a quiet host (no foreign
#    pytest / soak, /tmp/sandloom-ci-local.lock not held — a flock probe via
#    tools/ci/ci_lock.py, not the file's existence) and tags the JSON line.

# 6b. the PROVIDER at a fixed rate, per transport (split, RPC singles):
#    ladder/rpc_provider_probe.py (--probe: work counters in every service via
#    probe_site/sitecustomize.py; --profile: thread-CPU cProfile of the
#    provider); per op CPU, ctx switches, instructions / cycles / clock
#    (ladder/darwin_rusage.py), pg_stat_database, established connections.
#    Interleaved: ladder/run_owasp.py provider. On macOS read fixed-rate CPU µs
#    together with cycles: the P-cluster clock follows the host load.
# 7. the integration topology (monolith vs split, mixed profile), per
#    transport: ladder/run_rpctx.py (run_int.py's stand layout and guards).
```

### Suspected costs to verify (from code reading at `4021d0d`, not measured yet)

Client (`RemoteServiceProxy`):

1. **A new proxy per call.** In a split, `CoreFacade.get_service()` builds a
   new `RemoteServiceProxy` on every call. Callers resolve per request, so
   each call pays `__init__` and the `__getattr__` closure again (`R0.rpcn`).
   The local path also rebuilds: `local_contract()` returns
   `bind_contract(...)`, a new `ContractProvider` each time, and its
   per-instance method cache is always cold.
2. **URL and timeout per call.** The URL is built with an f-string and two
   `quote()` calls. httpx then re-parses that absolute string into an
   `httpx.URL`, a costly step in httpx, and merges it with the base URL. The
   `timeout=<float>` argument becomes a new `httpx.Timeout`.
3. **Headers.** `_rpc_headers()` builds a new dict and re-runs
   `from sandloom.core.observability.request_context import request_id_var` on every call.
   httpx then normalises and encodes the headers again.
4. **Signing (tenant calls only).** `json.dumps` + base64 + `time.time()`
   build the context. The signature computes `sha256(body).hexdigest()`, then
   `hmac.new(token.encode(), …)`. The key is re-encoded and the HMAC pads are
   re-derived on every call; a precomputed `hmac` object plus `.copy()` would
   avoid that.
5. **Per-call bookkeeping.** `strip_session_arguments`, `encode_typed`,
   msgpack pack and unpack, about three case-insensitive
   `response.headers.get` lookups, an `await self._remote_contract(hash)`
   coroutine even on a cache hit, and an `lru_cache` lookup in `wire_plan()`
   that hashes a typing annotation.
6. **Commands and undescribed methods** call `invalidate_contract_read_memo()`
   twice.
7. **httpx/httpcore** creates Request and Response objects per call, takes
   the connection-pool lock, and runs the auth / redirect wrapper loops. The
   **h11** state machine parses HTTP/1.1 in pure Python on the client, while
   the provider parses with httptools in C. Check whether `R0.cli - R0.enc`
   and the socket client CPU dominate.

Server (`/_sandloom/rpc` fast path):

8. **Fast-path overhead.** `RpcFastPathMiddleware._match` runs twice
   (`__call__` and `_serve`). Two lazy imports run per call (`request_id_var`,
   `metrics`). Each call also builds a Starlette `Request` and goes through
   the body-cap wrapper.
9. **Repeated lookups.** `has_internal_access()` and later `_server_token()`
   each read the token. Four to five `request.headers.get` calls follow, and
   each one scans the header list.
10. **Context verification.** `_verify_context` recomputes `sha256(body)` and
    the HMAC, then `json.loads` + base64 + `UUID()`. It runs only when a
    context is sent.
11. **A provider per call.** `_resolve_local_contract` → `local_contract` →
    a **new `ContractProvider` per call**, plus a `contract_database` lookup.
12. **A pydantic object for three fields.** `RpcRequest.model_construct` runs
    per call, then `_decode_arguments` and `runtime.bind` (signature binding +
    two UUID validations).
13. **Planned path (R0/R3).** Each call creates a `Reads(...)` object, enters
    the `read_only_scope` and `configured_tenant_scope` context managers, and
    runs `reads.aclose()` even when nothing was read.
14. **Read UoW (R2).** The contract UoW factory creates an `AsyncSession`.
    Each call writes three `session.info` keys and runs `_stash_stores` and
    `_isolated_uow_scope`. The read rollback is signalled by **raising and
    catching `_ReadOnlyRollback` on every read call**, then `session.close()`.
15. **Response.** `encode_result_as`, then `_ok_response`, which builds a
    Starlette `Response` and re-encodes the constant `X-SANDLOOM-Contract-Hash`
    header every call.
16. **Metrics.** Two Prometheus `.labels(...)` lookups (tuple hash + lock),
    plus `observe` and `inc`.
17. **Commands (R4).** Each command adds two Redis round trips (reserve +
    CAS record). `_command_arguments` re-binds the signature. `_args_digest`
    runs `contract_wire.digest`: encode + orjson + sha256. On success the
    result is encoded and `orjson.dumps` runs just to measure its size.
18. **Planner (R3 vs R3o).** The general executor takes 3 round trips; the
    optimized one takes 1 when there is no `tenant_rls`.

## B. Write path without argon2 (W1–W6)

Every route carries the **secure S5 stack**: JWT with the revocation check,
`TenantScope()`, `require(Subscribed())`, and `TenantScoped` tables under
FORCEd RLS behind a NOBYPASSRLS app role. The numbers are therefore
comparable to S5, and **no password hashing** happens anywhere. `?fail=true`
raises after all of a route's writes.

| rung | route | adds | PG round trips (pinned) | Redis (pinned) |
|---|---|---|---|---|
| W1 | `POST /dw/1` | one ORM INSERT in the request UoW | BEGIN, set_config, INSERT, COMMIT = **4** | MGET (revocation) |
| W2 | `POST /dw/2` | + `publish_event` (outbox row) | 5 in the tx + **1 after commit** (mark delivered, one autocommit UPDATE) = **6** | MGET, XADD |
| W3 | `POST /dw/3` | + `record_audit` | 6 + 1 = **7** | MGET, XADD |
| W4o | `POST /dw/4o/{id}` | UPDATE by PK: ORM load-modify-flush | BEGIN, set_config, SELECT, UPDATE, COMMIT = **5** | MGET |
| W4c | `POST /dw/4c/{id}` | UPDATE by PK: one `UPDATE … RETURNING` | **4** | MGET |
| W5 | `POST /dw/5` | order + line + outbox + audit + `on_commit` | 7 + 1 = **8** | MGET, XADD |
| W6 | `POST /dw/6` | W5 as a **command contract** (operation key) on the contract pool | **8** | MGET, SET, EVALSHA, XADD |

With `DB_RLS_MERGED_BEGIN` (default on since this branch) BEGIN and
set_config are ONE message, so every rung is one round trip lower: W1 3,
W2 5, W3 6, W4o 4, W4c 3, W5 7, W6 7 (the table shows the
`DB_RLS_MERGED_BEGIN=false` counts). These counts come from the ladder `rt`
mode against the S5 env: the
non-superuser role, `tenant_rls`, and the Redis Streams bus (XADD). The
tests pin the same lists (`test_round_trips_per_rung`), apart from XADD,
because the harness uses an in-memory bus. Correctness is pinned in
`tests/test_write_stand.py`:

- Rows land for the bound tenant only, and RLS `WITH CHECK` refuses a
  foreign `tenant_id`.
- Outbox, audit and `on_commit` commit **iff** the change commits; a forced
  failure leaves nothing behind.
- W4 is tenant-scoped (cross-tenant → 404) and rolls back.
- W6 is deduplicated on its key: a replay returns the same id, and different
  arguments return 409.
- 401/402 reject the request before any write.

### Commands

```bash
# 0. database: S5 schema + RLS + app role + the write-stand tables (owner DSN)
export S5_APP_ROLE=bench_app_w S5_APP_PASSWORD=...   # your own cluster role
python -m benchsuite.setup_db
python -m benchsuite.writes smoke                     # every rung, fail path, 401, cross-tenant

# 1. round trips per op (functional): PG wire RTs + Redis commands, with the
#    post-commit dispatch included (each op awaits drain_post_commit)
for r in L6W1 L6W2 L6W3 L6W4o L6W4c L6W5 L6W6; do
  uv run python ladder/ladder.py rt $r --n 50 --warmup 20; done

# 2. CPU per op in-process, and instructions per op
for r in L6W1 L6W2 L6W3 L6W4o L6W4c L6W5 L6W6; do
  uv run python ladder/ladder.py cpu $r --n 3000 --reps 5 --warmup 300 --loop uvloop
done | tee ladder/results/write_cpu.jsonl
CG_K=200 ladder/cg/run_cg.sh L6S5 L6W1 L6W2 L6W3 L6W4o L6W4c L6W5 L6W6
uv run python ladder/ladder.py prof L6W5 --n 2000

# 3. over a real socket: server CPU/op + Postgres and Redis container CPU/op
python -m benchsuite.writes env > /tmp/w.env
export LADDER_PG_CONTAINER=sandloom_postgres_test LADDER_REDIS_CONTAINER=sandloom_redis_test
for r in W1 W2 W3 W4o W4c W5 W6; do   # bench-cmd prints the server_bench line
  eval "$(python -m benchsuite.writes bench-cmd $r /tmp/w.env --n 5000 --reps 5)"
done | tee ladder/results/write_socket.jsonl
# the S5 read as the reference row, on the same server env:
uv run python ladder/server_bench.py --env-file /tmp/w.env --path /s5/ --ids 5000 \
  --header "Authorization: Bearer $(python -m benchsuite.writes token)" --label S5 \
  --n 5000 --reps 5
```

`server_bench.py` runs with `OUTBOX_RELAY_ENABLED=off` by default, so the
relay poller's CPU never leaks into the per-operation numbers. The
post-commit dispatch runs as in production. The container CPU readings are
**upper bounds** on a shared container.

### Suspected costs to verify (from the pinned round trips and code reading)

1. **(Fixed: 4 -> 1 RT.)** The mark is now ONE autocommit UPDATE inside the
   `non_tenant_statements()` allowlist, and the relay's sweep / claim / settle
   transactions skip the tenant setting too (relay tick 12 -> 9 RT; pinned by
   `test_relay_round_trips_on_the_rls_database`). Original finding:
   **Marking the outbox row delivered takes its own transaction.** After the
   post-commit dispatch, Sandloom runs BEGIN, **set_config(tenant)**, `UPDATE
   outbox_messages SET delivered_at`, COMMIT. That is **+4 PG round trips per
   event-publishing request**: W2 has 9 round trips against W1's 4, and the
   set_config is for a table that is not tenant-scoped. This is the first
   suspect for "Postgres CPU per op is 3× the read-heavy profile". Options:
   leave the marking to the relay, batch it, or skip the tenant GUC for core
   tables.
2. **(Fixed: merged BEGIN is the default.)** **The RLS GUC is its own round
   trip** after BEGIN. The
   `DB_RLS_MERGED_BEGIN` prototype merges the two. Measure W1 with it on.
3. **The outbox and audit INSERTs are separate round trips** at flush (no
   batching across the two tables), and every ORM INSERT is `… RETURNING id`.
4. **W4o vs W4c.** The ORM load-modify-flush costs an extra SELECT plus
   identity-map and dirty-tracking work, against a single UPDATE.
5. **`publish_event` preflight.** Channel and JSON-schema validation (cached
   validator) and an orjson encode to check the size run inside the
   transaction, then the outbox row is serialised again.
6. **`record_audit`** builds the row and serialises `data` with JSON.
   **`on_commit`** adds a queue and a callback after commit.
7. **W6 command.** The dedupe adds 2 Redis round trips. The arguments are
   digested (encode + orjson + sha256), and the result is encoded and
   `orjson.dumps`-ed to size it. A contract-pool checkout comes on top of
   W5's database work.
8. **Per-request baseline (as in S5).** JWT verification, the revocation
   MGET, the tenant binding, and `Subscribed()`.

## C. Soak: merged BEGIN ON vs OFF under faults

`DB_RLS_MERGED_BEGIN` (default ON since #46) sends `BEGIN; SELECT
set_config(...)` as one simple-query message and then hand-adopts the
transaction into asyncpg + SQLAlchemy private state
(`sql_alchemy/tenant_rls.py` through the asyncpg DBAPI adapter
`drivers/dbapi_asyncpg.py`, guarded by its probe in `sql_alchemy/driver_compat.py`).
This stand checks that the shortcut stays correct when requests are cancelled
at random points and backends die, by running BOTH configurations at the same
time under the same load and comparing. Code: `ladder/soak.py` (orchestrator
+ load client), `ladder/soak_app.py` (server wrapper). DIAGNOSTIC ONLY.

### What runs

| | |
|---|---|
| instances | two `uvicorn soak_app:app` (one worker, uvloop + httptools), `ENVIRONMENT=prod`, the S5 + write-stand env (`benchsuite.writes.server_env`): NOBYPASSRLS role, FORCEd RLS, `tenant_rls`, Redis Streams bus, outbox relay ON. `on` = `DB_RLS_MERGED_BEGIN=true` on `sandloom_test_soak_on` (Redis db 10/11), `off` = `false` on `sandloom_test_soak_off` (db 12/13) |
| pool | `DB_POOL_SIZE=3`, `DB_MAX_OVERFLOW=1`, `DB_POOL_TIMEOUT=10` (a leak surfaces as a pool timeout within seconds) |
| load | one client process per instance, 12 keep-alive connections, two tenants picked per request: `/s5/{own}` 30, `/s5/{foreign}` 8 (must 404), `/s5plan/{own}` 10, `/s5list` 7 (every row must be own), `POST /dw/3` 30 (row + outbox + audit), `POST /dw/4c/{own}` 10 (UPDATE), `POST /dw/4c/{foreign}` 5 (must 404) |
| client aborts | phases of `--phase-seconds` (120 s) cycle: `early` 7 % aborted at U(0, 0.6)×median latency of the route, `spread` 7 % at U(0, 1.5)×, `heavy` 10 % at U(0.2, 1.2)×. The client resets the socket; `soak_app.py` turns the disconnect into a real `task.cancel()` of the handler (uvicorn alone never cancels it), so the CancelledError lands in auth, the pool checkout, the merged message, a statement or COMMIT |
| backend kills | every `--kill-every` s (U(0.5, 1.5)× 2.5 s) `pg_terminate_backend` of a random app-role backend of each database (idle or busy, the relay's included) |
| counters | `soak_app.py` wraps `dbapi_asyncpg._merged_begin` (calls / ok / errors by type), listens to the pool's `connect` / `checkout` / `invalidate` and serves them with the pool state on `GET /_soak/state`. Reuses = checkouts - connects |

### Invariants (every `--tick` s; the first violation aborts with `pg_stat_activity`, the server state and the log tail)

- no session `idle in transaction` for longer than `--idle-grace` (3 s);
- backends of the role ≤ pool bound + 2, pool `checkedout` ≤ its bound,
  server in-flight tasks ≤ concurrency + 4 (no stuck handler);
- 5xx ≤ backend kills + 10, and every server ERROR record is classified by
  its FINAL exception: a disconnect / admin shutdown / `CancelledError` is
  expected; `another operation (N) is in progress` with N ≠ 2, `already in
  transaction`, a pool timeout, `current transaction is aborted`, tenant
  errors are fatal; anything unrecognised is fatal;
- cross-tenant probes: a foreign `/s5` or `/dw/4c` id is 404, every `/s5list`
  row belongs to the caller's tenant, `/s5` returns the asked id;
- outbox: nothing pending for more than `--stuck-seconds` (120 s), no dead row;
- ON: the driver probe is OK and the merged BEGIN keeps being used; OFF: it is
  never called;
- latency: a route whose 5 s p50 exceeds 4× its first-90 s p50 is logged as
  a warning (shared machine: never an abort).

After the targets (per instance: ≥ `--target-cancels` 5 000 server-side
cancellations, ≥ `--target-kills` 300 kills, ≥ `--target-reuses` 1 M reuses)
or `--max-minutes` (30): clients stop, the relay gets `--drain-seconds` to
deliver, then pool drained (`checkedout = 0`, no in-flight task), no
idle-in-transaction session, outbox fully delivered and **write atomicity**:
every `bench_tenant_writes` row has exactly one outbox row and one audit row
(no orphan either way), every id a 201 returned exists with the caller's
tenant, rows per tenant ∈ [acknowledged, acknowledged + uncertain] (uncertain
= aborted / 5xx / dropped), counter sums likewise.

### Dead pooled connections (found while building it; fixed)

Killing a backend whose connection sits IDLE in the pool leaves asyncpg in
`ERROR_CONSUME` (protocol state 2: it read the FATAL 57P01 and waits for a
ReadyForQuery that never comes) or — once the close is processed — closed.
Before the fix the NEXT statement on it failed with `InternalClientError:
cannot switch to state 12|15; another operation (2) is in progress` (or
"connection is closed") and the request was a **500** (kills-only diagnosis,
2-3 min, 1 kill/s: ON 46 / OFF 36 such records; aborts only: zero).

`sql_alchemy/dead_connection.py` now separates:

* **connection recovery** — only when provably nothing reached the server: a
  connection asyncpg reports closed is replaced at checkout (no round trip);
  the first message of a fresh transaction that asyncpg refused BEFORE
  writing a byte (`ERROR_CONSUME` / closed) is re-sent once on a new
  connection (`sandloom_db_dead_connection_replaced_total{phase}`; kill switch
  `DB_DEAD_CONNECTION_RETRY=false`);
* **classification** — everything else that fails on a dead connection (a
  statement / COMMIT that was sent: outcome unknown) is never retried; it is a
  disconnect of that one connection and a **503** `DatabaseUnavailable`.

The client counts 5xx per status (`server_5xx_by_status`), the server state
carries `dead_conn_replaced`; `SOAK_SERVER_<SETTING>=v` passes a setting to
both servers (A/B). Kills-only runs on the fix branch (1 kill/s,
`--cancel-rate 0 --keep-going`, shared machine), ON / OFF:

| run | kills | 500 | 503 | recovered (phase) |
|---|---|---|---|---|
| A: checkout check only (`DB_DEAD_CONNECTION_RETRY=false`), 2 min | 119 / 118 | 0 / 0 | 118 / 116 | 0 / 0 (checkout) |
| B: + recovery, 2 min (first cut) | 117 / 121 | **23** / 0 | 94 / 104 | 22 merged_begin / 16 tenant_sync |
| C: + recovery, finalizer fix, 1 min | 59 / 60 | **0 / 0** | 48 / 48 | 10 merged_begin + 1 checkout / 11 tenant_sync |

* The zero-round-trip checkout check alone changes nothing under load (the
  checkout almost always races the close: the connection is in
  `ERROR_CONSUME`, which asyncpg does not expose); with classification alone
  every such request is a 503 instead of a 500.
* Recovery removes them: ~1 in 6 kills hit an idle connection that the next
  request then took. The 23 500s of run B were a bug of the first cut: closing
  the dead pool record dropped its characteristic-reset finalizers, so a
  connection recovered under a planner READ ONLY checkout stayed read-only
  (`cannot execute INSERT in a read-only transaction`); fixed (finalizers are
  carried over; regression test).
* The remaining 503s are not preventable without a re-execution: run C ON 42 /
  OFF 36 `AdminShutdownError` (the backend was killed under a statement that
  was sent) and 6 / 12 never-sent refusals inside an already OPEN transaction
  (the connection died between two statements of the request: the
  transaction is lost). Integrity after every run: writes = outbox rows =
  audit rows, acked ids owned by their tenant, nothing stuck.
* Alternative measured with `ladder.py rt L6S5`: pinging on EVERY checkout
  (`DB_POOL_PING_IDLE_SECONDS=0`) would also catch `ERROR_CONSUME` but costs
  +1 round trip per request (3 → 4; it was 3 → 6 before the ping fix in the
  same change: SQLAlchemy 2.1's adapter has no `await_`, so the "one round
  trip" ping silently ran BEGIN / `;` / ROLLBACK). Recovery costs 0 round
  trips (RT per request unchanged: L5 / L6 / L6S5 / L6S5p 3.0 = main) and
  +5 / +8 Python calls per request (`ladder.py calls` L6 / L6S5, of 1 445 /
  3 177). Recommendation: keep the idle threshold, rely on recovery.

### Commands

```bash
# from benchmarks/ of THIS checkout; servers run from a git-archive export
git archive main | tar -x -C $SRV_ROOT && (cd $SRV_ROOT/benchmarks && uv sync --frozen)
export SOAK_SRV=$SRV_ROOT/benchmarks
export SOAK_PG=postgresql://test_user:test_password@localhost:5433/   # owner
export SOAK_REDIS_PORT=6390 SOAK_REDIS_CONTAINER=sandloom_redis_bench
export S5_APP_ROLE=soak_app S5_APP_PASSWORD=...                     # own role
PY=$SOAK_SRV/.venv/bin/python
$PY ladder/soak.py setup                           # both databases (idempotent)
$PY ladder/soak.py run --out ladder/results/soak.jsonl         # <= 30 min
$PY ladder/soak.py ab  --out ladder/results/soak_ab.jsonl      # 5 min A/B
# diagnosis: isolate one fault, never abort
$PY ladder/soak.py run --out /tmp/k.jsonl --max-minutes 2 --kill-every 1 \
    --cancel-rate 0 --keep-going
```

The A/B (`ab`) restarts both instances with the NORMAL pool (10 + 20), relay
off, no faults, and drives ONE instance at a time (closed loop, 8
connections, `--slot` 30 s): a warm-up pair, then `--blocks` × ABBA. Per
slot: server CPU/request (psutil), Postgres CPU/request summed over the
instance's OWN backends (`/proc/<pid>/stat` in the container, so other
databases on the shared server do not count), rps, p50 / p99 over every
request of the slot. The summary is the median over slots.

### Results (main `0c31f7c`, 2026-10-11, macOS host, shared Docker PG 16 / Redis 7)

Data: `ladder/results/soak_20261011.jsonl` (ticks every 5 s + summary),
`ladder/results/soak_ab_20261011.jsonl` (A/B slots + summary).

Soak: targets reached after **25.2 min**, **no violation**, every final
check green on both instances.

| | ON (merged) | OFF |
|---|---|---|
| requests OK | 1 019 469 | 972 865 |
| server-side cancellations (client aborts) | 58 882 (69 940) | 59 614 (67 889) |
| of which landed IN the merged `BEGIN; set_config` message | 4 261 CancelledError (+105 ConnectionDoesNotExist, +104 InternalClientError from kills) | – |
| backend kills | 598 | 599 |
| connection reuses (checkouts - connects) | 1 052 326 | 1 002 986 |
| merged BEGINs sent OK | 1 046 197 | 0 |
| 5xx (all classified: terminated connections) | 540 | 557 |
| invalidations: CancelledError / disconnect / state-2 | 18 324 / 361 / 224 | 17 011 / 393 / 195 |
| max backends, max idle-in-tx (instant, none stale) | 4, 4 | 4, 4 |
| max outbox pending (never stuck > 120 s, no dead row) | 46 | 46 |
| W3 rows = outbox rows = audit rows | 309 838 | 294 896 |
| final: pool drained, no idle-in-tx, outbox delivered, all acked ids owned by their tenant, rows/counters within [acked, acked + uncertain] | ✔ | ✔ |
| cross-tenant probe violations | 0 | 0 |

A/B (normal pool 10 + 20, relay off, no faults, 8 connections, one instance
loaded at a time, warm-up pair + 2 × ABBA of 30 s; medians of 4 slots each;
host load average 6-8 from other work, so read small deltas with care):

| | ON | OFF | ON vs OFF |
|---|---|---|---|
| rps | 886 | 848 | +4.6 % |
| server CPU / request | 1 109 µs | 1 154 µs | -3.8 % |
| Postgres CPU / request (own backends) | 256 µs | 274 µs | -6.6 % |
| p50 | 8.72 ms | 9.09 ms | -4.1 % |
| p99 | 13.49 ms | 14.82 ms | -9.0 % |

Server CPU: all four ON slots are below all four OFF slots. PG CPU: ON is
lower in 3 of the 4 adjacent ON/OFF pairs (slot 2 vs 3 is the exception). Verdict: keep `DB_RLS_MERGED_BEGIN` ON by default.

## C. Saga rung (ADR-0008): start path, per step, connection hold

`ladder/saga_rung.py` runs the real saga runtime of an app built by
`create_app` (no HTTP, no consumers) against PostgreSQL and reports, per
operation, the median / p95 wall time and the SQL statements sent on the
default and contract pools:

| rung | one operation |
|---|---|
| `B0` | baseline business UoW: one INSERT, COMMIT |
| `S1` | the same UoW + `start_saga` → start-path cost = `S1 − B0` |
| `L1` | one advance of a local step (lock, savepoint, step upsert, instance update, outbox nudge, COMMIT) |
| `C1` | one advance of a command step (+ the provider's command-log claim and result update) |

The connection (and the instance row lock) is held from the `SELECT … FOR
UPDATE` to the `COMMIT`: `L1` / `C1` are an upper bound of that hold (they
also include the post-commit outbox dispatch). Over internal RPC add one RPC
call (stand A).

```bash
DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433 \
CACHE_URL=redis://localhost:6380/198 SAGA_RUNG_DB=sandloom_bench_saga \
../.venv/bin/python ladder/saga_rung.py --n 1000 \
    --json ladder/results/saga_rung_$(date +%F).json
```

`tests/test_saga_rung.py` pins the statement budget: start `+3` (instance
INSERT, outbox nudge, its post-commit mark), local step `5`, command step
`7`. First run (2026-10-25, Apple Silicon dev machine, shared, load ≈ 3–4,
n = 1000; `ladder/results/saga_rung_2026-10-25.json`):

| rung | median µs | p95 µs | statements |
|---|---|---|---|
| B0 | 1245 | 1683 | 1 |
| S1 | 3562 | 4178 | 4 |
| L1 | 5027 | 8608 | 5 |
| C1 | 7030 | 8571 | 7 |

Start path ≈ +2.3 ms (3 statements) over the business UoW; a step ≈ 5 ms
local / 7 ms with a local command — dominated by commit round trips on a
loaded machine. Re-measure on an exclusive machine before sizing from it.

## D. Infra-agnostic branch vs main (cachegrind A/B, 2026-11-05)

What the infra-agnostic work (`design/infra-agnostic/spec.md` §9, §10 rows 2,
6, 7, 7b) costs on the measured paths on PostgreSQL: `main` `10be493` against
`claude/modules-portable` `93e8ab6`, each from its own `git archive` (its own
`src/`, `migrations/` and `benchmarks/`, with this change's `ladder.py` and
`infra_rungs.py` copied into both), one database per build, the same
cachegrind image (`cg/Dockerfile`, Python 3.13, aarch64), K = 200,
`LADDER_CG_GC=freeze`.

**Why `freeze`.** In the default GC mode a full collection fell inside the
measured window of the branch's L6S5 only (the branch has a larger startup
heap), which read as +5.9 %; with the collector off the same rung is −1.6 %.
`freeze` is production's `GC_FREEZE_AFTER_STARTUP` (the startup heap is never
traversed again) and removes that step; it agrees with `off` within 0.1 %.
Compare default-mode numbers between builds only when the heaps are alike.

New rungs for the run (`ladder/infra_rungs.py`): `XSgS` / `XSgL` / `XSgC`, the
saga rung's start, local step and command step (instances pre-started, a fixed
count, so the setup cancels), and `XCmp`, a compile-cache miss of a `select`
with comparisons, `IN` and `coalesce` (no I/O, deterministic).

```bash
export DATABASE_URL=postgresql+asyncpg://<owner>@localhost:5433/<own_db> CACHE_URL=... BROKER_URL=...
python -m benchsuite.setup_db && python -m benchsuite.platform seed && python ladder/write_login.py seed
LADDER_CG_GC=freeze CG_ANNOTATE=1 CG_OUT=$OUT CG_K=200 ladder/cg/run_cg.sh \
  WLlf HTr Pme Porgs Pmem WLr WLc HCs L6S5 L6S5p L6S5po L6R L6 L5 \
  L6W1 L6W3 L6W5 HW7 HEc XCmp XSgS XSgL XSgC
python ladder/cg/funcdiff.py $OUT_MAIN/HW7.annot $OUT_BRANCH/HW7.annot 200
```

`run_cg.sh` mounts `migrations/` (the core chain) and writes the entry points
of `packages/*/pyproject.toml` into a dist-info on the path: without them a
module's events have no producer (B015) and the branch finds no SQL renderer
(B024). `R4.loc` is not in the run (the RPC stand's env refuses local object
storage in prod); `WLc` and `HW7` cover the command and claim paths.

Instructions per op (`run 2`: a second, independent pair of runs of the rows
above 1 %; same image, same databases):

| rung | path | main | branch | Δ % | run 2 Δ % |
|---|---|---:|---:|---:|---:|
| `WLlf` | login (cheapest argon2) | 13,745,992 | 13,852,735 | +0.78 |  |
| `HTr` | refresh | 8,727,118 | 8,930,588 | +2.33 | +1.88 |
| `Pme` | /auth/me | 3,003,344 | 2,982,571 | -0.69 |  |
| `Porgs` | list orgs | 2,906,282 | 2,879,297 | -0.93 |  |
| `Pmem` | list members | 3,869,017 | 3,831,847 | -0.96 |  |
| `WLr` | role change | 16,850,280 | 16,835,076 | -0.09 |  |
| `WLc` | credits spend (command) | 9,615,898 | 9,738,060 | +1.27 | +2.11 |
| `HCs` | local contract read | 1,471,174 | 1,455,390 | -1.07 | -1.30 |
| `L6S5` | RLS read (merged BEGIN) | 3,694,802 | 3,637,955 | -1.54 | -1.32 |
| `L6S5p` | planner, general executor | 3,528,223 | 3,558,185 | +0.85 |  |
| `L6S5po` | planner, optimized executor | 2,178,396 | 2,171,209 | -0.33 |  |
| `L6R` | ReadOnlyDbSession | 2,449,100 | 2,457,723 | +0.35 |  |
| `L6` | /s3 read | 2,236,710 | 2,270,718 | +1.52 | +1.56 |
| `L5` | UoW + get | 1,559,202 | 1,558,590 | -0.04 |  |
| `L6W1` | insert | 3,907,920 | 3,913,064 | +0.13 |  |
| `L6W3` | insert + event + audit | 6,865,474 | 6,842,159 | -0.34 |  |
| `L6W5` | two tables + event + audit | 7,932,137 | 7,947,621 | +0.20 |  |
| `HW7` | class F write (idempotency guard) | 8,683,385 | 8,930,833 | +2.85 | +3.03 |
| `HEc` | event delivery | 4,986,292 | 5,001,291 | +0.30 |  |
| `XCmp` | compile-cache miss | 2,047,326 | 2,144,029 | +4.72 |  |
| `XSgS` | saga start | 6,431,900 | 6,551,768 | +1.86 | +1.64 |
| `XSgL` | saga local step | 10,688,472 | 10,913,737 | +2.11 | +2.08 |
| `XSgC` | saga command step | 14,694,413 | 14,917,778 | +1.52 | +1.15 |

**Noise.** A rerun of the same build moves a rung by 0.0–0.3 % (one
isolated rerun each) (HTr, HW7, L6,
L6S5, XSgS, XSgL, XSgC, HCs), except `WLc` (main 9.62 / 9.72 M, branch 9.74 / 9.92 M:
about 1–2 %; the ledger it appends to grows from run to run). A delta below 1 % is
within the stand's resolution. One callgrind pair of `XSgL` (K = 100) read
the branch 1.4 % *lower*, so the saga rows' cause is not pinned down at the C
level.

**What costs more, and why** (per op; `funcdiff` on the cachegrind annotations
plus Python call counts per op, cProfile, main vs branch):

- **`HW7`, +2.9 to 3.0 %: the HTTP idempotency claim builds its lock statement
  per call.** main executes a prebuilt `text("SELECT pg_try_advisory_xact_lock(:ns, :k)")`
  with the key as execution parameters; the branch takes
  `optional_lock_statement_for(...)`, whose PostgreSQL `_pg_lock` returns
  `base.bindparams(ns=..., k=...)`: a new statement each call (`_clone`,
  `_generative`, `_generate_cache_key`, +106 Python calls per op, +43 k
  instructions in the eval loop alone). Also new per claim:
  `dedupe_log._remember_ttl` (the expiry computed in Python),
  `effect_rows._write_arrays`, `portable._is_int` ×3, `dialect_name` ×2.
  Fix: keep the statement cached and bind the key at execution (the renderer
  returns the statement and its parameters).
- **`XCmp`, +4.7 % on a compile-cache miss only: the global
  `@compiles(BinaryExpression)` and `@compiles(coalesce)` hooks.** Every binary
  expression and `coalesce` goes through `sqlalchemy.ext.compiler`'s dispatch
  and `_is_json_path_text` (+70 calls per compile of a four-term `WHERE`). A
  cached statement never pays it. Fix: refuse a `json_path` comparison where
  the `json_path` comparator builds it (and on `_JsonPath`), not by a global
  hook on every `BinaryExpression`; or return through
  `compiler.visit_binary` before any check when neither side is a
  `_JsonPathText`.
- **`HTr`, +1.9 to 2.3 %: `UtcDateTime` processing.** New per refresh:
  `portable.process_result_value` ×14 (`type_api.process` ×18), `astimezone`
  ×3, `process_bind_param` ×4, plus some annotation and cache-key work.
  asyncpg already returns aware UTC values for `timestamptz`: on PostgreSQL the
  type can skip its result processor (`result_processor` returns `None`).
- **`WLc`, +1.3 to 2.1 %; saga rungs, +1.2 to 2.1 %; `L6`, +1.5 %.** Python
  work grows by 10 (`L6`) to 116 (`XSgC`) calls per op: per-unit
  `nesting_policy`, `uow._close`, `dbapi_asyncpg.connection_closed`; per
  claim or upsert the renderer lookups (`dialect_name`, `sql_dialect`,
  `_require`, `conflict_insert_for`, `for_update_for`); `dispatch._mark_in_line`
  / `_cancelling`. Resolving the renderer once per engine and keeping the
  per-unit checks on the adapter object would remove most of them.
- **Cheaper:** the RLS read (`L6S5`, −1.3 to −1.5 %: the merged `BEGIN` now
  built by `begin_and_bind` and the asyncpg adapter), list reads
  (`Porgs` / `Pmem`, about −1 %), the contract read (`HCs`).

Spec rows: claims and upserts (`conflict_insert`, `id_in`) cost nothing
measurable except item 1's statement building; the RLS `BEGIN` is cheaper;
`begin_statement` for read-only units is within noise (`L6S5p` +0.85,
`L6S5po` −0.33, `L6R` +0.35); the adapter's checkout check and the nesting
policy are one call per unit each (`L5` −0.04 %).

Data: `ladder/results/cg_infra_agnostic_2026-11-05.json`.

### After the perf fixes (infra-perf-regressions, 2026-11-05)

The four fixes of `design/infra-agnostic/spec.md` "Perf fixes, as built",
measured the same way: `main` `10be493` against the branch at `0fa1f85` plus
the fixes (uncommitted, its `src/` copied over a `git archive`), one database
per build (`cg_main_perf`, `cg_fix_perf`), its own Redis, K = 200,
`LADDER_CG_GC=freeze`. Two independent pairs, A and B; the fixed tree of A
lacks the last two refinements (the tighter `BinaryExpression` / `coalesce`
dispatch and the session read of `dialect_name`), B has all of them.
"before" is the Δ of the table above (run 1 / run 2).

| rung | path | before Δ % | main A | fixed A | Δ % | main B | fixed B | Δ % |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `HW7` | class F write (idempotency guard) | +2.85 / +3.03 | 8,630,823 | 8,673,849 | +0.50 | 8,656,796 | 8,685,999 | +0.34 |
| `XCmp` | compile-cache miss | +4.72 | 2,053,349 | 2,074,490 | +1.03 | 2,046,703 | 2,060,237 | +0.66 |
| `HTr` | refresh | +2.33 / +1.88 | 8,767,802 | 8,895,291 | +1.45 | 8,672,906 | 8,907,061 | +2.70 |
| `WLc` | credits spend (command) | +1.27 / +2.11 | 9,443,400 | 9,355,816 | -0.93 | 9,457,321 | 9,544,549 | +0.92 |
| `XSgS` | saga start | +1.86 / +1.64 | 6,303,315 | 6,479,214 | +2.79 | 6,422,580 | 6,543,394 | +1.88 |
| `XSgL` | saga local step | +2.11 / +2.08 | 10,680,256 | 10,910,967 | +2.16 | 10,584,465 | 10,850,544 | +2.51 |
| `XSgC` | saga command step | +1.52 / +1.15 | 14,661,555 | 14,935,510 | +1.87 | 14,574,048 | 14,861,304 | +1.97 |
| `L6` | /s3 read | +1.52 / +1.56 | 2,232,388 | 2,219,974 | -0.56 | 2,252,146 | 2,215,434 | -1.63 |

**Drift.** Unlike the first table's databases, these two were reused from A
to B, and the same build moved between the pairs: main's `HTr` by −1.1 %,
`XSgS` by +1.9 %, `XSgL` by −0.9 %, `L6` by +0.9 %, the fixed tree's `XSgS`
by +1.0 %. On this stand a delta of 1–2 % on those rungs is within its
resolution; `HW7` and `XCmp` moved by 0.3 % or less.

What the fixes did, and what is left:

- **`HW7`: +2.9 % → +0.3 to +0.5 %.** The lock is a cached statement plus
  parameters (`LockStatement`); the per-claim clone and cache-key work is
  gone. Left: one `dialect_name` and the TTL bookkeeping of the durable
  claim (the expiry is computed in Python, which is what lets SQLite share
  the claim).
- **`XCmp`: +4.7 % → +0.7 %.** No `sqlalchemy.ext.compiler` dispatch per
  binary expression any more: `BinaryExpression` and `coalesce` dispatch
  through one function each that does what SQLAlchemy's generated dispatch
  does, after a `frozenset` membership test and, for a comparison, two type
  checks. That check is what remains (about 3 k instructions per binary on
  a compile-cache miss; a cached statement pays nothing). After review the dispatch
  hands over to an application's earlier `@compiles` hook (one module-global
  test on the fast path) and drops two `typing.cast` calls; re-measured
  without a database (`--network none`, `XCmp` only): fixed 2,064,002 against
  pair B's 2,060,237 (+0.2 %), main 2,030,509 there (16 k below its pair-B
  count in that environment), so +1.65 % against main in that run. The
  remaining calls per op over main are the checks' `isinstance` / `getattr`
  only (+16, C-level). It cannot move to
  build time: for `Model.id == json_path(...)` the left side's comparator
  builds the expression and SQLAlchemy consults nothing of the right side's
  type there.
- **`L6`: +1.5 % → −0.6 to −1.6 %;** **`WLc`: +1.3 / +2.1 % → −0.9 / +0.9 %**
  (`WLc` moves 1–2 % between runs of one build, see above). The policy
  cached on the session factory, the inlined close and the per-call renderer
  lookups without `_require` / `sql_dialect` frames.
- **`HTr`: +2.3 % → +1.5 to +2.7 % (main itself moved 1.1 %).**
  `UtcDateTime` on PostgreSQL now has no result processor and only refuses a
  naive time on bind: its 14 `process_result_value`, 18 `type_api.process`
  and 3 `astimezone` calls per op are gone. What remains is not the kernel:
  the branch's identity module reads the refresh token joined with its user
  and then runs an ORM `UPDATE ... RETURNING`, where main ran a `SELECT` and
  a CTE `UPDATE ... SELECT users` (the SQL per op differs; the extra
  annotation, cache-key and `bulk_persistence` calls follow from it).
  `src/sandloom_identity` is outside this change; follow-up: measure that
  query against main's.
- **Sagas (`XSgS` / `XSgL` / `XSgC`): +1.9 to +2.8 %, not reduced.** The
  SQL each op sends is identical on both builds (logged per statement), and
  the fixed tree makes only about 15 more Python calls per op than main
  (`dialect_name`, `_mark_in_line`, `_cancelling`, `connection_closed`, the
  renderer's `conflict_insert`), worth a few thousand instructions. A
  callgrind pair of `XSgS` (K = 100) puts the excess (+128 k per op) inside
  `libpython` (+107 k: allocation, attribute misses, object teardown) and
  `libc` (+18 k, `malloc`), not in any Python function: it scales with the
  heap, not with a code path. With main's own drift of 1–2 % on these rungs,
  this is not pinned down; follow-up: a heap and GC probe of the saga rung
  (object counts per op on both builds).

Data: `ladder/results/cg_infra_agnostic_2026-11-05.json` (`after_fixes`).

## E. The cachegrind stand on the kernel-boundary architecture (kb-cg-stand)

`ladder/cg/run_cg.sh` and the rungs boot the **standard** composition root
(`sandloom_standard.main`, the `sandloom-standard` package), the root a
deployment runs. The lean `sandloom.main` assigns no claim authorities and
refuses a composition that loads the token issuer with `SANDLOOM-B035`; the
rungs therefore import `sandloom_standard.main` (and the uvicorn stands target
`sandloom_standard.main:app`). What the stand sets, and why:

| what | where | why |
|---|---|---|
| `sandloom_standard.main` mounted (`packages/sandloom-standard/src` at `/w/standard`, on `PYTHONPATH`) and `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main` | `cg/run_cg.sh` | the standard root is not under `src/`; the variable names the same root for the kernel's own entry points |
| `SANDLOOM_SERVICE_NAME` (default `ladder`) | `cg/run_cg.sh` | on a shared bus (the stand's `streams`) the kernel's durable revocation applier needs a service name for its consumer group; without it the app refuses to start. Override from the caller's environment |
| `log_notifier_in_prod` in `SANDLOOM_ACCEPTED_RISKS` | `cg/run_cg.sh` default, `benchsuite.rpcstand.server_env` | identity and tenancy require a notifier with the `email` feature. In prod the `log` driver advertises none unless this risk is accepted (`SANDLOOM-B037`); the stand sends no mail. A rung that sets `ENVIRONMENT=dev` (the platform rungs) needs nothing |
| the consumer's generated contract copy | `ladder/hot_paths.py` (`HCd`) | `sandloom.shared.contracts.tenancy_membership_v1` no longer exists; the rung reads `tenancy.membership.v1` through `sandloom_billing._generated.contracts.tenancy_membership_v1` |
| the 2FA secret encrypted without a bound app | `ladder/write_login.py seed` | `sandloom_identity` settings are read from the application's bound settings; a seed run has no application, so it builds the same `CryptoUtils(key=[TWOFA_KEY], info=...)` directly |

The image is unchanged (`cg/Dockerfile`); only mounts and environment moved.
Seed once per database, from the repository root, with the root checkout's
environment (`uv sync` installs `sandloom-standard` in dev mode):

```bash
export DATABASE_URL=... CACHE_URL=... BROKER_URL=...        # localhost URLs are fine
PYTHONPATH=benchmarks:benchmarks/ladder uv run python -m benchsuite.setup_db
PYTHONPATH=benchmarks:benchmarks/ladder uv run python -m benchsuite.platform seed
PYTHONPATH=benchmarks:benchmarks/ladder uv run python benchmarks/ladder/write_login.py seed
CG_K=200 benchmarks/ladder/cg/run_cg.sh R0.rpc WLlf HTr HCs HCd Pme HW7
```

Rungs by the question they answer (the M-ir row of the migration plan):

| question | rungs |
|---|---|
| request | `WLlf` (login), `HTr` (refresh), `Pme`, `Porgs`, `Pmem` |
| mint | `HTr` (access-token mint with claims), `WLlf` (login mint); the claims rungs of PR #118 when merged |
| contract call | `R0.rpc` (RPC, in-process transport), `HCs` / `HCd` (local contract read) |
| authz on the request path | `L6S5` (JWT + revocation + TenantScope + RLS + `Subscribed`), `HW7` (class-F route: strict revocation + durable idempotency), `Pme` (strict GET) |
| write / command | `WLc` (credits spend), `HW7` |

Measured on `arch/kernel-boundary` `c7658771` (aarch64 image `sandloom-ladder-cg:3.13`,
K = 50, 2026-11-09), to show the stand runs; `WLc` K = 20:

| rung | Ir/op |
|---|---:|
| `R0.rpc` | 2,145,104 |
| `WLlf` | 13,903,566 |
| `HTr` | 9,556,585 |
| `HCs` | 1,380,116 |
| `HCd` | 3,107,239 |
| `Pme` | 2,982,154 |
| `WLc` | 9,382,989 |

**Not yet running on this architecture: the S5 / `benchsuite` rungs that mint
their token by hand** (`L6S5*`, `HW7`, `HSbud`, the `mix` / `server_bench`
loads, which sign a bare HS256 JWT carrying `subscription_status`,
`tenant_ids`). The sealed mint (ADR-0014 decision 5) keeps a protected claim
only from the authority of the composition's map and the stand's only module is
`benchsuite.bench`, which assigns none: `Subscribed` answers 402. They need a
stand whose claims come from a real authority (a root with its own
`claim_authorities` and a contributor in the bench module, tokens through
`collect_claims` + `create_access_token`); the claims-tier rungs (`K*`,
`ladder/claims_rungs.py`, PR #118) are that kind of stand and cover the
request-path authorization and the mint without a database.
