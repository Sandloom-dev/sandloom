# Public API 1.0: ledger delta for hand-off H0

For the integrator to apply to `backend/guarantees.yml` (spec §11.1: workstream
branches never edit the ledger). Source branch: `worktree-agent-a7d5a1ffacc82f07d`.
Ids follow spec §9.4 (`G-SDK`). Paths are relative to the repository root.

## New entries

```yaml
- id: G-SDK-01
  statement: 'Importing a public aja.sdk module, aja.sdk.testing or a bundled module (identity, tenancy,
    billing) is side-effect free: with python -I, an empty environment and no .env it reads no environment
    variable, constructs no Settings, opens no socket or non-package file, installs no logging, signal or
    atexit handler, starts no thread and builds no engine or client; help(aja.sdk.events) works. Settings
    are lazy and every entry point forces them (enforce_configuration / load_settings), so an invalid
    environment still fails at startup.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.4 Import isolation
  - Docs/adr/0010-public-api-1.0.md#1. One public namespace, `aja.sdk`, with a beginner tier
  tests:
  - backend/src/tests/architecture/test_sdk_import_isolation.py::test_sdk_import_is_side_effect_free
  - backend/src/tests/architecture/test_sdk_import_isolation.py::test_the_audit_sees_an_entry_point_forcing_settings
  - backend/src/aja/core/tests/test_lazy_settings.py::test_import_does_not_validate_and_first_use_does
  - backend/src/aja/core/tests/test_lazy_settings.py::test_an_invalid_environment_still_fails_at_the_entry_point
  - backend/src/aja/core/tests/test_lazy_settings.py::test_loaded_settings_are_a_plain_settings_instance
- id: G-SDK-02
  statement: 'aja.sdk imports only the public kernel surface (no kernel internal, no private submodule
    of a public kernel package); the kernel (aja.core, aja.shared) never imports aja.sdk; L1
    (aja.sdk._decl) imports no kernel module; and aja.sdk is layered public modules > _decl > _prim.'
  sources:
  - Docs/design/public-api-1.0-spec.md#3. Three API levels
  - Docs/adr/0010-public-api-1.0.md#2. Three API levels; level 1 is built only from level 2
  tests:
  - backend/src/tests/architecture/test_kernel_import_contracts.py::test_kernel_import_contracts_are_kept
  import_contracts:
  - sdk-kernel-surface
  - kernel-never-imports-sdk
  - sdk-decl-l1
  - sdk-layers
```

and in `issued:` add `G-SDK: 2`.

Not yet: G-SDK-03 (L1 registration equals L2) and G-SDK-04 (beginner-tier
pin, AJA-T009) land at H2. **G-SDK-05** (every L1 dependency is a public L2
name) already has its test,
`backend/src/tests/architecture/test_public_api_sdk.py::test_l1_uses_only_public_l2`,
but the ledger checker refuses an issued-number gap, so add G-SDK-05 at H2
together with 03 and 04 (then `G-SDK: 5`). C-SDK-01/02 land at H2.

## Candidate entries without a §9.4 id (integrator decides)

These H0 deliverables have tests but no ledger id in the spec. Listed so the
integrator can attach them to an ADR-0010 section or an existing entry.

- **Migration scanner** (spec §2.1 rule 2, runs in the suite until the
  migration completes): every Appendix C name and every kernel import of the
  bundled modules and `examples/` has a public home; every home exists; no
  ADR-0009-removed name is public.
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_scanner_reports_no_unmapped_name`
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_scanner_detects_a_missing_home`
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_scanner_table_covers_appendix_c`
- **Error catalogue** (spec Appendix A, ADR-0010 Decision 10): every 1.0 code
  is reserved with its slug; a planned code names its hand-off and says so in
  `aja explain`.
  - `backend/src/aja/core/tests/test_error_codes.py::test_every_public_api_10_code_is_catalogued`
  - `backend/src/aja/core/tests/test_error_codes.py::test_planned_codes_name_their_landing_point`
  - `backend/src/aja/core/tests/test_error_codes.py::test_explain_cli_knows_planned_codes`
- **Application manifest** (spec §5.7, ADR-0010 Decision 8; frozen interface):
  the loader records one entry per loaded module, `create_app` publishes
  `app.state.app_manifest` in load order.
  - `backend/src/aja/core/tests/test_app_manifest.py::test_module_manifest_reads_the_loader_draft`
  - `backend/src/aja/core/tests/test_app_manifest.py::test_manifest_is_frozen_and_aggregates_in_load_order`
  - `backend/src/aja/core/tests/test_app_manifest.py::test_create_app_publishes_the_manifest`
- **Tiers and stubs** (spec §2.1 rule 4, §2.3):
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_every_public_module_declares_its_tiers`
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_root_reexports_the_beginner_code_names`
  - `backend/src/tests/architecture/test_public_api_sdk.py::test_new_names_are_stubs_naming_their_section`

## Rewordings

- **G-KERNEL-11**: "The enforced public surface (the six public_api modules +
  aja.shared.*)" becomes "The enforced public surface (the public_api modules
  of architecture_rules.yml, including the aja.sdk modules of Public API 1.0,
  + aja.shared.* and the test surfaces)". Tests and gap unchanged (the gap
  closes with AJA-T008 at H2).
- **G-CONFIG-06**: unchanged wording; its pinned baseline went down by one
  (`aja/core/contracts/payload_validation.py`: the `AJA_JSONSCHEMA_BACKEND`
  read is now the `Settings` ops field `jsonschema_backend`).

## Removals

None.

## ADR sections

`0010-public-api-1.0.md` stays in `planned_adrs`. When the integrator maps
sections: "1. One public namespace, `aja.sdk`, with a beginner tier" →
G-SDK-01 (and later G-SDK-04); "2. Three API levels; level 1 is built only from
level 2" → G-SDK-02, G-SDK-05 (and later G-SDK-03). Everything else stays
planned.
