# idk-cache-contract: the cache holds the operations of a module (2026-11-15)

Infra-drivers design (spec section 3): `cache` is contract major 2. `incr_window` becomes
`windowed_counter`; `incr_many`, `set_nx_get`, `set_max`, `set_subject_state` and
`raw_client` leave the cache.

## Changed entries

- C-INFRA-09 (wording): the cache is operation-based (get/set/add/delete/incr,
  get_many/set_many, `windowed_counter`, `cas` compare_and_set/compare_and_delete); the
  fused `incr_window`, `incr_many`, `set_nx_get` and `set_max` are gone.
- G-BLOCK-06 (tests): the two tests of the cache's portable subject-state writer
  (`test_portable_order`, `test_portable_writer_refuses_without_cas`) leave with the
  writer; the order is pinned by `test_value_domain` and the `atomic_store` tests.
