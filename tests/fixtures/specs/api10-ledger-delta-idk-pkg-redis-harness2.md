# Ledger delta: the Redis half of the test harness moves to sandloom-redis (idk-pkg-redis-harness2)

The Redis isolation, ownership and flush code of the test harness is `sandloom_redis.testing`, registered as the `redis` family of the entry-point group `sandloom.testing`; `flush_owned_redis` moved with it. The kernel keeps the database half and the family hooks.

| Id | Disposition |
| --- | --- |
| G-OPS-07 | reworded: the refusal of a test database or Redis index another owner stamped is split between `sandloom.core.testing.ownership` / `resources` (the database) and `sandloom_redis.testing` (the Redis index); `flush_owned_redis` is imported from `sandloom_redis.testing`. Enforced by `test_testing_ownership.py` and `test_testing_resources.py` in the kernel and in `packages/sandloom-redis/tests`. |
| C-TEST-02 | repointed: the per-worker Redis index tests are `packages/sandloom-redis/tests/test_testing_workers.py` (the worker's database is still tested in the kernel's `test_testing_workers.py`). |
| C-TEST-05 | repointed: the Redis half of the resource listing and pruning (a stale index is pruned, a changed one never) is `packages/sandloom-redis/tests/test_testing_resources.py::test_scan_and_prune_a_stale_index_but_never_a_changed_one`; the database half stays in `src/sandloom/core/tests/test_testing_resources.py`. |
| C-TEST-13 | repointed: capping `-n auto` to the Redis indexes the run holds or could claim is tested by `packages/sandloom-redis/tests/test_testing_resources.py::test_auto_workers_fall_back_when_redis_indexes_are_short` and `::test_affordable_workers_count_own_and_free_indexes_without_claiming`; the serial run without pytest-xdist stays in `src/sandloom/core/tests/test_quickstart.py`. |
| core.testing.flush_owned_redis | moved: the row of `public-api-1.0-spec.md` reads `replaced` with the new home `sandloom_redis.testing.flush_owned_redis` (G-OPS-07); the kernel no longer exports it. |
