# idk-gated-tests-redis: the Redis-driver tests leave the kernel suite (2026-11-16)

Follow-up of the kernel-without-Redis step (ADR-0016, `tools/ci/no_redis.py`):
the kernel tests that exercised the `redis` drivers move to
`packages/sandloom-redis/tests`, the ones that only borrowed a Redis store run
on the kernel's in-memory store. The behaviour and the assertions are unchanged.

## Changed entries (tests only)

- G-AUDIT-01, G-BLOCK-01, G-BLOCK-02, G-BLOCK-03, G-CONFIG-03, G-REVOKE-11,
  G-REVOKE-12, G-REVOKE-21: the evidence in `test_revocation_freshness.py`
  (a real Redis shared by two processes, the server's TTLs, a refused
  connection) moved whole to `packages/sandloom-redis/tests/`.
- G-BLOCK-06, G-BLOCK-07: the three `test_redis_*` subject-state tests moved from
  `test_subject_record.py` to `packages/sandloom-redis/tests/test_redis_subject_record.py`.
- G-IDEM-05: `test_redis_cas_primitives` moved to
  `packages/sandloom-redis/tests/test_redis_atomic_cas.py`; the guard's own
  transitions in `test_idempotency_cas.py` run on the in-memory atomic store.
- G-PROFILE-06: `test_strict_profile_attests_real_redis_identity` moved to
  `packages/sandloom-redis/tests/test_atomic_adopt_redis_identity.py`.
- G-OPS-01: `test_ten_minutely_slots_create_one_lease_name_on_redis` moved to
  `packages/sandloom-redis/tests/test_redis_periodic_slots.py`.
- C-INFRA-09: `test_cache_provider_operations` moved to
  `packages/sandloom-redis/tests/test_redis_cache_operations.py`.

The `redis-drivers` lane runs all of them (`SANDLOOM_REDIS_REQUIRED=1`), with the
rest of `packages/sandloom-redis/tests`.
