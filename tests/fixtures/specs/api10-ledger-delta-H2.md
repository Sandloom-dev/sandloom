# Public API 1.0: ledger delta for hand-off H2 (WS-B)

For the integrator to apply to `backend/guarantees.yml` (spec §11.1:
workstream branches never edit the ledger). Source branch:
`worktree-agent-a13992dce0de2b4a0` (on `integration/api10` at `2bb18685`).
Ids follow spec §9.4. Test paths are relative to the repository root;
`T = backend/src/tests`.

The kernel mechanisms of H1 (model E, units and `db()`, write admission,
intent mode, authorization subjects, idempotency modes) are not here; where an
entry's full behaviour needs them, the entry says what flips at H1.

## New entries

```yaml
- id: G-SDK-03
  statement: 'L1 registration equals L2: for a fixture module per L1 feature (router, producer events,
    views and consumers, a contract provider, a saga with a typed step, a projection with full-state,
    tombstone and delta groups) and for every example module, the descriptor and the recorded
    registration calls built by aja.sdk define_module equal those of a hand-written L2 register; the
    module_router routes carry exactly the per-route dependencies of the L2 form.'
  sources:
  - Docs/design/public-api-1.0-spec.md#3. Three API levels
  - Docs/adr/0010-public-api-1.0.md#2. Three API levels; level 1 is built only from level 2
  tests:
  - T/sdk/test_sdk_l1_registration.py::test_l1_equals_l2_registration
  - T/sdk/test_sdk_l1_registration.py::test_module_router_routes_equal_the_l2_router
  - T/sdk/test_sdk_l1_registration.py::test_example_modules_register_identically_through_l1
- id: G-SDK-04
  statement: 'Generated code (aja new, every aja add generator) and every example file that imports
    aja.sdk use only the 27 beginner names; an advanced name needs `# aja: advanced <reason>` on its
    import (AJA-T009).'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.2 Modules
  tests:
  - T/sdk/test_sdk_generators.py::test_the_beginner_tier_is_the_27_names
  - T/sdk/test_sdk_generators.py::test_generated_code_uses_only_the_beginner_tier
  - T/sdk/test_sdk_generators.py::test_examples_importing_the_sdk_stay_in_the_beginner_tier
  - T/sdk/test_sdk_generators.py::test_the_pin_reports_an_unmarked_advanced_name
- id: G-SDK-05
  statement: 'Every name aja.sdk._decl (L1) imports from aja.sdk._prim is exported by a public aja.sdk
    module, and every _prim name is public: each L1 dependency is an advertised L2 primitive.'
  sources:
  - Docs/design/public-api-1.0-spec.md#3. Three API levels
  tests:
  - T/architecture/test_public_api_sdk.py::test_l1_uses_only_public_l2
- id: G-DECL-01
  statement: 'An Event class is a producer when its channel prefix is the defining module (or stated),
    else a view; channels are versioned, fields are wire types, one producer per channel and module, a
    stated producer outside its module is refused (AJA-B015); a view never publishes (AJA-C025 at run
    time). A producer schema is registered, snapshotted (module.snapshot.json) and exported to the shared
    DTO package; a change on the same .vN other than an added optional field fails before anything is
    written (AJA-T005), in dev and CI.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.2 Typed events
  tests:
  - T/sdk/test_sdk_events.py::test_channel_must_be_versioned
  - T/sdk/test_sdk_events.py::test_reserved_and_non_wire_fields_are_refused
  - T/sdk/test_sdk_events.py::test_role_is_derived_from_the_channel_prefix
  - T/sdk/test_sdk_events.py::test_a_view_cannot_publish
  - T/sdk/test_sdk_discovery.py::test_producers_are_registered_and_views_are_not
  - T/sdk/test_sdk_discovery.py::test_two_producers_of_one_channel_are_refused
  - T/sdk/test_sdk_generated.py::test_only_an_optional_field_may_be_added_on_the_same_version
  - T/sdk/test_sdk_generated.py::test_an_incompatible_change_fails_before_anything_is_written
  - T/sdk/test_sdk_generated.py::test_plan_generates_the_l1_module_files
