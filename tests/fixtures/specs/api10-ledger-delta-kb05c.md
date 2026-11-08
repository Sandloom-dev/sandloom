# Kernel tests without batteries: the last test node ids moved (KB-05c, 2026-11-08)

The kernel-without-batteries job's pending list is empty: the kernel tests
that still needed a bundled module moved to `tests/integration/with_batteries`,
assertions unchanged. Only the node ids in each entry's `tests:` list change;
no statement, source or status changes. Two entries gain a test that runs
where no bundled module is installed.

- G-TENANT-17, G-MODULE-02, G-MODULE-03, G-MODULE-13, C-MODULE-13:
  `test_framework_manifest_passes_every_rule`,
  `test_framework_imports_catch_from_sandloom_import_and_dynamic_aliases` and
  `test_test_imports_whitelist_runs_in_sandloom_check`
  (from `tests/architecture/test_archcheck_library.py`).
- G-AUTH-05, G-AUTH-19, G-BILLING-18, G-CONFIG-07, G-RISK-07: the module
  refusal tests of `test_boot_configuration_checks.py`.
- G-MIGRATE-07, G-MIGRATE-08, C-MIGRATE-06: `test_module_extraction.py`, moved whole.
- G-OPS-16, C-SPLIT-06, C-DEPLOY-02, C-DEPLOY-08, C-DEPLOY-09: the bundled-module
  tests of `test_deploy_profiles.py`. C-DEPLOY-08 also lists the binding case
  of `test_the_split_invariants_report_what_breaks_them`, which keeps its
  other cases in the kernel suite.
- G-OPS-22, C-DEPLOY-06, C-DEPLOY-07, C-DEPLOY-08, C-DEPLOY-09: the tests of
  `test_scaffold_deploy.py` that deploy identity or tenancy.
- G-SDK-01: `test_sdk_import_is_side_effect_free` (from
  `tests/architecture/test_sdk_import_isolation.py`); the entry also lists
  `test_the_public_sdk_import_is_side_effect_free`, the same audit over the
  public `sandloom.*` modules alone.
- C-AUTH-03: `test_identity_declares_itself_the_token_issuer` (from `test_asymmetric_jwt.py`).
- C-SPLIT-03, C-MODULE-06: `test_module_subset_boots_alone` and
  `test_full_monolith_set_loads` (from `test_module_subset.py`).
- C-MODULE-12: `test_configuration_names_installed_modules_by_entry_point_name`
  (from `test_module_names.py`).
