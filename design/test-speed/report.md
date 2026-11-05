# `sandloom test --full`: where the time goes, what changed, what is left

Branch `claude/test-speed` (from `origin/main` 10be493), tm task `test-speed`.
Measured 2026-11-05 on the shared Mac (10 cores). Other sessions ran at the
same time, so the load average was 7–80 during these runs. Wall-clock numbers
from different loads are not comparable: every before/after pair below was
measured back to back at a similar load, with the same test-order seed (12345).

## 1. Summary

`uv run sandloom test --full --seed 12345`, whole gate:

| run | load average | total | full suite | architecture | typing | result |
|---|---:|---:|---:|---:|---:|---|
| `origin/main`, first run (cold caches) | ~30 | 533 s | 413 s | 60 s | 25 s | 2 failures (load, Redis index) |
| `origin/main` (BASE) | 16–23 | 461 s | 362 s | 48 s | 17 s | 1 failure (load) |
| **this branch (NEW)** | **27–42** | **389 s** | **250 s** | 70 s | 30 s | **green** |
| `origin/main` again (BASE2) | 42–80 | 918 s | 685 s | 122 s | 48 s | 1 failure (load) |

The machine got busier through the afternoon, so no two gate runs saw the same
load. NEW ran at a higher load than BASE and still took 72 s less overall and
112 s less in the full suite; its architecture and typing stages are inflated
by that load (alone at load 7–8 they take 22.7 s and 9.4 s). The cleaner
evidence is the suite-only pairs in 3.1 (355 s → 204 s / 177 s) and the
stand-alone times in 3.2 and 3.3. On an idle machine expect the full suite to
last about as long as its total test time divided by the workers (~125–175 s
here), and `--full` about 4 minutes (an estimate from the stage times
measured alone, not a measured run).

What made the difference, largest first:

1. **One test decided how long the full suite took** (section 2.1). pytest-xdist
   handed out the files largest first, so the one-test packaging check always
   started last, after every other file. Seeded runs now hand out the files in
   their own order, with the `slow` files first (3.1).
2. **The packaging check ran 40+ independent subprocesses one after another**:
   131 s → 29 s alone (3.2).
3. **The docs generator check ran 10 independent subprocesses one after
   another**: 15.1 s → 6.6 s alone, the architecture stage shrinks with it (3.3).
4. **Six tests fail when the machine is busy** (timing assumptions): three are
   fixed here, three are listed (3.5; 4, finding 1). One more failed on every run
   against the documented test Redis: fixed (3.5).

## 2. Baseline and where the time went

`uv run sandloom test --full --seed 12345` on `origin/main`, first run
(load average ~30, cold caches): **533 s**.

| stage | time | share |
|---|---:|---:|
| full suite (5701 tests, 10 xdist workers) | 412.6 s | 77% |
| architecture tests (1411 tests) | 59.9 s | 11% |
| typing tests (mypy + pyright on the proofs) | 25.1 s | 5% |
| `sandloom check` | 9.0 s | |
| mypy | 8.6 s | |
| migration drift | 5.2 s | |
| generated files | 4.6 s | |
| public API baseline | 3.2 s | |
| lint-imports, ruff check, ruff format, schema & deploy, contract and route snapshots | < 1 s each | |

The run used 756 s of CPU in 533 s of wall time with 10 workers: it mostly
waited.

### 2.1 One test decided the length of the full suite

A per-test timeline (start and end of every test on every worker) showed nine
workers done at about 150 s and the tenth running one test,
`tests/integration/test_package_distributions.py`, alone until the end: 731 s
in that run, 85–113 s in others. It always started last, at ~130–150 s.

The cause is pytest-xdist 3.8: `--loadscope-reorder` is on by default, and with
`--dist loadfile` it hands out the files **by test count, largest first**. A
one-test file goes out last, whatever the seeded order says. The suite lasted
"everything else" plus "the packaging test", instead of the longer of the two.
The same default also threw the seeded file order away: the shuffle plugin's
order only broke ties between files of equal size.

