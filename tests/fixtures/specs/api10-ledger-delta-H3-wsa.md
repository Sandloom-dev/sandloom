# Public API 1.0: ledger delta of hand-off H3 (WS-A), applied

WS-A integrated H3 and applied this delta to `backend/guarantees.yml`
itself (the other workstreams had finished). It is the record
`src/tests/architecture/test_ledger_migration.py` reads: every id the wave
changed must be assigned by spec §9.2–§9.4 or by one of these delta files.
Node ids are relative to `backend/` when they start with `src/`, else to the
repository root.

ADR-0009 = `Docs/adr/0009-route-policy-and-session-freshness.md`;
ADR-0010 = `Docs/adr/0010-public-api-1.0.md`.

---

## 1. New entries (issued)

| id | what it guarantees | tests |
| --- | --- | --- |
| G-ROUTE-05 | Observed effects are informational in `routes.snapshot.json`: the route runtime hands each request's risk effects to the harness observer; the `aja.sdk.testing` plugin records them per module when `AJA_TEST_OBSERVE_EFFECTS` is set (`aja test` outside `--ci`); `aja routes snapshot --write` attaches them; the check ignores them. | `src/aja/core/tests/test_observed_effects.py` (3 tests) |
| G-UNIT-03 | Write admission covers the cursor boundary (`exec_driver_sql`, also under `unscoped()`). | `src/tests/integration/test_units_and_admission.py::test_a_second_writing_unit_is_refused_before_the_cursor`, `::test_classification_of_core_constructs` |
| G-MSTORE-05 | AJA-B010: a write store without `audit` under a static risk, a durable guard on a `default` without `http_idempotency`, a database hosting request units without `write_admission` are refused at boot. | `src/tests/integration/test_write_store.py::test_a_write_store_without_a_default_feature_is_refused_at_boot`, `::test_a_database_hosting_request_units_needs_write_admission` |
| G-MSTORE-06 | AJA-B011: a writable session on a store without `transactions` in a write route is refused unless it is a declared secondary. | `src/tests/integration/test_write_store.py::test_a_non_transactional_store_in_a_write_route_is_refused` |
| G-MSTORE-07 | Consistency tokens: signed, expiring, at most 16 entries, ignored when invalid; a write route answers with the token of the rows its events produce; a read waits at most `wait_ms`, then serves with `AJA-Projection-Stale` or reads the source. Never an error. | `src/aja/core/tests/test_projection_kernel.py` (4 token tests), `src/tests/sdk/test_sdk_projection_ops.py` (4 token tests) |
| G-MSTORE-08 | Projection convergence (full-state, one owner per column, deltas only relational under the inbox, refused on a store). | `src/tests/laws/test_projection_laws.py` (4), `src/tests/sdk/test_sdk_projections.py::test_full_state_converges_under_shuffles_and_duplicates`, `src/tests/sdk/test_sdk_projection_ops.py` (2) |
| G-MSTORE-09 | Tombstones beat late older upserts; the purge floor, the rebuild guard, the watermark raised first and never lowered; older events dead-lettered on apply. | `src/aja/core/tests/test_projection_kernel.py` (4), `src/aja/core/tests/test_projection_watermarks_db.py` (2), `src/tests/sdk/test_sdk_projection_ops.py` (4), the two tombstone laws |
| G-MSTORE-10 | A store projection is tenant-native (foreign rows refused, reads per tenant). | `src/tests/sdk/test_sdk_projection_ops.py::test_a_store_row_of_another_tenant_is_refused_and_reads_are_per_tenant` |
| G-BILLING-21 | The billing API account is explicit (no single-organization inference). | `src/aja/billing/tests/test_billing_api.py::TestOrgContext::test_single_org_implies_org_context`, `::test_multiple_orgs_without_org_id_422` |
| C-DECL-05 | `db()` is the running unit's session everywhere, `contract_session()` in a contract method (also under `bind_contract_session`). | `src/tests/integration/test_units_and_admission.py::test_db_in_a_block_is_that_units_session`, `src/aja/core/tests/test_db_in_contracts.py::test_db_in_a_bound_contract_is_the_bound_session` |
| C-INFRA-12 | The relational driver contract of the unit machinery (`LazyUnitOfWorkContract`, `has_pending_changes`, `write_admission`). | `src/aja/core/tests/test_relational_driver_contract.py` (3), `src/tests/integration/test_write_store.py::test_a_database_hosting_request_units_needs_write_admission` |
| C-PROJ-07 | `Projection(store=...)` on a `conditional_write` store; the in-memory `memory` document driver. | `src/tests/sdk/test_sdk_projection_ops.py` (2), `src/aja/core/tests/test_projection_kernel.py::test_the_memory_driver_advertises_and_passes_the_kit` |
| C-PROJ-08 | Rebuild operations (watermark-marked, `rebuild_all`, `rebuild_entity`, `rebuild_from=(contract, method)`, blue/green in place and reported). | `src/aja/core/tests/test_projections.py::test_rebuild_cli_does_not_start_consumers_or_relay`, `src/tests/sdk/test_sdk_projection_ops.py` (2) |
| C-MSTORE-01 | Lattice laws (closure, meet semilattice). | `src/aja/core/tests/test_guarantee_lattice.py` (3) |
| C-MSTORE-02 | Monotonicity (the meet never strengthens). | `src/aja/core/tests/test_guarantee_lattice.py::test_the_meet_is_a_semilattice_and_never_strengthens` |
| C-MSTORE-03 | Labels derived from the mechanism, one scenario per row (cache fallback and expired windows included). | `src/aja/core/tests/test_guarantee_lattice.py::test_each_row_of_the_label_table`, `::test_a_label_is_shown_only_when_it_deviates` |
| C-MSTORE-04 | The `conditional_write` conformance kit. | `src/aja/core/tests/test_projection_kernel.py` (3) |

