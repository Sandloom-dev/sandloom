# Public API 1.0: ledger delta for hand-off H2b (WS-B, after H1)

For the integrator to apply to `backend/guarantees.yml` (spec §11.1:
workstream branches never edit the ledger). It follows
`api10-ledger-delta-H2.md` (applied) and `api10-ledger-delta-H1.md` (WS-A);
ids below continue the H1 numbering where both touch a prefix. Test paths are
relative to the repository root; `T = backend/src/tests`.

## New entries

```yaml
- id: C-TEST-07
  statement: 'aja.sdk.testing.route_policy(route) reports what the framework applies to a route: auth
    (user / service / public), the public reason (deny-by-default is assertable from an app suite),
    the effect (read / write by method) and the risks resolved from their sources;
    assert_route_snapshot(app, *modules) fails unless each module''s routes equal its committed
    routes.snapshot.json, naming the fix (AJA-T001), and fails on a missing snapshot.'
  sources:
  - Docs/design/public-api-1.0-spec.md#2.3 New names: exact signatures
  - Docs/adr/0009-route-policy-and-session-freshness.md#The route snapshot
  tests:
  - T/sdk/test_sdk_testing_kit.py::test_route_policy_reads_auth_risk_and_the_public_reason
  - T/sdk/test_sdk_testing_kit.py::test_assert_route_snapshot_pins_the_module_routes
- id: C-ROUTE-03
  statement: 'aja check untagged-sensitive (AJA-C022, warning) reports a money command without
    risk="money", a credential / session change (revocation, block, unblock) without
    @sensitive_effect, and a write route that decides an authority by hand (an authority call or a
    403 in its body, at_commit(), a membership change behind a member-level gate) without
    sensitive(risk); a route behind a framework admin gate (route, parameter or router
    dependencies) or public() is not reported. On the framework tree it reports the hand-rolled
    routes tenancy DELETE /{org_id}/members/{member_user_id}, identity PUT /users/{user_id} and
    tenancy POST /invitations/accept until they declare a risk.'
  sources:
  - Docs/adr/0009-route-policy-and-session-freshness.md#Checks (deterministic; each error names its fix)
  - Docs/design/public-api-1.0-spec.md#5.1 Model E and the module router
  tests:
  - T/architecture/test_api10_checks.py::test_c022_untagged_money_and_security_effects
  - T/architecture/test_api10_checks.py::test_c022_hand_rolled_authority_routes
  - T/architecture/test_api10_checks.py::test_c022_reports_the_bundled_hand_rolled_routes
- id: G-MIGRATE-12
  statement: 'aja migrate drift (and the aja test step "migration drift", AJA-T006) applies the core
    chain and every module chain, in the upgrade-all dependency order, to a scratch database on the
    default database''s server (created and dropped by the check) and compares each chain with its
    models (each env.py compares only the module''s tables); a difference fails naming the module,
    the operations found and the fix (aja migrate new <module> "<message>"). The configured
    databases are never written.'
  sources:
  - Docs/design/public-api-1.0-spec.md#5.6 Generated migrations with RLS
  - Docs/design/public-api-1.0-spec.md#5.8 `aja test`
  tests:
  - T/sdk/test_sdk_generators.py::test_migration_drift_names_the_module_and_the_fix
  - backend/src/aja/core/tests/test_run_tests_cli.py::test_plan_runs_module_tests_then_the_fast_suite_and_the_gates
- id: G-AUTHZ-13
  statement: 'The authorization algebra is unchanged (spec §8.3), pinned by properties over random
    And / Or / Not trees of fixed-outcome, evaluation-counting atoms against an independent model of
    today''s rule: the value, the atoms evaluated and their order (cheapest tier first,
    short-circuit, no extra evaluation), fail-closed soundness (an allow holds under every
    two-valued completion of the indeterminate atoms), Not(U) = U, Or allowing on a genuine allow
    beside an indeterminate, evaluate agreeing with decide; the known order dependence
    (~(F & U) allows, ~(U & F) does not) is kept.'
  sources:
  - Docs/design/public-api-1.0-spec.md#8.3 The authorization algebra is unchanged (Q4 revised twice)
  - Docs/design/public-api-1.0-spec.md#8.2 Property-based (Hypothesis)
  tests:
  - T/laws/test_authz_algebra_laws.py::test_value_and_evaluations_equal_the_documented_rule
  - T/laws/test_authz_algebra_laws.py::test_an_allow_holds_under_every_completion_of_the_indeterminate
  - T/laws/test_authz_algebra_laws.py::test_not_keeps_an_indeterminate
  - T/laws/test_authz_algebra_laws.py::test_or_allows_on_a_genuine_allow_beside_an_indeterminate
  - T/laws/test_authz_algebra_laws.py::test_evaluate_agrees_with_decide
  - T/laws/test_authz_algebra_laws.py::test_the_known_order_dependence_is_kept
- id: G-SENS-07
  statement: 'Risk is a join: Requirement.risks() is the union over And / Or and empty under Not; a
    route''s resolved risk is the union of its sources (admin gates, @sensitive_effect dependencies,
    sensitive() markers) whatever their order or repetition, each naming its sources; the derived
    step-up is the strictest of its contributors'' (obligations are a homomorphism).'
  sources:
  - Docs/adr/0009-route-policy-and-session-freshness.md#Risk is declared at its source, once, by its owner
  - Docs/design/public-api-1.0-spec.md#8.2 Property-based (Hypothesis)
  tests:
  - T/laws/test_risk_join_laws.py::test_requirement_risks_are_a_union
  - T/laws/test_risk_join_laws.py::test_route_risk_is_the_join_of_its_sources
  - T/laws/test_risk_join_laws.py::test_obligations_are_a_homomorphism
```