### 2.2 The packaging test itself

`tools/packaging/check_package_distributions.py` alone (load ~14): 131 s.

| phase | time |
|---|---:|
| 10 `uv build`s (5 packages × sdist-wheel and direct wheel), license checks | 8 s |
| import layering | 5 s |
| 27 × `sandloom <command> --help`, one after another (a cold interpreter each) | 60 s |
| `sandloom docs` ×2, inspection smoke, `sandloom new` ×5, app factory, RPC smoke | 38 s |
| `sandloom[standard]` install + boot | 9 s |
| `sandloom[admin]` install + boot | 11 s |

None of these depends on another except "install before use".

### 2.3 The architecture stage

Its two longest tests:

- `test_docs_generated.py` (25 s at load 30, 15 s at load 11): two
  `sandloom new` and eight `sandloom add` subprocesses one after another, each
  in its own scratch app.
- `test_archcheck_library.py::test_framework_manifest_passes_every_rule`
  (23 s at load 30, 11.6 s at load 7): `run_checks()` over the framework
  manifest. The `sandloom check` stage runs the same `run_checks()` over the same
  manifest (9 s). Proposal P2.

### 2.4 Stages that repeat work

- `sandloom check` and the architecture test above: the same rule run, twice.
- `generated files` checks the framework modules' route and contract
  snapshots; `route snapshots` and `contract snapshots` check them again (for
  every composed module). Both later steps take 0.0 s (the app is already
  built in the process): nothing to win.
- The architecture tests are not in the full suite (`testpaths` leaves
  `tests/architecture` out): they run once, as their own xdist session, which
  costs a second worker start-up and a second tail. Proposal P1.

### 2.5 Smaller items

- **Startup.** A first run in a fresh worktree spent 134 s before the first test
  (bytecode compilation on 10 workers). Later runs start in seconds. Only fresh
  checkouts and CI pay it.
- **Typing stage.** 9.4 s at load 8 (mypy 5.5 s, pyright 2.2 s); the 25 s of
  the baseline was load. Running its two tests in parallel would save ~2 s:
  not done.

## 3. Changes

### 3.1 Seeded runs hand out the slow files first (`sandloom.core.testing.shuffle`)

With a seed (every `sandloom test` run, CI's `--sandloom-shuffle`, and now the
pre-push hook, 3.4):

- the plugin turns xdist's `--loadscope-reorder` off, so the workers take the
  files in the seeded order;
- files holding a test marked `slow` come first (in their seeded order), then
  the rest (in theirs). The `--sandloom-shard` deal follows the same order, so
  the slow files are spread over the shards first.

The same tests run, the seed still replays the order, and the tests inside a
file are still shuffled. Two new tests in `test_testing_shuffle.py` pin it; both
fail on `origin/main`:

- `test_the_files_with_a_slow_test_come_first` (a function-level and a
  module-level `slow` marker; nothing moves without a seed);
- `test_xdist_runs_the_files_in_the_seeded_order`: a real `-n 1 --dist loadfile`
  run logs the order the files ran in; it must equal the seeded collection
  order, slow file first (on `origin/main` the 12-test file ran first and the
  slow one-test file last).

The full suite alone, same seed, back to back:

| run | wall time | note |
|---|---:|---|
| A: `origin/main` | 355 s (247 s in pytest) | fresh worktree: ~107 s of the gap is bytecode compilation; the packaging test started at 129 s |
| B: this branch | 204 s | load 15; the packaging test started at 0 s |
| C: this branch, again | 177 s | load 21, all passed |

In B and C all ten workers end within 25 s of each other. The suite now lasts
about "all test time / 10 workers".

**The fast suite** (`sandloom test`, `-m "not slow"`) has no slow files, so
there the change only replaces xdist's largest-first order by the seeded one.
Measured back to back:

| run | load average | wall time | slowest worker ends | `test_testing_shuffle.py` |
|---|---:|---:|---:|---:|
| `origin/main` | 22 | 239 s | 163 s | 71 s |
| this branch, before the fix below | 20 | 299 s | 239 s | 152 s |

After the fix below the pair could not be measured again at a comparable load
(the load reached 60–80 for the rest of the afternoon); the file itself was
measured: 39 s for 9 tests on this branch, 52 s for 7 on `origin/main`, back to
back at load 28.

The first pair showed this branch slower (239 s → 299 s). The timeline named
the cause: `test_testing_shuffle.py` (the last file to end in both runs) went
from 71 s to 152 s, because the new tests added eight `pytest` subprocesses to
a file that already ran twenty one after another. Its independent subprocesses
now run side by side (`_collect_all`): the file takes 39 s for 9 tests, against
52 s for 7 on `origin/main` at the same moment (load 28).

### 3.2 The packaging check runs its independent parts side by side

`tools/packaging/check_package_distributions.py` (the CI `packages` job and
`tests/integration/test_package_distributions.py` run it):

- the five packages build side by side (a package's own two builds stay in
  order; the build hook writes only temporary directories);
- the three clean environments (lean, standard, admin) are checked side by side;
- in the lean environment, the 27 `--help` calls run in a pool (one per CPU),
  then the other lean checks (`sandloom docs`, inspection smoke,
  `sandloom version` + `new`, the four `new --with` probes, the app factory, the
  RPC smoke) side by side, each in its own directory;
- the installs pass `--compile-bytecode`, as `pip install` does; before, every
  cold CLI process compiled the same modules at once.

Every check and assertion is unchanged. A failure in any thread fails the
run (checked by passing an unknown `sandloom docs` name: exit 1 with the
`CalledProcessError`), reported in the old order. Alone, at load 12–14:
**131 s → 40 s → 29 s**. Inside the full suite, where every core is busy, the
gain is smaller (85 s → 77 s), but there it now runs beside the other files
(3.1).

### 3.3 The docs generator check runs its scratch apps side by side

`tools/docs/docs_generated.py --check`: the two `sandloom new` bases, then the
six generators (each in its own copy), side by side. Same comparison; a stale
snippet is still reported (checked by appending a line to a committed one).
Alone: **15.1 s → 6.6 s**. The architecture stage: see the summary table.

### 3.4 The pre-push hook runs in a seeded order

`tools/checks/prepush_full_tests.sh` ran pytest without a seed, so the shuffle
plugin did nothing there: a fixed order (worse at finding order-dependent tests
than `sandloom test` and CI) and xdist's largest-first hand-out (the packaging
test last). It now draws a seed, prints it
(`backend-full-tests: test order seed N`), and passes it to the parallel run and
to the serial fallback alike; a `--sandloom-shuffle=<seed>` argument replays
one (that seed is then the one used and printed).
`test_prepush_hook_runs_the_suite_in_one_printed_seeded_order` runs the script
with stand-in `docker` and `uv` that record their arguments (the parallel run
refused with exit 4, then the serial run), with and without a given seed: both
runs get the printed seed, and a given seed is the one printed. `docs/modules/testing.md` says so.

### 3.5 Tests that failed when the machine was busy

Each of the runs above had a different single failure. Fixed here (the
assertion each makes is unchanged):

- `tests/sdk/test_sdk_projection_ops.py::test_a_waiting_relational_read_reloads_the_rows_it_already_holds`:
  the writer commits v2 10 ms into a 100 ms wait; on a busy machine the commit
  took longer, so the read served v1. The wait returns as soon as the version
  arrives, so the budget only bounds a failure: 5 s (`WAIT_MS`) for the three
  reads that wait for a concurrent writer. The `wait_ms=20` reads are unchanged
  (they expect the budget to run out, or find the version stored already).