| C-TEST-11 | Under `client`, a `DbSession` route and a `db()` route share the test session identically (the DX eval p3 finding). | `src/tests/integration/test_client_session_parity.py::test_a_dbsession_route_and_a_db_route_share_the_test_session` |
| C-TEST-12 | The per-test pool release covers every database provider the app built (the "another operation is in progress" fast-suite failures). | `src/aja/core/tests/test_release_pools.py::test_a_providers_own_engine_is_released_for_the_next_loop` |

The multistore design's G-MSTORE-05..13 allocation (2026-10-25) was a
proposal; the ids above are the issued ones. Not issued: a blue/green
rebuild with an alias swap (no 1.0 store has one) and "a poison event marks
its aggregate stale" (not built).

## 2. Reworded entries (each the same or stronger)

| id | change | tests added |
| --- | --- | --- |
| G-UNIT-01 | the closed-unit refusal is a `ClosedKernelSession`, independent of `DB_SESSION_OWNER_CHECKS` | — |
| C-UNIT-02 | states that the best-effort detection runs with `DB_SESSION_OWNER_CHECKS` (the test default; off in production by the performance gate) and that `db()` from a non-owner task is always refused | — |
| G-UNIT-02, G-BILLING-19 | the intent window opens atomically with the no-unit check and is re-verified after the drain | `src/aja/core/tests/test_intent_window_race.py` (2) |
| G-IDEM-17 | an ordinary write through `db()` is mode S like a `DbSession` write | `src/tests/integration/test_automatic_idempotency.py::test_a_lazy_db_write_gets_the_durable_status_replay` |
| G-IDEM-18 | also a request unit whose database keeps no durable request log | — |
| G-SENS-02, G-SENS-04 | a remote money command marks the caller's request like a local one | `src/aja/core/tests/test_command_risk_parity.py::test_a_money_command_marks_the_callers_request_locally_and_remotely` |
| G-SAGA-11 | resolve-before-compensate also after a crash that rolled back the attempt count (durable started marker) | `src/tests/laws/test_saga_laws.py::test_a_deadline_after_a_crash_resolves_the_step_before_compensating`, `::test_steps_are_idempotent_and_status_only_moves_forward` |
| G-DECL-03 | saga contract requirements merge into `consumes` by name | `src/aja/core/tests/test_saga_requirement_merge.py` (3) |

Applied earlier by the integrator (e5a2981c) and recorded here for the
migration test: G-TENANT-12 (gap closed), G-ROUTE-04 (`authz`),
G-CONTRACT-16 (bigint), G-MSTORE-03/04 (the write-admission law).

## 3. Ids changed earlier in the wave, assigned here

| id | disposition |
| --- | --- |
| C-AUTH-09 | new at H1 (WS-A): `current_service` is a service authenticator (ADR-0009 "Defaults every route gets") |
| G-CONFIG-03 | reworded at H1: `AUTH_REVOCATION_STRICT_PATH_PREFIXES` joins `config_tiers.REMOVED` (spec §9.3 "Removed outright"); stronger |

## 4. ADR sections

ADR-0010 has no `planned` section left; `planned_adrs` is gone.

* "6. Guarantees as a lattice, labels derived from mechanisms" ->
  C-MSTORE-01..04, G-MSTORE-07..10, C-PROJ-07, C-PROJ-08.
* "Amendments to earlier ADRs" -> C-MSTORE-03 (ADR-0005), G-SDK-02
  (ADR-0007), G-REVOKE-22, G-REVOKE-18, G-REVOKE-24 (ADR-0009).
* "4." adds C-DECL-05; "5." adds G-MSTORE-05, G-MSTORE-06, G-UNIT-03,
  C-INFRA-12; "7." adds G-BILLING-21; ADR-0009 "The route snapshot" adds
  G-ROUTE-05.

## 5. Removed entries

None.

## 6. Error-code catalogue

AJA-B010 and AJA-B011 are live (`StoreCapabilityError`). AJA-C021 is live.

## 7. Performance gate (spec §10.4)

`benchmarks/ladder/RESULTS.md`, section "Public API 1.0 — performance gate,
H3". In-process additions: within +1 % on `L6W4c`, `L6W6`, `L6WM` and
`L6S5` (freeze mode); +1.10 % to +1.53 % on `L6W1`–`L6W5` (guarantee
enforcement, attributed per function there): the owner decides.