- id: G-DECL-02
  statement: '@on(E) registers through register_event_consumer with every option (name, durable,
    required, dead_letter, start_from, group, database) and binds the tenant from a `tenant_id: UUID`
    field (or a named UUID field, or none); a handler whose parameter is not E, a non-async handler, a
    duplicate name or a bad tenant field is refused (AJA-B016); deliveries decode the payload into E
    (unknown fields ignored) with EventMeta attached and never part of the payload.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.4 Consumers
  tests:
  - T/sdk/test_sdk_events.py::test_on_derives_the_tenant_binding_and_keeps_every_option
  - T/sdk/test_sdk_events.py::test_on_refuses_bad_handlers
  - T/sdk/test_sdk_events.py::test_callback_decodes_the_payload_and_attaches_meta
  - T/sdk/test_sdk_discovery.py::test_duplicate_consumer_names_are_refused
- id: G-DECL-03
  statement: 'define_module derives consumes from module-level Contract(...) references: name and
    required exact; methods only from direct `<name>.<method>(` calls on the name or a direct alias,
    any other use giving () (every method); merged with explicit consumes never weaker (required ORed,
    methods unioned or ()).'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.3 Typed contracts
  tests:
  - T/sdk/test_sdk_contracts.py::test_methods_are_derived_from_direct_calls_only
  - T/sdk/test_sdk_discovery.py::test_consumes_are_derived_and_merged_never_weaker
- id: G-DECL-04
  statement: 'Contract(REF, optional=True).client() is None only when the provider is neither local nor
    bound in the deployment (ServiceNotFound / ServiceUnavailableInDeployment); a deployed provider is
    returned even when it will fail (503 on the call).'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.3 Typed contracts
  tests:
  - T/sdk/test_sdk_contracts.py::test_optional_is_none_only_when_not_deployed
  - T/sdk/test_sdk_contracts.py::test_optional_returns_a_deployed_provider_even_if_it_will_fail
- id: G-DECL-05
  statement: 'aja check AJA-C021 refuses a function-local import in a business module (typing-only
    imports and a marked optional dependency excepted). Enforced once its code is un-planned (it reports
    the 48 bundled-module imports WS-C removes).'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.10 No function-local imports in modules
  tests:
  - T/architecture/test_api10_checks.py::test_c021_function_local_imports
  - T/architecture/test_api10_checks.py::test_every_api10_rule_has_its_appendix_a_code
- id: C-DECL-01
  statement: 'Event / on: typed construction, publish() through the outbox of the running unit (recorded
    for published()), meta before delivery raises EventMetaUnavailable.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.3 New names: exact signatures
  tests:
  - T/sdk/test_sdk_events.py::test_construction_is_typed_and_strict
  - T/sdk/test_sdk_events.py::test_meta_is_unavailable_before_delivery
  - T/sdk/test_sdk_events.py::test_publish_writes_the_outbox_row_and_is_recorded
  - T/sdk/test_sdk_events.py::test_abstract_base_shares_fields
- id: C-DECL-02
  statement: 'contract / command / Contract: a stateless provider class becomes the L2
    classmethod(contract_method(...)) form with every option (idempotency_key, idempotency_scope,
    database, planned); __init__, instance attributes, non-async or public non-method members and bases
    outside the package are refused (AJA-B018); Contract(REF) resolves when called (fakes, then the
    application resolver); typed saga steps name the command.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.3 Typed contracts
  tests:
  - T/sdk/test_sdk_contracts.py::test_provider_becomes_the_l2_form_with_every_option
  - T/sdk/test_sdk_contracts.py::test_stateful_or_non_async_providers_are_refused
  - T/sdk/test_sdk_contracts.py::test_command_needs_its_key_parameter
  - T/sdk/test_sdk_contracts.py::test_caller_resolves_when_called_through_a_fake
  - T/sdk/test_sdk_contracts.py::test_caller_uses_the_bound_resolver
  - T/sdk/test_sdk_contracts.py::test_contract_needs_a_generated_reference
  - T/sdk/test_sdk_contracts.py::test_typed_saga_step_names_the_command
  - T/sdk/test_sdk_discovery.py::test_provider_bases_must_be_inside_the_package