- `src/sandloom/core/tests/test_tenant_guard_cache.py::test_cached_guard_is_cheaper_than_walking`:
  a wall-clock micro-benchmark (`cached < walk / 2`) failed at load 25
  (0.227 s vs 0.192 s). Now timed on the thread's CPU clock, best of five
  rounds for each side: the same bound, without the time the thread spends
  descheduled. (That identical statements are walked once is proven by the
  walk counter in `test_identical_statements_are_walked_once`.)
- `src/sandloom/core/tests/test_reconnect_backoff.py::test_cancelled_probe_releases_admission_and_wait_is_bounded`:
  the probe sleeps out the backoff (5–10 ms) inside the gate's 30 ms
  `wait_timeout`; a late wake-up timed the probe out before it signalled, and
  the test's 1 s wait failed. Now a 1 ms backoff in a 0.5 s timeout: the second
  caller's wait is still bounded and still ends in `DatabaseUnavailable`
  (the test takes 0.5 s).
- `src/sandloom/core/tests/test_dev/test_dev_external.py::test_only_empty_redis_indexes_are_taken_on_your_own_server`
  failed on every run against the documented test Redis: it wrote to index
  860–862, and `deploy/compose/test.yml` and CI start Redis with
  `--databases 256` ("DB index is out of range"). It now reads the server's
  `databases` setting and uses the top three indexes; it passes against a
  256- and a 1024-database server.

### 3.6 Tests that could not fail, and copies

- Deleted `test_jwt_fixtures.py::test_fixture_improvements_summary` (it only
  printed a changelog) and `test_sso.py::test_redis_connection_failure_handling`
  (it printed one line on 200 and another otherwise; the same request is
  asserted by `test_get_sso_redirect_url`).
- Deleted `test_token_service.py::test_cleanup_expired_tokens_no_tokens`: an
  exact copy of `test_cleanup_expired_tokens_none_found`, assertions included.
- `test_role_service.py`: `test_create_role_duplicate_code` and
  `test_create_permission_duplicate_code` were copies of the `..._name` tests
  (with mocks, the service's single `name = … OR code = …` query cannot tell
  them apart), so nothing proved that a duplicate **code** is refused. Each now
  also asserts that the existence query is bound to the new code.

None of these is named in `guarantees.yml` or the docs.

### 3.7 Reviews and what the green run covers

Two independent reviews (a fresh agent each, given the diff, not my reasoning)
found no correctness bug. What they raised and what changed:

- The first review: the reorder test only checked the option's value, not the
  order the files ran in. It was replaced by the real `-n 1 --dist loadfile`
  run (3.1). It also noted that the fast suite gets the reorder change without
  the slow-first rule, which led to the fast-suite measurement and the fix in
  3.1, and that the pre-push hook passed no seed (3.4).
- The second review: an `assert "code" in str(query)` in the role tests proved
  nothing (`select(Role)` always lists the column): removed, the bound-parameter
  assertion stays. The hook printed its own seed even when an argument
  overrode it: the argument's seed is now used and printed, and the test covers
  both cases. Not changed: when several parallel checks of the packaging script
  fail, only the first is raised (the others' streamed output is still in the
  log).

The green `sandloom test --full` (NEW, section 1) ran on the final code except
for the second review's two fixes; those touch
`test_testing_ownership.py`, `test_role_service.py` and the hook script, and
were checked by running those files (and mypy on the changed test files).

## 4. Test-code review: findings, ranked

Found by reading the code (two read-only sweeps, every claim then checked by
hand) and by the timelines above. Done items point to section 3.

1. **Timing assumptions that fail on a busy machine.** Fixed: the four in 3.5.
   Left (each needs someone who knows what the test bounds):
   - `test_rpc_transports.py::test_slow_peer_times_out_and_discards_the_connection[aiohttp]`
     (failed in the `origin/main` gate at load 20);
   - `test_transport_redis.py::test_live_lease_renewal_prevents_another_replica_reclaim`
     (failed once at load 20);
   - `test_rpc_keyed_binding.py::test_a_forged_response_without_the_pair_key_is_rejected[impersonator-aiohttp]`
     ("overall RPC deadline exceeded" at load 30);
   - `test_rpc_response_binding.py::test_concurrency_64_with_random_faults_never_returns_another_result[3-aiohttp]`
     passed, but took 125 s at load 30, against 0.3–9 s in the other runs: its
     time depends on load more than any other test's.
