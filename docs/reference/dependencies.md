# Dependency stack — versions and why

The framework's runtime stack is chosen by **measurement**, not by "latest".
Each pin or cap below records the number that justified it, so raising it is a
deliberate re-measurement rather than a drive-by `uv lock --upgrade`.

The original performance review remains in the private project history. The
reproducible measurement commands and fixtures are in the
[benchmark guide](https://github.com/Sandloom-dev/sandloom/tree/main/benchmarks).
Numbers below describe their recorded environments; they do not replace
measurements of a new release or deployment.

## Security floor after the emSign CA incident (2026-10-20)

The framework requires `certifi>=2026.10.19` for outbound HTTPS trust bundles.
Repository tooling, generated CI and Docker builds use `uv 0.13.4` or newer;
the production base is `python:3.12.16-slim-trixie`.
These are security updates, independent of the performance measurements below.

Existing environments also need `pip>=26.2.2`: upgrading the system trust store
alone does not replace pip's bundled certificates. Rebuild container images
with `--pull --no-cache`. The Python security team provides the bootstrap
command and the minimum interpreter versions in the
[official incident notice](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576).

**How it was measured.** CPU instructions per operation under `cachegrind`
(Linux container, Python 3.11, `PYTHONHASHSEED=0`): `(Ir(N) - Ir(0)) / N`, so
app start-up cancels out. HTTP numbers drive the full platform app (all
middlewares, ~80 routes, the route index) as raw ASGI for N = 1000 requests;
Redis and ORM numbers run against the real test Redis / PostgreSQL. Wall-clock
was not used (shared, heavily loaded machine). Run-to-run noise is about ±1 %
at N = 1000 and ±3 % at N = 100.

## Web stack: FastAPI 0.136.x, Starlette 1.6.x, pydantic 2.14

| instr / request (N = 1000)                         | before (FastAPI 0.116.1, Starlette 0.47.2, pydantic 2.11.7, SQLAlchemy 2.0.42) | after (0.136.3, 1.6.0, 2.14.0, 2.1.4) | after, GC frozen (before → after) |
|----------------------------------------------------|---------:|---------:|------------------:|
| `GET /health/simple`                               |   718 k  |   774 k (+8 %)  |   715 k → 725 k (+1 %) |
| 404 (full route scan)                              |   589 k  |   649 k (+10 %) |   586 k → 602 k (+3 %) |
| claims-only authz route (`require(Role & Perm)`)   | 1 262 k  | 1 144 k (−9 %)  | 1 260 k → 1 099 k (−13 %) |
| `list[Item]` response, 50 items                    | 3 382 k  | 2 270 k (−33 %) | 3 384 k → 2 223 k (−34 %) |

The "GC frozen" column calls `gc.collect(); gc.freeze()` after warm-up: the
trivial-route increase is almost entirely SQLAlchemy 2.1's larger import graph
making every gen-2 collection dearer (swapping only SQLAlchemy back to 2.0.54
removes it, and it disappears with the GC frozen), not per-request code.

- **FastAPI is capped `<0.137`.** 0.137 began including routers lazily
  (`_IncludedRouter`): per-request routing cost grows ~2.4× on Sandloom's app
  (`/health/simple` 0.80 M → 1.94 M; a 404 0.64 M → 3.06 M; 0.140 / 0.143 are
  worse still) and `app.router.routes` no longer lists the flattened routes the
  route index and module introspection read. Among 0.130–0.136 the differences
  are ≤ 2 % (0.133.1 ≈ 1 % cheaper than 0.136.3, inside noise), so the newest
  pre-lazy release is taken. 0.130+ serialises responses through
  pydantic-core's Rust JSON path and no longer dumps-and-revalidates a returned
  model instance — the list response is −32 % on its own.
- **Starlette ≥ 1.3.1** is a security floor (GHSA-82w8-qh3p-5jfq: form limits
  ignored; GHSA-86qp-5c8j-p5mr: Host header; GHSA-wqp7-x3pw-xc5r, …), which
  rules out FastAPI 0.130–0.132 (they require Starlette < 1.0). **The lock
  holds Starlette < 1.7** (`[tool.uv] constraint-dependencies`, lock-only — the
  published metadata keeps just the floor): 1.7.0 costs +6 % on a trivial route
  (0.724 M → 0.770 M with the GC frozen; 1.3.1, 1.5.1 and 1.6.0 are flat) for
  features Sandloom does not use (experimental OpenTelemetry middleware,
  `scope["route"]`). 1.6.0 includes the 1.5.1 `Range` hardening.
- **pydantic 2.14.1 / pydantic-core 2.50.1**, pydantic-settings 2.16.1 (2.16
  needs python-dotenv ≥ 1.2.2; its alias / union / set-decoding changes do not
  touch `Settings`, which uses no aliases, set fields or type aliases).

### Re-measurement 2026-10-21: caps kept

The original measurement used an internal dependency-pinning stand (Python 3.11, cachegrind, N = 1000,
same platform app plus a bench module with a claims-only `require(Role & Perm)`
route and a 50-item `list[Item]` route; base = this lock: FastAPI 0.136.3,
Starlette 1.6.0, pydantic 2.14.1, SQLAlchemy 2.1.5). Absolute numbers are this
harness's and are not comparable with the table above; compare within a row.

| instr / request (N = 1000) | base | Starlette 1.7.0 | Starlette 1.9.0 | FastAPI 0.156.8 + Starlette 1.9.0 |
|---|---:|---:|---:|---:|
| `GET /health/simple`      |   510 k |   545 k (+7 %) |   544 k (+7 %) | 2 450 k (4.8×) |
| 404 (full route scan)     |   405 k |   433 k (+7 %) |   436 k (+8 %) | 4 797 k (11.9×) |
| claims-only authz route   | 2 043 k | 2 087 k (+2 %) | 2 089 k (+2 %) | 4 114 k (2.0×) |
| `list[Item]`, 50 items    | 1 296 k | 1 334 k (+3 %) | 1 337 k (+3 %) | 3 219 k (2.5×) |

With the GC frozen after warm-up every cell moves by < 1 %.

- **FastAPI stays `<0.137`.** 0.156 still includes routers lazily:
  `app.router.routes` lists 34 entries instead of the 115 flattened routes
  (module routes such as `/bench/items` are absent; upstream points to
  `iter_route_contexts()`), and Sandloom's route-marker pruning turns itself off
  outside the verified range (`sandloom.core.route_markers` warns), so every request
  pays 2–12× the instructions.
- **Starlette stays `<1.7` in the lock.** 1.7–1.9 cost +7 % on a trivial route
  and +2–3 % on routes that do work; no advisory affects 1.6.0. 1.8 and 1.9
  carry robustness fixes (`FileResponse` stops on client disconnect, multipart
  range loop, path delimiters escaped in `request.url`, case-insensitive
  `TrustedHostMiddleware`) for paths Sandloom does not serve. Re-evaluate when an
  advisory affects 1.6.x.

### Behaviour change that came with FastAPI ≥ 0.118: UoW scope

FastAPI now runs a yield dependency's exit code **after** the response has been
sent unless the dependency is declared with `scope="function"`. For the request
Unit of Work that meant a client could act on a `200` before the write — and
its on-commit hooks such as token revocation — had committed (the split
conformance suite caught it: a promoted member's next request still saw the
old role; a password change did not yet revoke old tokens). Every framework UoW
dependency (`DbSession`, `Database(name)`, the authz / identity / tenancy
guards) is declared with `scope="function"`, pinned by
`test_uow_commits_before_response.py`. A hand-written `Depends(get_db)` must do
the same — FastAPI caches dependencies per scope, so a mixed declaration opens
a second UoW.

### "Double validation" of returned models

On FastAPI ≤ 0.129 a returned pydantic instance was dumped to a dict and then
re-validated against `response_model`. From 0.130 the instance passes through
(`revalidate_instances="never"`), so the pattern
`return [ItemOut.model_validate(row) for row in rows]` is validated once. The
remaining difference to returning the ORM rows directly (FastAPI validating
`from_attributes` itself) is ≈ 5.9 k instructions per item (50 items: 2.31 M vs
2.02 M) — Python call overhead, not a second validation. Endpoints keep the
explicit `model_validate`: returning ORM objects would put ORM types into the
API layer's signatures, which the ORM-leakage architecture rule and mypy
strict forbid.

## SQLAlchemy 2.1, asyncpg 0.32, psycopg2 for migrations

| instr / ORM operation (fresh session + transaction, N = 200) | 2.0.42 + asyncpg 0.30 | 2.0.54 + asyncpg 0.32 | **2.1.4 + asyncpg 0.32** |
|---|---:|---:|---:|
| `select … where id = :x` → `scalar_one()` | 1 691 k | 1 716 k | **1 674 k** |
| 20-row list → `scalars().all()`           | 2 783 k | 2 826 k | **2 714 k** |
| insert + flush                            | 1 882 k | 1 884 k | **1 927 k** |
| load + modify + flush                     | 2 596 k | 2 624 k | **2 592 k** |
| `text("select 1")`                        | 1 154 k | 1 186 k | **1 202 k** |

2.1 is within ±4 % of 2.0 per query (reads slightly cheaper, inserts / `text()`
slightly dearer — 2.1 autoflushes before `text()` too) and the full suite is
green, so it is adopted. Consequences:

- `requires-python` is `>=3.11` (2.1's floor).
- greenlet is no longer installed implicitly: the dependency is
  `sqlalchemy[asyncio]`.
- **Every PostgreSQL URL names its driver.** 2.1 maps a bare `postgresql://` to
  psycopg 3. The app uses `postgresql+asyncpg://`; migrations use
  `postgresql+psycopg2://`, derived by
  `sandloom.core.migrations.urls.to_sync_url` (which also rewrites asyncpg's
  `ssl=` query option to libpq's `sslmode=`, since psycopg2 rejects `ssl=`).
  psycopg2-binary (2.9.13, with 3.14 wheels) stays the single sync driver —
  the test harness (`src/sandloom/core/testing/workers.py`) and the test
  helpers already use it.
- The legacy `sqlalchemy.ext.mypy` plugin is gone in 2.1 (models are typed with
  `Mapped[]`); DML `rowcount` is read from a `CursorResult` cast.

## redis-py held on 5.2.1 (hiredis 3.5.0)

| instr / command (asyncio client, hiredis) | 5.2.1 | 5.3.1 | 7.4.1 | 8.1.0 |
|---|---:|---:|---:|---:|
| GET                 | 239 k | 259 k (+9 %) | 306 k (+28 %) | 387 k (+62 %) |
| SET … EX            | 274 k | 295 k (+8 %) | 370 k (+35 %) | 451 k (+65 %) |
| INCR                | 241 k | 263 k (+9 %) | 310 k (+29 %) | 390 k (+62 %) |
| pipeline of 8 GETs  | 643 k | 665 k (+4 %) | 717 k (+11 %) | 1 236 k (+92 %) |
| XRANGE 32 entries   | 699 k | 731 k (+4 %) | 817 k (+17 %) | 903 k (+29 %) |

Re-measured on 2026-10-21 (internal Redis dependency-pinning stand, hiredis 3.5.0,
same method; absolute numbers are that harness's):

| instr / command (asyncio client, hiredis 3.5.0) | 5.2.1 | 7.4.1 | 8.1.0 |
|---|---:|---:|---:|
| GET                 | 223 k | 285 k (+28 %) | 367 k (+65 %) |
| SET … EX            | 256 k | 346 k (+35 %) | 430 k (+68 %) |
| INCR                | 225 k | 289 k (+28 %) | 371 k (+65 %) |
| pipeline of 8 GETs  | 593 k | 663 k (+12 %) | 1 156 k (+95 %) |
| XRANGE 32 entries   | 490 k | 560 k (+14 %) | 642 k (+31 %) |

The regressions are unchanged, and the hold stays.

The rule was "move to 7.4 only if needed for security/maintenance and the
regression is ≤ 10 %". No advisory affects 5.2.1 and 7.4 regresses 11–35 %, so
redis stays on **5.2.1** (RESP2). 5.3.x only adds token-based auth (plus a hard
PyJWT dependency) for +4–9 % per command. The trade-off: the 5.x line no
longer receives upstream fixes — re-evaluate when a 5.x-affecting advisory
appears or 7.x/8.x performance improves. hiredis 3.5.0 is current.

## Internal-RPC client: aiohttp 3.14 (httpx kept as fallback)

`RPC_TRANSPORT=aiohttp` is the default outbound client of internal RPC
(`sandloom.core.rpc_transport`). One real RPC (membership read, conc 64) costs
~0.36 ms total CPU on aiohttp vs ~1.25 ms on httpx 0.28 (caller 0.115 vs
~0.83 ms): httpcore's pool polls every idle connection per request and h11
parses in Python (`benchmarks/ladder/RESULTS.md`, "RPC transport A/B").
aiohttp is **capped `>=3.14.5,<3.15`**: Sandloom subclasses its `TCPConnector` /
`ResponseHandler` (pool metrics, and closing a connection that receives
unsolicited bytes while idle) and reads connector internals; widen the range
only with `sandloom/core/tests/test_rpc_transports.py` green on the new version.
httpx stays a dependency (`RPC_TRANSPORT=httpx`, test clients).

## Everything else

Taken at the latest release (security fixes noted): alembic 1.20.0, celery
5.6.3 / kombu 5.6.2, uvicorn 0.54.0 + uvicorn-worker 0.4.0, httpx 0.28.1,
orjson 3.13.0, msgpack 1.2.3 (GHSA-6v7p-g79w-8964), cryptography 50.0.2
(GHSA-g6cj-pr64-35w5, GHSA-jwv3-5hgf-82ww, …), python-multipart 0.0.32 (five
2026 advisories), prometheus-client 0.26.0, PyJWT 2.15.1 (GHSA-hxm8-2xgr-2p9m
and the 2.14/2.15 batch; decode is slower — taken for the fixes), argon2-cffi
25.1.0, authlib 1.9.1 (JOSE/OAuth validation hardening; Sandloom declares it but
imports nothing from it) with joserfc 1.7.7 (JWE AAD / compact-size limits,
non-string `jti` rejected), pillow 12.3.0, requests 2.34.2, python-dotenv 1.2.4,
anyio 4.15.1, urllib3 2.8.0, idna 3.20.

uvicorn ≥ 0.36 no longer installs an event-loop policy (it passes a loop
factory to `asyncio.run`); `sandloom.core.cli.gunicorn_conf.server_implementation`
reads the running loop instead.

## Python 3.14

`uv sync --frozen --python 3.14` installs the whole locked set from wheels
(no source builds) and the full suite passes on Python 3.14.8 (`-n 4`).
`requires-python` stays `>=3.11`.
