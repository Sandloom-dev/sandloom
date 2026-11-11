# Kernel tests without batteries: test node ids moved (KB-05, 2026-11-08)

ADR-0012's fitness test runs the kernel's suites with no module package
importable. The tests below need a bundled module, so they moved to
`tests/integration/with_batteries`, assertions unchanged. Only the node ids in
each entry's `tests:` list change; no statement, source or status changes.

- G-IDEM-14, G-GUARD-01: `test_bundled_app_routes_pass` (from `test_guard_order.py`).
- G-EVENT-11, G-OPCLASS-07, C-OPS-01: `test_admin_operations.py`, moved whole.
- G-AUTH-11, C-SPLIT-02, C-SPLIT-07: the identity-claims tests of `test_contract_runtime.py`.
- G-ROUTE-02, G-ROUTE-03: `test_bundled_public_routes_are_exactly_the_reviewed_set`
  (from `test_route_policy_auth.py`).
- G-COMPAT-01, G-COMPAT-04, C-OPS-05: the bundled-module tests of `test_contract_compat.py`.
- G-MIGRATE-03: the chain-collision tests of `test_loader_migrations_hardening.py`.
- G-OPS-10, G-MIGRATE-12, C-TEST-14: the framework-tree plan tests of `test_run_tests_cli.py`.
- G-OPS-18, C-OPS-06: the restore checks of `test_backup_cli.py`.
- G-SAGA-02, C-SAGA-01: the bundled-saga tests of `test_sagas_definition.py`.
- G-DECL-01: `test_every_bundled_event_is_in_the_closed_set`
  (from `tests/sdk/test_sdk_schema_differential.py`).
- C-EFFECT-01, C-WORKER-01: `test_worker_registers_loaded_module_tasks_and_beat_schedule`
  (from `test_worker.py`).
- C-OPS-13: `test_sandloom_check_runs_against_a_manifest_with_empty_environment`
  (from `test_static_cli_no_env.py`).
- C-MODULE-12: `tests/sdk/test_sdk_module_names.py`, moved whole.