2. **Per-test database setup is the largest share of the remaining suite
   time.** In `sandloom_identity/tests/api/test_users.py` (load 24) setup takes
   335 ms per test, the test itself 49 ms. Under a profiler, `db_engine`
   (`sandloom.core.testing.fixtures`) — a new engine, a schema reset
   (`_truncate_dirty`) and a dispose for every test — is ~75 ms of CPU per test.
   Root cause: pytest-asyncio runs every test in its own event loop, so no pool
   survives a test. Proposal P3.
3. **The same rule run twice** (`sandloom check` stage and
   `test_framework_manifest_passes_every_rule`). Proposal P2.
4. **A duplicated property test.**
   `tests/integration/test_units_and_admission.py::test_random_unit_sequences_never_execute_a_second_writer`
   (12 seeded sequences of 6 steps; its docstring says Hypothesis would replace
   it) has the same body as `tests/laws/test_write_admission_laws.py::test_no_second_writer_statement_ever_executes`,
   which draws 1–8 steps of the same four kinds over 150 examples and asserts
   more (the refused lazy unit never wrote; committed rows come from a unit
   that wrote). Not deleted: `guarantees.yml` names it as an enforcing test of
   G-MSTORE-03 and -04. Proposal P4.
5. **Fixed sleeps and waits** (~45–50 s of test time per run; with 10 workers
   ~5 s of wall time). The largest:
   - `test_dead_pooled_connection.py::test_fault_matrix`: `sleep(0.4/0.5)` plus
     `pg_sleep(2)` / `pg_sleep(1.5)`, ~16 s;
   - `tests/perf_budgets/test_rls_merged_begin.py`: four 1 s cancellation waits
     plus `pg_sleep`, 4–8 s;
   - `test_idempotency_pending_renewal.py`: two `sleep(1.8)` past a 1 s TTL;
   - `test_beat_lease.py`: 3.2 s on a real Redis lease;
   - lock-holding tests in identity and tenancy (`_HOLD_S = 0.5`) and billing
     race tests (`sleep(0.5)`): a lock seen in `pg_locks` or an `asyncio.Event`
     would end the wait at once.
   Proposal P7.
6. **Per-test DDL where a module-scoped table would do.** Function-scoped
   fixtures that build an engine and `DROP` / `CREATE` a table for every test:
   `test_post_commit_dispatcher.py` (21 tests), `test_outbox_relay_batch.py` (15),
   `test_operational_metrics.py` (15), `test_fused_effect_rows.py` (12),
   `test_uow_outbox.py` (10), `test_tenant_isolation.py` (20),
   `test_contract_read_scope.py` (17), the RLS fixtures of `test_tenant_rls.py`,
   `test_tenant_rls_paths.py` and `test_tenant_rls_bypass.py` (53 tests; each
   also creates a login role), `tests/integration/test_units_and_admission.py`
   (18) and `test_route_runtime.py` (14). `test_dev/test_dev_command.py` runs
   `git init`, `add` and `commit` for each of 34 tests. Proposal P6.
7. **Overlapping tests** (not copies; worth a look when touched):
   `test_tenant_rls.py` / `test_tenant_rls_paths.py` (savepoint re-sync),
   `test_credits.py` / `test_credit_reversal_invariants.py` (concurrent spend).
8. **Measured and found cheap** (no change): identity's autouse Redis flush
   (10 ms per test in the mock-only role-service tests); the root conftest's
   `_release_default_engine_pool` running next to the SDK's
   `_sandloom_release_default_pools` (the second dispose finds empty pools).

## 5. Left for the owner