`issued:` becomes `C-TEST: 10` (C-TEST-07 here; C-TEST-08..10 of the H2
delta can now issue), `C-ROUTE` as WS-A's H1 delta plus C-ROUTE-03,
`G-MIGRATE: 12`, `G-AUTHZ: 13` (G-AUTHZ-09..12 are WS-A's H1 entries; the
§9.4 "algebra-pinning probe" moved to 13 because H1 used 09..12 for the
authorization subjects), `G-SENS: 7`.

## Rewordings

- **C-GEN-06**: "and its tests pass except where they reach an H1 stub" →
  "and every one of its tests passes". The node id
  `T/sdk/test_sdk_generators.py::test_a_fully_generated_app_passes_its_tests_up_to_the_h1_stubs`
  is kept by name and now allows no failure.
- **C-MODULE-01 / G-MODULE-18** (whichever pins `aja new` + scaffold
  passing its own tests): `backend/src/aja/core/tests/test_project.py::test_generated_project_passes_its_own_tests`
  now allows no failure (the H1-stub allowance is gone).
- **G-DECL-05**: "Enforced once its code is un-planned" → "reported as a
  warning once its code is un-planned, and an error after WS-C removes the
  48 bundled-module imports (spec §5.10)"; add
  `T/architecture/test_api10_checks.py::test_the_trees_pass_the_10_checks_once_unplanned`
  (the framework tree and the four examples report no error from C021-C027,
  so un-planning breaks no gate).
- **C-DECL-02**: add "`@command(risk="money")` is passed to
  `contract_method(risk=)` (the contract snapshot records it), and
  `Contract(REF)` resolves through the active core (`aja.core.di.get_service`)
  outside the test plugin"; add
  `T/sdk/test_sdk_contracts.py::test_the_default_resolver_is_the_kernels_active_core`.
- **G-SDK-05 / G-KERNEL-11**: the H1 names are the kernel objects themselves
  (no wrapper); add
  `T/architecture/test_public_api_sdk.py::test_h1_names_are_the_kernels`,
  `::test_db_and_open_uow_are_the_kernel_units`,
  `::test_public_is_the_kernel_marker`.
- **C-DECL-03**: add "a route with a service authenticator stamped by
  `aja.sdk.web.authenticates(kind="service")` gets nothing injected"
  (`T/sdk/test_sdk_router.py::test_public_and_service_routes_get_nothing`,
  same node id).
- **G-CONTRACT-05 / G-CONTRACT-16**: add the codec properties (see the next
  section).
- **G-DECL-01**: "A producer schema is registered" → "A producer registers
  its wire schema (`E.wire_schema()`: the serialization schema, so a
  defaulted field is required, with no `additionalProperties: false`, so a
  reader accepts a newer producer's added optional field; construction stays
  strict)"; add
  `T/sdk/test_sdk_events.py::test_the_wire_schema_requires_defaults_and_accepts_added_fields`.
  This deviates from the spec §3 table (`E.model_json_schema()`); the L2 form
  is `core.register_event_schema(C, E.__name__, E.wire_schema())`. Committed
  `module.snapshot.json` / exported schemas generated before this change are
  regenerated once (the required set grows for defaulted fields).
- **G-DECL-01 / C-DECL-01 (C025)**: the static view check sees fields
  inherited from a same-file abstract base;
  add `T/architecture/test_api10_checks.py::test_c025_sees_fields_inherited_from_an_abstract_base`.
- **G-MODULE-14 / C-MODULE-04 (ownership)**: a business module named like a
  conventional submodule (`tasks`) owns its declarations, and `Saga[T](...)`
  records its constructing module; add
  `T/sdk/test_sdk_discovery.py::test_a_module_named_like_a_submodule_owns_its_declarations`,
  `::test_a_subscripted_saga_records_its_constructing_module`.
- **C-GEN-03 / G-COMPAT-01 (stubs)**: generated stubs import `ContractRef`
  from its kernel home `aja.core.descriptors.contract_refs` (no `aja.sdk`
  import, no advanced marker) and are `ruff format` clean (double-quoted
  defaults and `Literal` values); add
  `T/sdk/test_sdk_generated.py::test_a_generated_stub_is_ruff_formatted`.
- **C-TEST-03 (or the entry pinning `capture_effects`)**: it also records a
  typed `Event.publish()` made from the listed modules; add
  `T/sdk/test_sdk_events.py::test_capture_effects_records_typed_publishes`.