- id: C-DECL-03
  statement: 'module_router: derived prefix and tags, no router-level auth; per route current_context and
    TenantScope(required=...) unless public() or a service principal; a route that binds the tenant
    itself gets no second binder; public() with an authenticator is refused (AJA-B008); tenant="derive"
    is required iff the module declares a TenantScoped model.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.1 Model E and the module router
  tests:
  - T/sdk/test_sdk_router.py::test_prefix_and_tags_are_derived_from_the_module
  - T/sdk/test_sdk_router.py::test_authentication_and_tenant_are_injected_per_route
  - T/sdk/test_sdk_router.py::test_optional_binding_does_not_require_a_tenant
  - T/sdk/test_sdk_router.py::test_public_and_service_routes_get_nothing
  - T/sdk/test_sdk_router.py::test_public_with_an_authenticator_is_refused
  - T/sdk/test_sdk_router.py::test_tenant_binding_is_derived_from_tenant_scoped_models
  - T/sdk/test_sdk_router.py::test_unauthenticated_requests_are_refused_and_public_ones_served
- id: C-DECL-04
  statement: 'Projection (relational targets): key starts with tenant_id, one owning group per column, one
    version column per group, delta mappers only under the durable inbox, tombstones need their column
    (AJA-B012); full-state events converge under shuffles and duplicates, tombstones beat older upserts,
    deltas increment, views never return tombstones.'
  sources:
  - Docs/design/public-api-1.0-spec.md#6.4 Projection convergence model
  tests:
  - T/sdk/test_sdk_projections.py::test_invalid_projections_are_refused
  - T/sdk/test_sdk_projections.py::test_the_l2_form_is_one_consumer_per_channel
  - T/sdk/test_sdk_projections.py::test_full_state_converges_under_shuffles_and_duplicates
  - T/sdk/test_sdk_projections.py::test_a_tombstone_beats_a_late_older_upsert
  - T/sdk/test_sdk_projections.py::test_deltas_increment_and_versions_never_go_back
  - T/sdk/test_sdk_projections.py::test_views_skip_tombstones_and_page
  - T/sdk/test_sdk_projections.py::test_the_registered_handler_decodes_the_envelope
- id: C-SDK-01
  statement: 'aja docs <name> answers a public name''s home, tier, signature and documentation (and a
    module''s names by tier) without a kernel read; every beginner code name defined in aja.sdk documents
    an example.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.5 Zero kernel reads
  tests:
  - T/architecture/test_sdk_reference.py::test_aja_docs_answers_a_name
  - T/architecture/test_sdk_reference.py::test_every_beginner_name_documents_an_example
- id: C-SDK-02
  statement: 'Every public aja.sdk module starts with a generated reference block (beginner and advanced
    names, doc page) that is current (aja docs --write).'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.5 Zero kernel reads
  tests:
  - T/architecture/test_sdk_reference.py::test_every_public_module_carries_a_current_reference_block
  - T/architecture/test_sdk_reference.py::test_a_block_lists_the_tiers_and_the_doc_page
- id: C-GEN-01
  statement: 'aja add module writes new files inside the package only (model, schemas, services, events,
    module_router routes, a baseline migration with RLS rendered from the model, tests) and refuses an
    existing package or a bad name.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_add_module_writes_only_inside_the_package
- id: C-GEN-02
  statement: 'aja add event appends a producer class to events.py, never rewriting it, and refuses a file
    that does not parse or lacks an import.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_add_event_appends_and_refuses_an_unparsable_file
