# Ledger delta: the Redis half of the test harness moves to sandloom-redis (idk-pkg-redis-harness2)

The Redis isolation, ownership and flush code of the test harness is `sandloom_redis.testing`, registered as the `redis` family of the entry-point group `sandloom.testing`; `flush_owned_redis` moved with it. The kernel keeps the database half and the family hooks.

| Id | Disposition |
| --- | --- |
| G-OPS-07 | reworded: the refusal of a test database or Redis index another owner stamped is split between `sandloom.core.testing.ownership` / `resources` (the database) and `sandloom_redis.testing` (the Redis index); `flush_owned_redis` is imported from `sandloom_redis.testing`. Enforced by `test_testing_ownership.py` and `test_testing_resources.py` in the kernel and in `packages/sandloom-redis/tests`. |
| C-TEST-02 | repointed: the per-worker Redis index tests are `packages/sandloom-redis/tests/test_testing_workers.py` (the worker's database is still tested in the kernel's `test_testing_workers.py`). |