- **C-MODULE-01 / G-MODULE-18**: the `aja-scaffold` / `aja-migrate` console
  scripts are removed; `aja scaffold` / `aja migrate` are the commands (the
  static-CLI test now runs `aja migrate --help` unconfigured).

## Laws (spec §8.2) attached to existing entries

All under `T/laws/` (Hypothesis 6.168.5, in `testpaths`):

| entry | add tests |
| --- | --- |
| G-CONTRACT-05 (non-wire refused) | `test_wire_codec_laws.py::test_a_non_wire_annotation_is_refused_wherever_it_sits` |
| G-CONTRACT-16 (codec 1 / codec 2, local equals remote) | `test_wire_codec_laws.py::test_codec_1_round_trips_every_wire_value`, `::test_local_equals_remote`, `::test_codec_1_equals_codec_2_when_fully_typed` |
| G-MSTORE-08 (restated by §6.4) and C-DECL-04 | `test_projection_laws.py::test_full_state_converges_to_the_max_version_event_per_group`, `::test_a_tombstone_beats_any_late_older_upsert`, `::test_a_column_has_exactly_one_owner_or_the_projection_is_refused`, `::test_a_mapper_writing_a_column_its_group_does_not_own_is_refused`, `::test_a_delta_mapper_is_accepted_only_under_the_durable_inbox`, `::test_deltas_under_the_inbox_count_each_event_exactly_once` |
| G-SAGA-06 (a step never runs twice) | `test_saga_laws.py::test_saga_laws_hold_outside_the_crash_then_deadline_gap` |
| G-MSTORE-03/04 (restated, write admission; WS-A H1) | `test_write_admission_laws.py::test_no_second_writer_statement_ever_executes` |

**Not covered, said so:** the guarantee lattice (§6.2, §8.2 row 2) has no
implementation yet (it arrives with the stores, H3, WS-A); a delta mapper on
a non-relational target (needs `Projection(store=...)`, H3).

**Open findings, pinned as strict xfails (not ledger tests until fixed):**

- `test_saga_laws.py::test_steps_are_idempotent_and_status_only_moves_forward`:
  a crash after the provider committed a step (the transition rolled back,
  `attempt == 0`) followed by the saga deadline compensates around an applied
  step (shrunk: `[Crash("after", "charge", "do"), JumpPastDeadline(1)]`, and
  the same on the pivot `grant`). `_on_deadline` resolves the current step
  only when `_ambiguous()` (`attempt > 0` and a transient outcome); the crash
  erased the attempt. Kernel fix (WS-A, `aja/core/sagas/runtime.py`): on a
  deadline, always resolve the current command step (re-issue with the same
  key; the command log replays or runs it) before compensating. Then the
  xfail flips and the test joins G-SAGA-06 / G-SAGA-07.
- `test_wire_codec_laws.py::test_an_int_beyond_64_bits_does_not_cross_rpc`:
  an int beyond 64 bits passes an in-process call but cannot be packed by the
  RPC transport (msgpack), so local differs from remote. Kernel fix (WS-A,
  `aja/core/contract_wire.py`): refuse it in the codec (a `ContractWireError`
  both ways) or tag it as a string. Then it joins G-CONTRACT-16.


## Removals

- **C-TEST-04** (`assert_operation_classes`): superseded by C-TEST-07 (as
  WS-A's H1 delta states).

## ADR sections

`0010-public-api-1.0.md`: "2. Three API levels" → add the H1 identity tests
of G-SDK-05; "3. Declarative module surface" → C-DECL-01..04 lose their
"planned for model E's route policy part" note (model E is the kernel's
since H1); "10. Checks and error codes" → C-ROUTE-03, G-MIGRATE-12;
"7. Explicit authorization subjects" → G-AUTHZ-13.
`0009-route-policy-and-session-freshness.md`: "Checks" → C-ROUTE-03;
"Risk is declared at its source" → G-SENS-07; "The route snapshot" → C-TEST-07.

## Codes to un-plan in `error_codes.py` (one-line changes, WS-A file)

| code | why now |
| --- | --- |
| AJA-C021 `module-local-imports` | the rule is implemented and reports at **warning** severity (`api10_rules.SEVERITY`) until WS-C's cleanup; un-planning activates it in `aja check` without failing any tree |
| AJA-C022 `untagged-sensitive` | warning; reports the three hand-rolled routes and 8 effects until WS-A / WS-C tag them |
| AJA-C023 `multi-store-waivers` | info listing (no waiver in any tree) |
| AJA-C024 `unsafe-tenant-query` | error; no tree reports one |
| AJA-C025 `event-view-compat` | error / warning; no tree reports one |
| AJA-C026 `contract-union-hint` | info |
| AJA-C027 `account-route-mismatch` | error; `Account` landed with H1; no tree reports one |
| AJA-T006 `migration-drift` | `aja migrate drift` and the `aja test` step landed |

`test_every_check_rule_has_a_catalogue_entry` stays green either way (a
pending rule joins `RULES` exactly when its code is un-planned), and
`test_planned_codes_name_their_landing_point` keeps holding for what stays
planned (AJA-B010, B011).
