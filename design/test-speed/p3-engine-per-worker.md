# P3: one event loop per xdist worker, one test engine per worker

Proposal P3 and finding 2 of `report.md` in this folder. Branch
`claude/elastic-mcnulty-3d8a91` (on top of `claude/test-speed`), tm task
`test-session-loop`. Measured 2026-11-05 on the same shared Mac, with my own
PostgreSQL 16 and Redis containers; load average 20–55 throughout.

## 1. What changed

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

## 2. What broke, and why

The first full run on the session loop: 70 failed, 5624 passed.

| failures | cause | fix |
|---:|---|---|
| 61 (billing: operation intents, refunds and disputes, reconcile, checkout, portal) | **A production bug.** The intent insert names the partial unique index `uq_billing_intent_live_request` as its `ON CONFLICT` arbiter with the predicate `status IN ($14, $15)` as bound parameters. PostgreSQL plans a prepared statement with the real values five times, then may switch to a generic plan, where a predicate made of parameters can no longer be matched to the index: "there is no unique or exclusion constraint matching the ON CONFLICT specification". Reproduced in `psql` (`PREPARE` + seven `EXECUTE`s: the 6th and 7th fail). A per-test engine never ran the statement six times on one connection; a production pool does, so every checkout and portal request on a connection that had served five would fail. | `sandloom_billing/services/operation_intents.py` takes the index's own predicate (literal SQL). New `test_intent_generic_plan.py`: eight inserts on one connection; fails before the fix, passes after. |
| 9 (`test_post_commit_dispatcher.py`, `test_uow_outbox.py`, `test_pipelined_dispatch.py`; 15 when the three files run alone) | The post-commit dispatcher is one per event loop. With a loop per test every test got a new one; on the session loop a test inherited the previous test's broker cool-down ("unavailable", events left to the relay) and pending marks. | Drained and dropped after every test (section 1). |

Tasks left running after their test (found with a plugin that lists the tasks
a test leaves pending on the loop): a delayed audience check of the streams bus
(19 tests, streams-bus tests that never close their bus), the idempotency
reservation keeper (16 tests) and RPC peer handlers (6). None failed a test in
these runs, but the keeper was an order dependency waiting to happen: a keeper
left by an earlier test sleeps its 60 s interval, so
`test_idempotency_pending_renewal.py` (which patches the interval to 0.2 s)
would get no renewal and lose its key. They are now cancelled at the end of
their test (section 1); after that the plugin found none.

The new `test_testing_loop_engines.py` checks the reset of section 1: a test leaves a
`search_path`, a custom setting, an advisory lock, a temporary table, a
`LISTEN` and a cached plan of a table another connection then recreates; the
next test's checkout of the same backend finds none of them. With the session
reset switched off it fails (the leaked `search_path` even makes the next
schema reset create the tables in `pg_catalog`); with the cache clear switched
off it fails on the stale plan.

## 3. Numbers

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

## 4. Isolation and what is left

- Gates on the final code: `uv run sandloom test --full --seed 12345` passes
  every stage (full suite 157.5 s, load ~25). CI's `backend-tests-isolation`
  step (`pytest -p no:xdist --sandloom-shuffle=<seed> --sandloom-shard=N/2
  -m "not subprocess_probe"`: one process, so one loop, for ~2800 tests per
  shard) passes both shards with seed 4242 and again with seed 999 (2823 and
  2813 passed).
- Rows: reset between tests exactly as before (`reset_schema` is unchanged).
- Connection state, prepared plans, leftover tasks, the post-commit dispatcher:
  reset as in section 1, each with a test or a full run that failed without it.
- Not reset: timers (`loop.call_later` handles) a test leaves behind, e.g.
  `rpc_transport.close_later`'s 60 s pool close. A per-test loop dropped them;
  on the session loop they fire later, in another test. None failed a test
  here; cancelling them would need a hook into the loop's timer list.
- Two tests failed on this branch under load and passed alone in both loop
  modes (five times each) and in a serial replay of the files their worker ran
  first. Both are on `report.md`'s list of timing failures:
  - `test_rpc_transports.py::test_slow_peer_times_out_and_discards_the_connection[aiohttp]`
    (2 of 3 runs here, 0 of 3 on the base; it also failed once on `origin/main`
    at load 20, `report.md` section 4, finding 1). Its overall RPC deadline is about 0.3 s for two
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