- id: C-GEN-03
  statement: 'aja add contract writes the provider and, the stated exception, the stub (rendered like aja
    test renders it) and DTO module in the shared DTO package.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_add_contract_writes_the_generated_stub
- id: C-GEN-04
  statement: 'aja add consumer writes a view from the producer schema (the tree or the exported file);
    without one it needs --field and marks the view unverified.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_add_consumer_takes_the_producer_schema_or_marks_unverified
- id: C-GEN-05
  statement: 'aja add saga writes package files only; an existing target is kept and the output written
    as *.new.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_an_existing_target_is_never_overwritten
- id: C-GEN-06
  statement: 'aja add projection writes the projection, its row model and a migration chained after the
    module chain head; a fully generated app (every generator) passes ruff, format, mypy --strict and aja
    check with the pending 1.0 checks, and its tests pass except where they reach an H1 stub.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.9 Generators
  tests:
  - T/sdk/test_sdk_generators.py::test_add_projection_chains_its_migration_after_the_head
  - T/sdk/test_sdk_generators.py::test_a_fully_generated_app_passes_the_static_gates
  - T/sdk/test_sdk_generators.py::test_a_fully_generated_app_passes_its_tests_up_to_the_h1_stubs
- id: C-GEN-07
  statement: 'aja migrate new adds enable_tenant_rls / disable_tenant_rls (system_access for
    __system_access__ models) for every TenantScoped table the autogenerated revision creates.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.6 Generated migrations with RLS
  tests:
  - T/sdk/test_sdk_generators.py::test_migrate_new_adds_row_level_security_to_new_tenant_tables
- id: C-TEST-08
  statement: 'published(E) lists the E events the code under test published with Event.publish(); a
    published() call without the testing plugin explains how to activate it.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.3 New names: exact signatures
  tests:
  - T/sdk/test_sdk_events.py::test_publish_writes_the_outbox_row_and_is_recorded
  - T/sdk/test_sdk_events.py::test_a_view_cannot_publish
- id: C-TEST-09
  statement: 'fake_contract(REF | name, impl) serves the contract to Contract(REF) for the block, nested,
    and restores it.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.3 New names: exact signatures
  tests:
  - T/sdk/test_sdk_testing_kit.py::test_fake_contract_nests_and_restores
- id: C-TEST-10
  statement: 'deliver drives the real consumer runtime (schema preflight, the durable claim, the tenant
    binding), accepts an Event instance and keeps today''s deliver_event parameters; as_user is
    make_user_headers.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.3 New names: exact signatures
  tests:
  - T/sdk/test_sdk_testing_kit.py::test_deliver_drives_the_real_consumer_once
  - T/sdk/test_sdk_testing_kit.py::test_deliver_keeps_the_channel_form
  - T/sdk/test_sdk_testing_kit.py::test_as_user_is_make_user_headers
- id: G-MODULE-19
  statement: 'An application derives its module list: aja.sdk.app.app_modules("<app>.modules") finds,
    statically, every subpackage whose __init__ defines register; aja new passes it to create_app as the
    default (AJA_MODULES selects a subset).'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.7 The application manifest and discovery
  tests:
  - backend/src/aja/core/tests/test_project.py::test_minimal_app_loads_only_its_module
  - backend/src/aja/core/tests/test_project.py::test_with_batteries_orders_modules