Each would save time but removes a check from a run, changes the gate's
visible steps, edits the guarantee ledger, or proves less. Not done.

| | proposal | saves (estimate) | what it costs |
|---|---|---:|---|
| P1 | Run `tests/architecture` inside the suite's xdist session instead of as its own stage | ~10–15 s per `sandloom test` (one worker start-up and one tail fewer) | the stage "architecture tests" disappears from the summary and from the docs that describe the steps (`docs/contributing/setup.md`, `index.md` and others); a failure shows under "full suite" |
| P2 | Run the architecture rules once in `sandloom test`: skip `test_framework_manifest_passes_every_rule` there when the `sandloom check` stage ran, and move its extra assertion (warnings only `untagged-sensitive`) into that stage | ~11 s off the architecture stage's longest test | the test does not run inside `sandloom test` (CI's architecture job still runs it) |
| P3 | One engine per xdist worker: run fixtures and tests in a session-wide event loop (`asyncio_default_fixture_loop_scope` / `asyncio_default_test_loop_scope = "session"`), keep the pool, reset only the rows; drop the two pool-release autouse fixtures | ~75 ms of CPU per database test, ~2500 such tests: ~3 min of test time, ~15–20 s of wall time | a harness change in `sandloom.core.testing`; tests then share one loop per worker, which may expose tests that leave tasks running. Worth its own task, measured. **Done in section 7**: CPU down, wall time unchanged at this load; it found a production bug |
| P4 | Delete `test_random_unit_sequences_never_execute_a_second_writer` (finding 4) and take it out of `guarantees.yml` | 12 DB tests (~5–10 s of test time) | a ledger entry changes; the law stays as the enforcing test |
| P5 | A smaller Hypothesis profile for the fast loop only (`sandloom test` without `--full`: e.g. 25 examples; `--full` and CI keep 150–400) | the laws take ~120 s of test time per run (`test_saga_laws` 30–65 s, `test_projection_laws` 35 s, `test_authz_algebra_laws` 23 s) | fewer examples in the fast loop |
| P6 | Module-scoped engines and tables for the fixtures of finding 6, with a per-test `DELETE` | ~30–60 s of test time | fixture changes in kernel tests, each checked for what it isolates |
| P7 | Replace the fixed sleeps of finding 5 by events or `pg_locks` polls | ~45 s of test time (~5 s wall) | test changes, each checked for what it proves |

Not proposed: skipping the packaging check or the split-conformance matrix in
`--full`. After 3.1 they run beside the other files and add no wall time of
their own.

## 6. How to measure again

- Per-stage times: the `sandloom test` summary.
- A per-test timeline: a 10-line pytest plugin that appends
  `{worker, nodeid, start, end}` per test to a JSON-lines file (load it with
  `-p` and `PYTHONPATH`); per worker it shows the tail, per file the heavy files.
- Compare on the same seed (`--seed`), back to back, and note the load average
  (`uptime`) before and after.

## 7. Addendum: one event loop per worker (proposal P3)

Branch `claude/elastic-mcnulty-3d8a91` (on top of this branch), tm task
`test-session-loop`. Measured 2026-11-05 on the same shared Mac, with my own
PostgreSQL 16 and Redis containers; load average 20–55 throughout.

### 7.1 What changed

- `pyproject.toml` sets `asyncio_default_fixture_loop_scope` and
  `asyncio_default_test_loop_scope` to `"session"`: every async test and fixture
  of one process (one xdist worker) runs on one event loop.
- `db_engine` keeps one engine and pool per loop for the whole run and resets
  only the rows, with the same `reset_schema` as before
  (`sandloom.core.testing.database.LoopEngines`; the autouse session fixture
  `_sandloom_loop_engines` names the session loop and disposes the engines at
  the end). `db_engine`'s signature is unchanged (the public API baseline). With pytest-asyncio's default (a loop per test, as
  in an application that does not set the two options) it builds and disposes an
  engine per test, as before.
