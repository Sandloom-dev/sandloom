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

Round 2 (section 6, the owner's decisions P2, P6, P7): the architecture rules
run once per `sandloom test`; the `test_dev_command` repository is built once per
file; fixed sleeps in seven test files became waits for the event; three more tests
that failed under load were fixed.

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
| P3 | One engine per xdist worker: run fixtures and tests in a session-wide event loop (`asyncio_default_fixture_loop_scope` / `asyncio_default_test_loop_scope = "session"`), keep the pool, reset only the rows; drop the two pool-release autouse fixtures | ~75 ms of CPU per database test, ~2500 such tests: ~3 min of test time, ~15–20 s of wall time | a harness change in `sandloom.core.testing`; tests then share one loop per worker, which may expose tests that leave tasks running. Worth its own task, measured |
| P4 | Delete `test_random_unit_sequences_never_execute_a_second_writer` (finding 4) and take it out of `guarantees.yml` | 12 DB tests (~5–10 s of test time) | a ledger entry changes; the law stays as the enforcing test |
| P5 | A smaller Hypothesis profile for the fast loop only (`sandloom test` without `--full`: e.g. 25 examples; `--full` and CI keep 150–400) | the laws take ~120 s of test time per run (`test_saga_laws` 30–65 s, `test_projection_laws` 35 s, `test_authz_algebra_laws` 23 s) | fewer examples in the fast loop |
| P6 | Module-scoped engines and tables for the fixtures of finding 6, with a per-test `DELETE` | ~30–60 s of test time | fixture changes in kernel tests, each checked for what it isolates |
| P7 | Replace the fixed sleeps of finding 5 by events or `pg_locks` polls | ~45 s of test time (~5 s wall) | test changes, each checked for what it proves |

Not proposed: skipping the packaging check or the split-conformance matrix in
`--full`. After 3.1 they run beside the other files and add no wall time of
their own.

**The owner's decisions (2026-11-05):** do P2, P6 and P7, and fix the
load-flaky RPC tests of finding 1 where their waits are timing-bound; P3 runs in
its own session; not P1, P4 or P5. Round 2 below.

## 6. Round 2: P2, P6, P7 and the load-flaky RPC tests

Every pair below was measured back to back on the same load (it was 20–60 all
evening, so only the pairs compare), on my own test PostgreSQL and Redis.

### 6.1 P2: the architecture rules run once per `sandloom test`

The `sandloom check` step runs every rule over the manifest in-process. It now
also writes what it found (rule, file, detail, severity of each violation) to a
file in a temporary directory of the run (`check.main(record=...)`); the
architecture tests step gets that file's path in its environment
(`SANDLOOM_TEST_ARCH_RECORD`, a default of that step only).
`test_framework_manifest_passes_every_rule` reads the violations back
(`check.recorded_violations`, which refuses a record of another manifest or an
unreadable one) and makes the same two assertions on them: no error, and no
warning but `untagged-sensitive`. Without a record (CI's architecture job, plain
`pytest`) it runs the rules itself, as before. No configuration is read from the
process environment (the ledger's ratchet on raw environment reads stays at 0
for `run_tests.py`).

Proof it still bites: with a record holding one error, or one warning of
another rule, the test fails; with an `untagged-sensitive` warning it passes.
`test_the_architecture_rules_run_once_per_sandloom_test` runs a fake `sandloom
check` step and a real subprocess step through `_run_recording_the_rules`: the
subprocess finds the file the first step wrote.

| architecture stage (`pytest -n auto tests/architecture`), load ~45–52 | without the record | with it |
|---|---:|---:|
| pair 1 | 62.9 s | 48.0 s |
| pair 2 | 40.4 s | 40.0 s |
| `test_framework_manifest_passes_every_rule` | 22.7–34.4 s | ~0 s (reads the file) |

The stage now ends with `test_docs_generated` (15–25 s at this load), so how much
P2 saves in wall time depends on which of the two was the stage's last test.

### 6.2 P6: tables once per file instead of per test

Measured first, because the fixtures' own DDL turned out to be small:

| file | tests | setup (sum) | what the setup is |
|---|---:|---:|---|
| `test_units_and_admission.py` | 39 | 12.1 s | profiled: the harness `db_engine` 8.8 s, the file's `kcore` (its `create_all` and `DELETE`) 1.1 s of 19.6 s |
| `test_dev/test_dev_command.py` | 72 | 10.3 s | `git init`, `add`, `commit` per test |
| `test_tenant_rls_paths.py` / `_bypass.py` / `test_tenant_rls.py` | 32 / 23 / 15 | 7.9 / 7.1 / 3.1 s | new engines and connections per test; `CREATE ROLE` takes 7 ms, `CREATE TABLE` 5 ms, the drops < 4 ms |
| outbox fixtures (`test_outbox_relay_batch.py` and others) | 10–15 each | 1.6–3.2 s each | `DROP` / `CREATE` of one table ~10 ms; the rest is connecting |

Done: `test_dev_command.py` builds its git repository once per module and copies
it for each test (a normal `.git` holds no absolute path, so every test still
gets its own fresh repository): **setup 9.5–9.8 s → 2.3–3.0 s**, 72 passed.

Not done, with the reason: in the other files the per-test DDL is a few
milliseconds and the setup time is the harness engine and the new connections
per test, which a module-scoped table does not remove (every test has its own
event loop, so an engine cannot be shared). That cost is P3's. The RLS table must
also stay per test: one test (`test_tenant_rls.py`, line 285) disables RLS on it.

### 6.3 P7: fixed sleeps became waits for the event

| test | the wait was | now | measured (same load, before → after) |
|---|---|---|---:|
| `test_dead_pooled_connection.py::test_fault_matrix` (`killed_during_first_statement`, `killed_during_commit`, 3 kinds) | 0.4 s / 0.5 s before killing the backend | until `pg_stat_activity` shows that backend running the statement (`_wait_running`) | 4.7–5.2 s → 2.8–3.2 s |
| `test_idempotency_pending_renewal.py::test_a_dead_requests_reservation_still_self_heals` | 1.8 s past a 1 s TTL | until the key is gone | 1.81 s → 1.06 s |
| `test_beat_lease.py::test_two_leases_on_real_redis` | 3.2 s past a 3 s TTL | until the key is gone (still ~3 s: the TTL) | saves ~0.2 s |
| `src/sandloom_identity/tests/api/test_admin_in_transaction.py` (3 tests) | a lock held 0.5 s | until a transaction is blocked by the holder (`pg_blocking_pids`, asked through the session holding the lock) | file 2.1–2.3 s → 0.7–1.0 s |
| `src/sandloom_tenancy/tests/test_actor_row_lock.py` (2 tests) | the same | the same | file 3.4 s → 0.5 s |
| `src/sandloom_billing/tests/test_credit_reversal_invariants.py` (`_concurrently` barrier) | 0.5 s | until N transactions of the test database wait on a lock (the second caller queues behind the first, so `pg_blocking_pids` names the first waiter, not the holder) | file 2.9 s → 1.3 s |
| `src/sandloom_billing/tests/test_refunds_disputes.py` (refund vs completion) | 0.5 s | until the completion waits on the lock | file 4.9 s → 3.6 s |

Each still fails when the behaviour breaks: a backend that never runs the
statement or a racer that never blocks makes the wait give up (after 10–30 s)
with an `AssertionError`; a key that never expires (renewal not stopped) ends the
Redis polls after ~6 s and the next assertion fails, as the fixed sleep's version
did.

One trap found on the way: the lock polls first opened a new session, and the
test pool has two connections, both held by the transactions under test; the
poll then waited 30 s for a connection (one failure in 6 runs). The polls now ask
through the session that holds the lock (`SELECT pg_stat_clear_snapshot()` first:
`pg_stat_activity` is a snapshot per transaction). After that: 9 runs, no failure.

Left as they were, with the reason:

- `tests/perf_budgets/test_rls_merged_begin.py::test_cancellation_leaves_nothing_on_the_pooled_connection`:
  a poll for the running statement did not see the `select` point's
  `pg_sleep` statement in 2 of 6 runs, and for the `commit` point
  `pg_stat_activity` showed the app role's **INSERT** active for the trigger's
  0.5 s and never a `COMMIT`. So the `commit` case most likely cancels during the
  INSERT, not during COMMIT. Worth a look by whoever owns that test; the file is
  unchanged.
- `test_refunds_disputes.py`, the purge test (line ~779): the lock is held inside
  the completion task's own session, and the pool has no third connection to poll
  from.
- `test_idempotency_pending_renewal.py::test_a_running_request_keeps_its_reservation_past_the_pending_ttl`:
  waiting past the TTL is the point of the test (the TTL is at its 1 s floor).

### 6.4 The load-flaky RPC tests

| test | why it failed under load | change | cost |
|---|---|---|---:|
| `test_rpc_transports.py::test_slow_peer_times_out_and_discards_the_connection` | the call's overall deadline is 3 × the attempt timeout plus the retry delay (0.4 s here, `proxy.py`); two 0.1 s attempts left ~0.1 s for connecting and discovery, which ran out, and the deadline cancelled the retry (`{"timeout": 1, "cancelled": 1}`) | attempt timeout 0.5 s (0.5 s of slack); same assertions | ~+0.8 s per transport |
| `test_transport_redis.py::test_live_lease_renewal_prevents_another_replica_reclaim` | the holder renews every `min_idle_ms / 3` (20 ms); a loop paused past three renewals let the other replica reclaim | `min_idle_ms` 500, the wait 1.2 s (2.4 × the limit, as 150 ms was 2.5 × 60 ms) | ~+1 s |
| `test_rpc_keyed_binding.py::test_a_forged_response_without_the_pair_key_is_rejected` | a 2 s attempt timeout ran out while the connection was set up (59 s at load 30); the test bounds no time | 10 s budget (the peer answers at once) | none |

### 6.5 Net effect

P7 takes about 10 s of test time off and 6.4 adds about 2.6 s back: ~7 s of
test time, under 1 s of wall time with 10 workers. P6 (one file): ~7 s of test
time. P2: one 11–34 s test out of the architecture stage. The larger lever left
is P3 (its own session). More important than the seconds: the tests of seven
files now wait for what they mean to wait for, and three tests that failed on a
busy machine have room.

### 6.6 Review and the green run

An independent review (a fresh agent, given the diff) found no correctness bug.
Changed after it: the architecture step's record path is set for that step
unconditionally (`Step.run_env`), so an exported or inherited
`SANDLOOM_TEST_ARCH_RECORD` cannot name another run's file (the unit test now
exports a stale path and still finds the run's own); the single-racer lock
polls count only transactions blocked by the holder (`pg_blocking_pids`); a
leftover comment; wording here and in the CHANGELOG.

`sandloom test --full` on the round-2 code before those review fixes: green,
5695 passed, 590 s at load 30–51. The review fixes were checked by running the
changed files (the four lock-test files three times, 52 passed each time; the
`run_tests` CLI tests).

## 7. How to measure again

- Per-stage times: the `sandloom test` summary.
- A per-test timeline: a 10-line pytest plugin that appends
  `{worker, nodeid, start, end}` per test to a JSON-lines file (load it with
  `-p` and `PYTHONPATH`); per worker it shows the tail, per file the heavy files.
- Compare on the same seed (`--seed`), back to back, and note the load average
  (`uptime`) before and after.
