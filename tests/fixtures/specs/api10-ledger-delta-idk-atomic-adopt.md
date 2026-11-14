# idk-atomic-adopt: security and idempotency duties on `atomic_store` (2026-11-14)

Infra-drivers design (spec sections 4 and 12): revocation marks, blocks, RPC
nonces, the idempotency guard and enqueue dedupe leave the cache for the
`atomic_store` instances `INFRA.atomic_store.security` and
`INFRA.atomic_store.idempotency`. `SECURITY_CACHE_INSTANCE` and
`IDEMPOTENCY_CACHE_INSTANCE` are removed.

## Changed entries

- G-REVOKE-03 (wording and tests): the mark is written with the security store's
  `set_subject_state`; the `set_max` clause and the "cache with neither `set_max`
  nor `cas` is refused" clause go, with the test
  `test_revocation_refuses_a_cache_without_set_max_or_cas`.
- G-REVOKE-19 (wording): marks and blocks live in the `security` `atomic_store`
  instance, never in the `idempotency` one (not in the settings that named
  cache instances).
- G-BLOCK-02, G-BLOCK-03, G-BLOCK-04, G-BLOCK-06 (wording): "cache outage" becomes
  "security store outage"; the block swap is the store's `compare_and_set`; the
  subject-state writer is the driver's own atomic `set_subject_state` (no portable
  loop in the kernel).
- G-IDEM-01, G-IDEM-05, G-IDEM-08, G-IDEM-16, G-IDEM-18 (wording): the guard's records
  are in the `idempotency` `atomic_store` (reserved with one `add_or_get`, transitions by
  `compare_and_set` / `compare_and_delete`); an outage there is a "store" outage.
- G-CMD-09 (wording and test): the cache fallback of command dedupe lives in the
  `idempotency` instance; the test is `test_atomic_adopt.py::
  test_contract_command_cache_fallback_uses_the_idempotency_role`.
- C-TENANT-08 (wording and tests): routing of security-critical state and idempotency
  records is by the `atomic_store` instances; the tests are in `test_atomic_adopt.py`.
- G-PROFILE-01 (wording): the strict profile refuses an `INFRA.atomic_store.security`
  that is undeclared, a memory driver or on the default cache's Redis server.
- G-PROFILE-06 (wording and tests): startup attests `never_evicts`, `shared` and a
  different `isolation_identity()` from the default cache for the security (and a
  separate idempotency) instance; a `None` or raising identity fails. The test
  `test_strict_profile_checks_physical_redis_identity` (`INFO server` through the
  cache) is replaced by four tests in `test_atomic_adopt.py`.
- G-PROFILE-07 (wording and tests): the same rules for a declared
  `INFRA.atomic_store.idempotency` (declared on its own server, not memory; sharing
  the security server only warns); the tests move to `test_atomic_adopt.py`.
- G-PROFILE-08 (wording and tests): state is written through the `atomic_store`
  instance of its role; an absent `INFRA.atomic_store.security` is refused in
  production and never built from the cache's options.
- G-RPC-08 (wording): the shared nonce is an atomic `add` on the security
  `atomic_store`; an outage there fails closed in production.

Review fixes of PR #206 (idk-atomic-adoptc), wording and one test id:

- G-PROFILE-06 (wording and test): a real Redis cache reports `redis:<run_id>`; the test
  `test_atomic_adopt.py::test_strict_profile_attests_real_redis_identity` is added.
- G-PROFILE-08 (wording): the refusal of an absent or memory
  `INFRA.atomic_store.security` is for every production deployment, any
  `PRODUCTION_PROFILE`, at startup.
- G-INFRA-10 (wording): the unused-instance list names `atomic_store`, not "revocation
  and security cache".
- G-OPS-19 (wording): "security-store restore" and "security store", not the cache.
- G-CMD-09 (wording): the dedupe fallback outage is an outage of the idempotency
  `atomic_store`, not "a cache outage".