- A pooled connection now serves many tests. The first time it is checked out
  in a new test it is put back to how a new connection starts: `RESET ALL`
  (settings, `SET ROLE`, `search_path`), session advisory locks released,
  `UNLISTEN *`, cursors closed, temporary tables dropped, and the prepared
  statement cache emptied (a table another test recreated makes its plans
  stale). One round trip per connection per test. A connection that died
  between tests is replaced by the pool. The `default` engines and the app's
  database providers, whose pools are now kept too, get the same reset
  (`reset_kept_pools`).
- The two per-test pool disposals (`conftest.py` `_release_default_engine_pool`,
  `_sandloom_release_default_pools`) now skip async tests on the session loop.
  They still run after a sync test, which may have opened connections on a loop
  of its own (`asyncio.run`).
- Two things a per-test loop did for free, now done by the harness after every
  test on the session loop:
  - the post-commit dispatcher (one per loop: its task, pending marks, broker
    cool-down) is drained and dropped, so the next test gets a new one
    (`end_post_commit_dispatch`);
  - tasks the test started and left running are cancelled, as closing its loop
    did (`_sandloom_cancel_leftover_tasks`). It snapshots the tasks after every
    higher-scoped fixture is set up (pytest sets them up first), so tasks
    module and session fixtures start in their setup survive; a task such a
    fixture starts lazily inside a test is cancelled with the test's, as a
    per-test loop did.

### 7.2 What broke, and why

The first full run on the session loop: 70 failed, 5624 passed.

| failures | cause | fix |
|---:|---|---|
| 61 (billing: operation intents, refunds and disputes, reconcile, checkout, portal) | **A production bug.** The intent insert names the partial unique index `uq_billing_intent_live_request` as its `ON CONFLICT` arbiter with the predicate `status IN ($14, $15)` as bound parameters. PostgreSQL plans a prepared statement with the real values five times, then may switch to a generic plan, where a predicate made of parameters can no longer be matched to the index: "there is no unique or exclusion constraint matching the ON CONFLICT specification". Reproduced in `psql` (`PREPARE` + seven `EXECUTE`s: the 6th and 7th fail). A per-test engine never ran the statement six times on one connection; a production pool does, so every checkout and portal request on a connection that had served five would fail. | `sandloom_billing/services/operation_intents.py` takes the index's own predicate (literal SQL). New `test_intent_generic_plan.py`: eight inserts on one connection; fails before the fix, passes after. |
| 9 (`test_post_commit_dispatcher.py`, `test_uow_outbox.py`, `test_pipelined_dispatch.py`; 15 when the three files run alone) | The post-commit dispatcher is one per event loop. With a loop per test every test got a new one; on the session loop a test inherited the previous test's broker cool-down ("unavailable", events left to the relay) and pending marks. | Drained and dropped after every test (7.1). |

Tasks left running after their test (found with a plugin that lists the tasks
a test leaves pending on the loop): a delayed audience check of the streams bus
(19 tests, streams-bus tests that never close their bus), the idempotency
reservation keeper (16 tests) and RPC peer handlers (6). None failed a test in
these runs, but the keeper was an order dependency waiting to happen: a keeper
left by an earlier test sleeps its 60 s interval, so
`test_idempotency_pending_renewal.py` (which patches the interval to 0.2 s)
would get no renewal and lose its key. They are now cancelled at the end of
their test (7.1); after that the plugin found none.

The new `test_testing_loop_engines.py` checks the reset of 7.1: a test leaves a
`search_path`, a custom setting, an advisory lock, a temporary table, a
`LISTEN` and a cached plan of a table another connection then recreates; the
next test's checkout of the same backend finds none of them. With the session
reset switched off it fails (the leaked `search_path` even makes the next
schema reset create the tables in `pg_catalog`); with the cache clear switched
off it fails on the stale plan.

### 7.3 Numbers

Full suite, `pytest -n 10 --dist loadfile --sandloom-shuffle=12345`, the
unchanged branch head (in its own worktree) and this branch alternately, back
to back. A 10-line plugin recorded each test's setup, call and teardown, wall
time and process CPU time.