```

`issued:` becomes `G-SDK: 5`, `G-DECL: 5`, `C-DECL: 4` (C-DECL-05, `db()`,
is WS-A's with H1), `C-SDK: 2`, `C-GEN: 7`, `C-TEST: 10` (C-TEST-07 is WS-A's
route snapshot; if it is not issued yet, add C-TEST-08..10 with it), and
`G-MODULE: 19` (if G-MODULE-17/18 numbering allows; otherwise renumber).

**Typing laws** (spec §8.1): `T/typing/test_sdk_typing.py::test_mypy_strict_accepts_the_proofs`
and `::test_pyright_strict_accepts_the_proofs` belong under a §8.1 entry (no
§9.4 id names it; suggested: attach to C-DECL-02 and C-DECL-01, or a new
`G-SDK-06`). `src/tests/typing` is outside `testpaths` (run by `aja test
--full`, step "typing tests"); if the ledger checker only resolves collected
node ids, add `src/tests/typing` to `testpaths`.

## Rewordings

- **G-KERNEL-11**: gap closed. The surface gate covers the aja.sdk modules
  (public_api_baseline.json) and their reference blocks
  (`T/architecture/test_sdk_reference.py::test_every_public_module_carries_a_current_reference_block`);
  `aja test` runs it as step "public API baseline" (AJA-T008).
- **G-COMPAT-01**: add "for L1 modules, `aja test` compares every generated
  file with the committed one before writing (AJA-T004 contracts, T005
  events, T007 stubs, T001 a route that lost its authentication); `aja test
  --ci` only checks". Add tests
  `T/sdk/test_sdk_generated.py::test_apply_writes_in_dev_and_fails_stale_in_ci`,
  `::test_stub_compatibility_is_additive`,
  `::test_a_route_that_loses_its_authentication_is_incompatible`.
- **G-MODULE-14 / C-MODULE-04**: add the discovery tests
  `T/sdk/test_sdk_discovery.py::test_plain_routers_keep_todays_rule_and_module_routers_join`,
  `::test_a_declaration_outside_the_conventional_submodules_is_refused`,
  `::test_a_refused_module_registers_nothing`,
  `::test_events_override_skips_producer_schemas`. The plain-router rule is
  unchanged (same tests as today keep passing).
- **G-MODULE-18 / C-MODULE-01**: "`aja add module` replaces `aja-scaffold`"
  — the console scripts `aja-scaffold` / `aja-migrate` are still installed
  (deploy files and runbooks use them; WS-C removes them in the same series
  as its deploy update). `aja new` already uses `aja add` and `aja migrate`.

## Removals

None.

## ADR sections

`0010-public-api-1.0.md`: "2. Three API levels" → add G-SDK-03, G-SDK-05;
"1. One public namespace" → add G-SDK-04, C-SDK-01, C-SDK-02; "3. Declarative
module surface" → G-DECL-01..04, C-DECL-01..04 (still planned for model E's
route policy part, H1); "8. The application manifest; derived framework
lists" → G-MODULE-19 (the bundled-module derivation stays WS-C's); "9.
Business modules carry no function-local imports" → G-DECL-05 (planned until
AJA-C021 is un-planned after WS-C's cleanup).

## Codes to un-plan in `error_codes.py` (one-line changes, WS-A file)

| code | when |
| --- | --- |
| AJA-B006 (refusals: misplaced declaration) | now (already unplanned) |
| AJA-B015 `event-definition` | now: raised by `Event` and discovery |
| AJA-B016 `consumer-definition` | now: raised by `@on` and discovery |
| AJA-B018 `contract-definition` | now: raised by `@contract` / `@command` |
| AJA-B012 `projection-contract` | now for relational targets (stores: H3) |
| AJA-T005, AJA-T007 | now: `aja test` step "generated files" |
| AJA-T008 | now: `aja test` step "public API baseline" |
| AJA-T009 | now: the beginner-tier pin |
| AJA-C023, C024, C025, C026 | now (clearing `planned` activates them in `aja check`; the framework tree reports none) |
| AJA-C022 | now as a warning (it lists 11 identity/tenancy effects until WS-A's `sensitive_effect` and WS-C's tagging land) |
| AJA-C027 | with H1 (`Account` lands then; no module uses `of=` before) |
| AJA-C021 | after WS-C removes the 48 bundled-module function-local imports |
| AJA-T006 | not yet: the drift step is not implemented (see the report) |