| run | load at start | pytest wall | user CPU (all workers) | setup CPU, all tests | `test_users.py` setup, median wall / CPU | result |
|---|---:|---:|---:|---:|---:|---|
| base 1 | 39 | 385 s | 640 s | 58 s | 522 / 47 ms | green |
| new 1 | 32 | 387 s | 592 s | 48 s | 927 / 34 ms | 2 timing failures (below) |
| base 2 | 34 | 580 s | 635 s | 64 s | 1358 / 53 ms | green |
| new 2 | 32 | 409 s | 587 s | 47 s | 668 / 33 ms | green |
| base 3 | 14 | 184 s | 569 s | 54 s | 245 / 44 ms | green |
| new 3 | 21 | 220 s | 607 s | 46 s | 269 / 29 ms | 2 timing failures (below) |

On the database server (the third pair, PostgreSQL's own CPU time from its
cgroup and `log_connections`): connections opened 3155 → 1769 (−44%),
PostgreSQL CPU 86 s → 70 s (−19%).

`test_users.py` alone, both loop modes of this branch back to back, two rounds
each (load 30–55): setup median 702 → 544 ms wall, 52 → 32 ms CPU.

What the numbers say:

- **Setup CPU goes down, every time.** Setup CPU of the whole suite −8 to −17 s
  (−15 to −27%); `test_users.py` setup CPU −13 to −20 ms per test (about −35%);
  connections and server CPU as above. User CPU of the workers fell in two pairs
  out of three (−48 s twice; +38 s in the third, whose call phase took longer,
  with its two failures).
- **Wall time: no measurable change.** Two runs of the same code differ by up to
  50% at this load (base: 184 s, 385 s, 580 s), and no file was slower in all
  three pairs. The saving the report estimated for P3 (15–20 s of wall time,
  from ~75 ms of CPU per database test) is smaller than this noise, and the
  estimate itself was high: without a profiler the engine build and dispose
  cost ~15 ms of CPU per database test, not 75. What remains of a database
  test's setup is the row reset (one probe over every table, the `DELETE`s, the
  sequence rewind: several round trips) and the client fixtures; at 10–35 ms per
  round trip to a Docker PostgreSQL on this machine, round trips dominate.

### 7.4 Isolation and what is left

- Rows: reset between tests exactly as before (`reset_schema` is unchanged).
- Connection state, prepared plans, leftover tasks, the post-commit dispatcher:
  reset as in 7.1, each with a test or a full run that failed without it.
- Not reset: timers (`loop.call_later` handles) a test leaves behind, e.g.
  `rpc_transport.close_later`'s 60 s pool close. A per-test loop dropped them;
  on the session loop they fire later, in another test. None failed a test
  here; cancelling them would need a hook into the loop's timer list.
- Two tests failed on this branch under load and passed alone in both loop
  modes (five times each) and in a serial replay of the files their worker ran
  first. Both are on the report's list of timing failures:
  - `test_rpc_transports.py::test_slow_peer_times_out_and_discards_the_connection[aiohttp]`
    (2 of 3 runs here, 0 of 3 on the base; it also failed once on `origin/main`
    at load 20, 4 finding 1). Its overall RPC deadline is about 0.3 s for two
    0.1 s attempts and a retry delay; when the loop wakes up late the deadline
    cancels the second attempt, and the connection closes as `cancelled`
    instead of `timeout`. A busier loop may make that likelier; the test needs
    an owner to say what its deadline should bound.
  - `test_revocation_freshness.py::test_budget_fast_path_is_one_block_read`:
    a test bug, fixed. It built its claims twice from `time.time()`, so a
    second boundary between the two calls gave two tokens and two reads.
- Applications made by `sandloom new` keep a loop per test (their template does
  not set the two options), so they keep the old per-test engine. Setting them
  there is a separate change (docs and template).
