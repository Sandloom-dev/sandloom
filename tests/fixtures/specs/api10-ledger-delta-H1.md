# Public API 1.0 — ledger delta of hand-off H1 (WS-A)

The guarantees ledger (`backend/guarantees.yml`) is never edited on a
workstream branch (spec §11.1). This file is WS-A's ledger delta for H1: the
integrator applies it on the integration branch at the hand-off. Every test
node id below exists on the H1 branch; ids are relative to the repository root
unless they start with `src/` (then relative to `backend/`).

How the branch kept the ledger green meanwhile: a node id the current ledger
references was **kept by name** and now asserts the stronger replacement
behaviour; the entries below say so (`kept name`). Nothing referenced by the
current ledger was deleted.

Sources use the ADR section anchors of spec §9.3 (ADR-0009) and the ADR-0010
sections (`Docs/adr/0010-public-api-1.0.md`).

ADR-0009 = `Docs/adr/0009-route-policy-and-session-freshness.md`;
ADR-0010 = `Docs/adr/0010-public-api-1.0.md`.

---

## 1. New entries

### G-ROUTE (ADR-0009 "Defaults every route gets", "The route snapshot", "Checks")

| id | statement | tests |
| --- | --- | --- |
| G-ROUTE-01 | `create_app` refuses (`RoutePolicyError`, AJA-B008) an API route that neither resolves an authenticator (`@authenticates`, user or `kind="service"`) nor declares `public(reason=...)`, one that does both, an empty reason, or two different public reasons; the fix is named. | `src/aja/core/tests/test_route_policy_auth.py::test_an_unauthenticated_untagged_route_is_refused_with_the_fix`, `::test_public_and_an_authenticator_together_are_refused`, `::test_an_empty_reason_is_refused_at_declaration`, `::test_create_app_refuses_an_unauthenticated_route`, `::test_every_platform_route_is_authenticated_or_public`; `src/tests/architecture/test_operation_classes.py::test_every_platform_route_has_one_consistent_class`, `::test_unclassified_route_fails_the_check`, `::test_two_different_classes_fail` (kept names) |
| G-ROUTE-02 | `public(reason=)` is recorded with its reason; issuers (`issues_tokens=True`) keep the live issuance check; router-level `public()` covers its routes; the bundled public routes are exactly the reviewed set. | `src/aja/core/tests/test_route_policy_auth.py::test_public_with_a_reason_passes_and_is_recorded`, `::test_router_level_public_covers_its_routes`, `::test_bundled_public_routes_are_exactly_the_reviewed_set`; `src/tests/architecture/test_operation_classes.py::test_router_level_class_covers_its_routes` (kept name) |
| G-ROUTE-03 | PSP webhooks are `public(reason=)` and verify their signature in the handler (unchanged verification). | `src/aja/core/tests/test_route_policy_auth.py::test_bundled_public_routes_are_exactly_the_reviewed_set`; existing billing webhook signature tests |
| G-ROUTE-04 | Each module's `routes.snapshot.json` (format 3) records the static resolved policy per route and method: `auth`, `effect`, `session`, `risk` with its sources, `idempotency` (mode), `audit`, `step_up_max_age_s`, `public_reason`, `issues_tokens`, `operation_intent`, `write_store`, `multi_db_write`; drift — including a weakening such as a dropped risk — fails the module's tests with the fix (AJA-T001). | `src/tests/architecture/test_operation_classes.py::test_module_routes_match_their_snapshots`, `::test_a_stale_snapshot_names_the_route_and_the_fix`, `::test_kernel_routes_carry_the_kernel_classes` (kept names); `src/tests/integration/test_automatic_idempotency.py::test_the_snapshot_records_the_mode` |
| G-ROUTE-05 | (informational observed effects) — **not delivered at H1**: no `observed_effects` field is written yet. Keep the entry planned. | — |
| C-ROUTE-01 | `no_idempotency(reason=)` opts a write out of the automatic guard; explicit `idempotency_guard(...)` keeps all its parameters. | `src/tests/integration/test_automatic_idempotency.py::test_without_a_request_unit_the_guard_is_the_fail_closed_in_flight_form`, `::test_mode_x_re_runs_authorization_and_the_handler` |
| C-ROUTE-02 | `sensitive(risk)` (escape hatch) resolves `{risk: ("sensitive()",)}`; `sensitive("security")` derives a 300 s step-up; an unknown risk is refused. | `src/tests/integration/test_sensitive_risk.py::test_sensitive_marker_and_effect_tags_resolve_with_their_source`; `src/tests/architecture/test_operation_classes.py::test_unknown_class_is_refused` (kept name) |
| C-ROUTE-03 | `aja check untagged-sensitive` (AJA-C022) — **WS-B**; not on this branch. | — |

### G-REVOKE-21..24, G-BLOCK-06..07, G-HTTP-12 (ADR-0009 "Defaults every route gets", "One subject-state record")

| id | statement | tests |
| --- | --- | --- |
| G-REVOKE-21 | Every authenticated route reads the subject-state record `aja:auth:subject:{id}` live, with one `GET`, memoised per request; a revoked or blocked token is `401 WWW-Authenticate: Bearer error="invalid_token"`, a fail-closed outage `503`. | `src/aja/core/tests/test_subject_record.py::test_the_hot_path_reads_one_key`; `src/aja/core/tests/test_revocation_freshness.py::test_strict_default_sees_revocation_on_next_request`, `::test_revocation_in_one_process_invalidates_another`, `::test_the_freshness_knobs_are_gone`; `src/tests/integration/test_platform_routes_strict.py::test_every_authenticated_platform_route_refuses_a_withdrawn_session`; `src/tests/perf_budgets/test_cache_round_trips.py::test_authenticated_get_budget` |
| G-REVOKE-22 | The commit-time re-check runs before the COMMIT of every committing unit of an armed request — automatically on user writes (a write or a queued effect commits), on a GET whose unit wrote (its `on_commit` kept), and on every unit under `at_commit()`; units that commit nothing skip it; a foreign revocation between two commits of one request refuses the second; intent routes use `release_check`. | `src/tests/integration/test_route_runtime.py::test_a_block_during_a_write_that_declares_nothing_refuses_and_rolls_back`, `::test_a_get_that_writes_is_rechecked_and_a_reading_get_is_not`, `::test_a_foreign_revocation_between_two_commits_refuses_the_second`; `src/aja/core/tests/test_kernel_review_regressions.py::test_revocation_at_commit_covers_named_db_in_either_dependency_order`, `::test_nested_http_request_has_independent_commit_authority`, `::test_reused_session_does_not_keep_an_old_requests_authority_check` |
| G-REVOKE-23 | Self-revocation is "check, then revoke": a unit revoking its own subject commits after the live check passed; a foreign revocation landing before the self-revoking call is still seen; a self-revocation inside a rolled-back savepoint contributes nothing; committed units carry the exemption to later units of the request; over RPC the provider's self-revocations come back MAC-bound (`sr`). | `src/tests/integration/test_route_runtime.py::test_a_route_revoking_its_own_subject_commits`, `::test_a_foreign_revocation_before_the_self_revoking_call_is_still_seen`, `::test_a_self_revocation_in_a_rolled_back_savepoint_contributes_nothing`; `src/aja/core/tests/test_security_review_regressions.py::test_rpc_http_boundary_isolates_and_restores_outer_commit_authority` |
| G-REVOKE-24 | A remote command under an armed re-check is propagated in the MAC-covered RPC context (`rc`: `sub`, `iat_us`) and the provider re-checks before its own COMMIT (replaces today's refusal); reads unchanged. | `src/aja/core/tests/test_security_review_regressions.py::test_guarded_request_never_sends_remote_command` (kept name; now asserts propagation), `::test_guarded_request_can_still_send_described_remote_read` |
| G-BLOCK-06 | One record: `v1:blocked:<us>` (no TTL) outranks every `v1:after:<us>`; a revocation never unblocks; the Redis writer is one Lua script, the portable writer a CAS loop that refuses without CAS. | `src/aja/core/tests/test_subject_record.py::test_value_domain`, `::test_portable_order`, `::test_portable_writer_refuses_without_cas`, `::test_a_revocation_concurrent_with_a_block_never_unblocks`, `::test_redis_subject_state_order_and_ttl`, `::test_redis_concurrent_block_and_revocations_end_blocked` |
| G-BLOCK-07 | Unblock replaces `blocked` with `after:<now+skew>` by compare-and-set after commit, never deletes; tokens issued before the block stay revoked; a rolled-back unblock keeps the block. | `src/aja/core/tests/test_subject_record.py::test_unblock_never_resurrects_pre_block_tokens`, `::test_a_rolled_back_unblock_keeps_the_block`, `::test_redis_unblock_swaps_and_never_deletes` |
| G-HTTP-12 | User routes answer `Cache-Control: no-store` unless the route sets its own. | `src/tests/integration/test_route_runtime.py::test_user_routes_are_no_store_unless_they_set_cache_control` |

### G-IDEM-17..18 (ADR-0009 "Defaults every route gets" item 5; ADR-0010 §4)

| id | statement | tests |
| --- | --- | --- |
| G-IDEM-17 | The automatic guard (user writes with `Idempotency-Key`, no explicit guard / `no_idempotency` / intent) always replays status only (mode S, `Idempotent-Replay: true`, empty body, the handler never runs again) — including after an ownership transfer; `replay_safe(reason=)` opts a non-sensitive route in to body replay (mode B); a sensitive write is S; the replay is decided after every route-level dependency (session check, gates, step-up); 422 on a different request, cross-route / cross-tenant reuse never replays. | `src/tests/integration/test_automatic_idempotency.py::test_the_automatic_guard_replays_the_status_only_never_a_body`, `::test_replay_safe_replays_the_body_after_every_route_dependency`, `::test_a_revoked_session_is_refused_before_any_replay`, `::test_mode_x_re_runs_authorization_and_the_handler` |
| G-IDEM-18 | A write route without a request unit gets the fail-closed in-flight form (concurrent duplicate 409, cache outage 503, nothing stored). | `src/tests/integration/test_automatic_idempotency.py::test_without_a_request_unit_the_guard_is_the_fail_closed_in_flight_form` |

### G-SENS (ADR-0009 "Risk is declared at its source", "What risk triggers")

| id | statement | tests |
| --- | --- | --- |
| G-SENS-01 | The framework's admin gates carry the admin risk statically: `require_platform_admin`, `require(Role("admin"))`, `require(OrgRole("admin"\|"owner"))` (and `Or` of them), tenancy's `require_org_role("admin"\|"owner")`, the platform console's operator gate; member gates and `Not` do not. | `src/tests/integration/test_sensitive_risk.py::test_admin_gates_carry_the_admin_risk_and_member_gates_do_not`, `::test_every_platform_route_behind_an_admin_gate_resolves_the_admin_risk`; `src/tests/architecture/test_operation_classes.py::test_kernel_routes_carry_the_kernel_classes` |
| G-SENS-02 | Money sources: `contract_method(command=True, risk="money")` (recorded in the describe document and the contract snapshot; billing `credits.spend`), the `MoneyLedger` mixin (validated NOT NULL + UNIQUE operation key; billing's credit ledger; an INSERT in a request is a money effect), `@sensitive_effect("money")` (billing intents). | `src/tests/integration/test_sensitive_risk.py::test_a_money_command_records_its_risk_in_the_contract_schema`, `::test_a_money_ledger_must_name_a_not_null_unique_operation_key`, `::test_a_money_route_commits_only_with_a_deduped_money_effect_or_a_key` |
| G-SENS-03 | Security: identity's credential services (password change, 2FA init / enable / disable / backup-code regeneration, SSO link / unlink) are `@sensitive_effect("security", step_up=300)`; `mark_reauthenticated()` after the current-password check (or a recent `auth_time`) satisfies it; identity's behaviour is unchanged. | `src/tests/integration/test_sensitive_risk.py::test_a_security_effect_needs_a_recent_auth_time_or_a_reauthentication`; identity suites (`test_reauth_auth_time.py`, `test_review_security.py`, 2FA / SSO / password tests) unchanged |
| G-SENS-04 | One automatic audit row (`aja.sensitive_write`) per sensitive write, in the request's writing unit, mandatory: actor, route template, method, path parameters, risks with their sources, the route's status; never the body; none for a read or a refused request. | `src/tests/integration/test_sensitive_risk.py::test_a_sensitive_write_gets_one_audit_row_and_a_read_or_failure_none` |
| G-SENS-05 | Money dedupe is mandatory: a money effect needs a dedupe record (its own `operation_key`, the request's `request_operation_key()`, a command key, a ledger key); a client without `Idempotency-Key` gets `428` (AJA-R010); a route that cannot dedupe the key it got is AJA-R009; a `sensitive("money")` write committing without either is refused before COMMIT; at startup a money route's replay-only or fail-open guard is refused (AJA-B019; replaces the class-F check). | `src/tests/integration/test_sensitive_risk.py::test_a_money_effect_needs_a_dedupe_record_or_answers_428`, `::test_a_key_the_route_cannot_dedupe_is_a_developer_error`, `::test_a_money_route_commits_only_with_a_deduped_money_effect_or_a_key`; `src/aja/core/tests/test_durable_idempotency_guard.py::test_f_route_refuses_a_replay_only_guard`, `::test_f_route_accepts_durable_and_endpoint_owned_guards`, `::test_bundled_f_routes_pass_and_the_platform_wraps_no_route` (kept names) |
| G-SENS-06 | Step-up (RFC 9470): an effect declaring `step_up=N` (security: 300 s) needs `auth_time` (never `iat`) ≤ N s old or `mark_reauthenticated()` in the request, else `401 WWW-Authenticate: Bearer error="insufficient_user_authentication", max_age=N` (AJA-R011); checked at entry, at the effect's return and before the writing unit's COMMIT; a `sensitive("security")` route refuses before the response, reads included. | `src/tests/integration/test_sensitive_risk.py::test_a_security_effect_needs_a_recent_auth_time_or_a_reauthentication`, `::test_a_security_route_refuses_before_the_response_even_on_a_read` |

### G-UNIT, C-UNIT (ADR-0010 §4 "Transaction and replay lifecycle", §5 "One writer per request")

| id | statement | tests |
| --- | --- | --- |
| G-UNIT-01 | `db()` is the session of the innermost open unit of the current context; outside any unit AJA-R002; the first `db()` in a request creates the lazy request unit (handler task only), committed on success and rolled back on error before the response; a closed unit refuses reuse. | `src/tests/integration/test_units_and_admission.py::test_db_outside_any_unit_is_refused`, `::test_db_in_a_block_is_that_units_session`, `::test_lazy_request_unit_commits_on_success_and_rolls_back_on_error`, `::test_child_db_before_the_parent_creates_nothing_then_parent_owns`, `::test_child_open_uow_before_the_parent_works`, `::test_an_exited_unit_refuses_reuse_and_never_autobegins` |
| C-UNIT-01 | `db()` from a task that does not own the unit is AJA-R013; child tasks use `open_uow()`. | `src/tests/integration/test_units_and_admission.py::test_a_session_passed_to_another_task_is_detected`, `::test_child_db_before_the_parent_creates_nothing_then_parent_owns` |
| C-UNIT-02 | Best-effort footgun detection (worded as detection, not a guarantee): a session object used from another task is detected on its kernel-session methods and at the cursor; known undetectable forms: in-place mutation of a mutable column value (JSON), attribute assignment on a loaded instance (no attribute listeners — see the perf gate). | `src/tests/integration/test_units_and_admission.py::test_a_session_passed_to_another_task_is_detected`, `::test_in_place_json_mutation_is_a_known_undetectable_form` |
| G-UNIT-02 | Operation-intent mode: no request unit, sequential units; `external_call()` and `release_check()` exclude any unit / read-scope connection / checkout during their lifetime (AJA-R008, the authority callback's read-contract calls excepted); a 2xx outside the issue case (≥1 external call, `release_check` after the last, exactly one write after it) or the issued-retry case (no commit after the release) is discarded with AJA-R012; an organization account needs the authority; `db()` is AJA-R002; AJA-B019 at startup. | `src/tests/integration/test_route_runtime.py::test_an_intent_2xx_without_release_check_is_discarded`, `::test_two_writes_after_the_release_check_are_discarded`, `::test_the_issue_and_issued_retry_cases_pass_and_hold_no_connection`, `::test_release_check_for_an_org_needs_the_authority_and_db_is_refused`, `::test_capability_replay_checks_at_startup`; `src/aja/billing/tests/test_operation_intents.py`, `test_intent_reconcile.py`, `test_f_class_authority.py` (kept names) |

### G-MSTORE (spec §4.4, §6.1)

| id | statement | tests |
| --- | --- | --- |
| G-MSTORE-03/04 (restated) | One writer per request: the first unit that writes becomes the writer; a write by another unit is refused before the cursor (AJA-R007), unflushed changes at COMMIT included; declared secondaries (`allow_multi_db_write`) are admitted; contract, consumer, task, saga-step and kernel units run in a fresh admission scope. Stated scope: SQLAlchemy statements through kernel sessions/connections (raw DBAPI is unavailable to modules). | `src/tests/integration/test_units_and_admission.py::test_a_second_writing_unit_is_refused_before_the_cursor`, `::test_an_unflushed_change_in_a_second_unit_is_refused_at_commit`, `::test_the_first_writer_wins_even_if_it_opened_later`, `::test_read_only_parent_and_a_childs_sole_write_is_allowed`, `::test_a_child_and_its_parent_never_both_write`, `::test_outside_a_request_units_are_unconstrained`, `::test_declared_secondaries_are_admitted`, `::test_random_unit_sequences_never_execute_a_second_writer`, `::test_classification_of_core_constructs` |
| G-MSTORE (B009/B013) | `create_app` refuses a write route with several writable sessions and neither `writes()` nor `allow_multi_db_write`, a `writes()` naming a database it does not inject (AJA-B009), and an `allow_multi_db_write` secondary that is the write store or not injected (AJA-B013). | `src/tests/integration/test_write_store.py::test_several_writable_sessions_need_a_write_store` |
| G-MSTORE (B014) | A money ledger insert in an opted-out secondary is refused (AJA-B014). | `src/tests/integration/test_write_store.py::test_a_money_effect_in_an_opted_out_secondary_is_refused` |

AJA-B010 / AJA-B011 (store capability facts) are **not delivered at H1**.

### Authorization subjects (ADR-0010 §7 "Explicit authorization subjects"; spec §7)

| id | statement | tests |
| --- | --- | --- |
| G-AUTHZ-09 (new) | `Subscribed`, `SubscriptionActive`, `Credits`, `Feature`, `WithinLimit` require `of=`; a missing / non-`Account` value is `AuthzSubjectMissingError` (a `TypeError`, AJA-B017) naming the three choices. | `src/tests/integration/test_authz_subjects.py::test_every_billing_atom_requires_of` |
| G-AUTHZ-10 (new) | `Account.org(param=)` resolves path, then query parameter, else `X-Tenant-Id`, else the bound tenant, validated as before; no organization or the caller's own personal tenant is indeterminate — never the caller's user id; `Account.user()` is the caller's own account on any route. Property over random route shapes and bindings. | `src/tests/integration/test_authz_subjects.py::test_route_shapes_resolve_the_stated_account_only`, `::test_no_resolution_returns_a_foreign_account` |
| G-AUTHZ-11 (new) | `Account.bound_tenant()` by tenant kind (personal → user, organization confirmed by the membership contract → org, canonical / unknown / unbound → indeterminate); `tenant_kind()` is authoritative only and memoised per request. | `src/tests/integration/test_authz_subjects.py::test_bound_tenant_resolves_by_tenant_kind`, `::test_tenant_kind_is_authoritative_and_memoised` |
| G-AUTHZ-12 (new) | `Feature` / `WithinLimit` with an organization account read the owner's `organization.features` / `organization.limits` facts live (501 without a registered fact); with `Account.user()` the claims as today. | `src/tests/integration/test_authz_subjects.py::test_org_feature_and_limit_need_the_owners_fact` |
| G-BILLING-20 (new) | `billing.credits.v1.spend` / `get_balance` hold a personal account to the end user of the call when the call context carries one (the request's subject locally, the MAC-covered `usr` over RPC): another user's id is `BillingTenantMismatchError` (403); system and service callers unchanged. | `src/tests/integration/test_authz_subjects.py::test_a_personal_spend_must_be_the_callers_own` |
| (example) | The helpdesk export gate checks the organization's subscription: organization subscribed → allow; personal only → 402. | `examples/helpdesk/helpdesk/modules/tickets/tests/test_tickets_api.py::TestTickets::test_export_requires_subscription` |

Billing account selection (spec §7.4): no ledger entry pins the removed
single-organization inference; the pinning billing tests were rewritten
(`src/aja/billing/tests/test_billing_reads_semantics.py`,
`test_tenant_resolver_split.py` — two tests renamed to
`test_token_with_tenant_ids_and_no_membership_contract_is_personal` and
`test_tenancy_deployed_but_unbound_resolves_personal`,
`test_billing_api.py`, `test_tenant_gates.py` —
`test_implicit_single_org_member_cannot_manage` renamed to
`test_no_org_id_never_manages_the_single_org` — and
`test_f_class_authority.py`). `BILLING_SPEC` §6 needs the matching update (WS-C).

### Request boundary

| id | statement | tests |
| --- | --- | --- |
| G-TENANT-12 (gap closed) | Also a `MissingTenantError` reaching the request boundary answers `400 tenant context required` (never a bare 500). | `src/tests/integration/test_request_boundaries.py::test_a_missing_tenant_at_the_boundary_is_the_tenant_scope_400` |
| C-ROUTE (current_service) | `current_service` is a service authenticator (internal token, 404 otherwise) returning the service principal. | `src/tests/integration/test_request_boundaries.py::test_current_service_authenticates_a_service_principal` |

---

## 2. Reworded entries (same or stronger)

| id | rewording | tests |
| --- | --- | --- |
| G-REVOKE-10, 11, 12 | "strict and budgeted routes" → every authenticated route; one `GET` of the subject record; a corrupt record fails closed and issuance repairs it. | `src/aja/core/tests/test_revocation_immediate_and_corrupt.py::test_corrupt_mark_rejects_every_token_and_is_counted`, `src/aja/core/tests/test_subject_record.py::test_a_corrupt_record_fails_closed_and_issuance_repairs_it`, `test_redis_down_fail_closed` (kept, rewritten), `src/aja/core/tests/test_revocation_freshness.py::test_strict_default_sees_revocation_on_next_request` |
| G-REVOKE-17, C-AUTH-07 | the re-check runs before the COMMIT of every committing unit of the request; automatic on writes and writing GETs; `at_commit()` for explicit commit obligations (replaces `revocation_at_commit()`). | as G-REVOKE-22 |
| G-REVOKE-18 | remote commands under an armed re-check are propagated (provider re-checks in its unit) instead of refused; reads unchanged. | as G-REVOKE-24 |
| G-BLOCK-01, 02, 03 | the block is a value of the subject record, read live on every authenticated request; unblock by CAS after commit. | `src/aja/core/tests/test_revocation_freshness.py::test_block_applies_on_next_request`, `src/aja/core/tests/test_revocation_immediate_and_corrupt.py::test_a_blocked_record_blocks_every_token`, `src/tests/integration/test_platform_routes_strict.py::test_every_authenticated_platform_route_refuses_a_withdrawn_session`, `src/aja/core/tests/test_route_markers.py::test_differential_strict_budgeted_revoked_blocked` (kept name), G-BLOCK-02/03 tests kept by name |
| G-OPCLASS-03 | per-module policy snapshot pins every route's resolved policy (format 3). | `src/tests/architecture/test_operation_classes.py::test_module_routes_match_their_snapshots`, `::test_kernel_routes_carry_the_kernel_classes`, `::test_a_stale_snapshot_names_the_route_and_the_fix`; `src/aja/billing/tests/test_billing_route_classes.py` (kept file) |
| G-OPCLASS-04 | the model E markers (`public`, `no_idempotency`, `replay_safe`, `sensitive`, `operation_intent`, `writes`, `allow_multi_db_write`) are pruned from `route.dependant` and still read; responses, OpenAPI and session behaviour unchanged. | `src/aja/core/tests/test_route_markers.py::test_create_app_prunes_every_marker`, `::test_pruned_markers_are_no_longer_called`, `::test_openapi_is_identical`, `::test_marker_bound_to_a_parameter_is_kept` (kept names) |
| G-OPCLASS-05, 06, 07 | "class A / RS" → "admin-gated" / "platform-admin gated" (the gates now carry the admin risk); locks and audits unchanged. | unchanged files |
| G-AUTHZ-01, 02, 03, C-AUTH-06 | atoms take a required `of=`; claims-only `Subscribed(of=Account.user())` still does no I/O; the tests pass `of=Account.user()`. | existing node ids (unchanged names) |
| G-BILLING-01, 02, 05 | the same protocol under `operation_intent`; `release_check` wraps the two re-reads. | billing tests (kept names) |
| G-BILLING-19 | enforced by write admission rule 5 (sequential intent units) and the lifetime exclusion; gap closed. | `src/tests/integration/test_route_runtime.py::test_the_issue_and_issued_retry_cases_pass_and_hold_no_connection` |
| G-TENANT-12 | still `400`; additionally a boundary `MissingTenantError` maps to it; gap closed. | see §1 |
| G-CONFIG (REMOVED) | `AUTH_REVOCATION_STRICT_PATH_PREFIXES` in `config_tiers.REMOVED`, inert `[]`. | `src/aja/core/tests/test_revocation_freshness.py::test_the_freshness_knobs_are_gone`; `src/tests/architecture/test_operation_classes.py::test_strict_path_prefix_satisfies_strictness` (kept name) |

---

## 3. Removed entries (each with its superseding id)

| removed | superseded by | note |
| --- | --- | --- |
| G-OPCLASS-01 | G-ROUTE-01 | presence of authentication is now a startup refusal (was a CI gate). Its node ids are kept and assert G-ROUTE-01. |
| G-OPCLASS-02 | G-ROUTE-01 / G-ROUTE-04 | the budget / strictness rules are moot (every route strict; no budgets). Node ids kept. |
| G-REVOKE-13, 14, 15, 16, 20 | G-REVOKE-21 | one live `GET`, memoised; no budgets, no local copies. |
| G-IDEM-13, G-GUARD-03 | G-SENS-05 | the class-F startup check is kept as the money-route check (AJA-B019); money dedupe is required on every path. Node ids kept. |
| C-TEST-04 | C-TEST-07 (WS-B) | `assert_operation_classes` removed; `aja.core.testing.route_policy(route)` now returns `auth` / `public_reason` (apps assert deny-by-default with it). |

Vanished node ids (were referenced only by the removed entries above):
`test_corrupt_mark_rejects_on_budgeted_reads_too`,
`test_copy_expiring_during_block_read_fails_closed_on_outage`,
`test_block_applies_on_next_request_even_on_budget_routes` (→
`test_block_applies_on_next_request`), `test_any_block_value_blocks` (→
`test_a_blocked_record_blocks_every_token`), billing
`test_blocked_user_rejected_on_every_route_like_before` (→
`src/tests/integration/test_platform_routes_strict.py::test_every_authenticated_platform_route_refuses_a_withdrawn_session`).

---

## 4. ADR section mapping (spec §9.3 / §9.4)

| ADR section | ids |
| --- | --- |
| ADR-0009 "Defaults every route gets (nothing to declare)" | G-ROUTE-01, 02, 03, G-REVOKE-21, 22, 23, 24, G-HTTP-12, G-IDEM-17, 18, C-ROUTE-01 |
| ADR-0009 "Risk is declared at its source, once, by its owner" | G-SENS-01, 02, 03, C-ROUTE-02 |
| ADR-0009 "What risk triggers (applied by the framework)" | G-SENS-04, 05, 06 |
| ADR-0009 "One subject-state record replaces the two marks" | G-BLOCK-06, 07; reworded G-REVOKE-10/11/12, G-BLOCK-01–03 |
| ADR-0009 "The route snapshot" | G-ROUTE-04 (G-ROUTE-05 planned) |
| ADR-0009 "Removed outright (unreleased, so no deprecation period)" | the removals of §3; G-CONFIG (REMOVED setting) |
| ADR-0009 "Checks (deterministic; each error names its fix)" | G-ROUTE-01, G-SENS-05, G-SENS-06, G-REVOKE-21, G-REVOKE-22, G-GUARD-01 |
| ADR-0010 §4 "Transaction and replay lifecycle" | G-UNIT-01, C-UNIT-01, C-UNIT-02, G-UNIT-02, G-IDEM-17, 18 |
| ADR-0010 §5 "One writer per request" | G-MSTORE-03/04 (restated), G-MSTORE B009 / B013 / B014 |
| ADR-0010 §7 "Explicit authorization subjects" | G-AUTHZ-09..12, G-BILLING-20; reworded G-AUTHZ-01/02/03, C-AUTH-06 |
| ADR-0010 §10 "Checks and error codes" | the catalogue changes of §5 |

---

## 5. Error-code catalogue (`aja.core.error_codes`)

Landed (the `planned` field cleared): AJA-B008, B009, B013, B014, B017, B019,
R007, R008, R009, R010, R011, R012, R013; for WS-B: B012, B015, B016, B018,
T005, T007, T008, T009. AJA-R003's fix now names an Event class.

Retired: AJA-B003, AJA-B004 (`retired=True`, never reused).

Still planned: AJA-B010, B011 (store capability facts); C021–C027 (WS-B rules —
`test_every_check_rule_has_a_catalogue_entry` pins the parity of landed check
codes with `aja check` rules, so they land with their rules); T006.

**Deviation:** AJA-B007 (gate order) is **kept active**, not retired: the
gate-order bug it catches is real until `module_router()` (WS-B) derives
`TenantScope` per route; retiring it now would weaken a startup check.

---

## 6. Recorded decisions (readings that weaken nothing)

- Transaction-control statements (`BEGIN` / `COMMIT` / `SAVEPOINT` …) are not
  writes for admission.
- Operation intents count only the route's own units (contract units run in a
  fresh admission scope); the issued-retry case allows no commit after the
  release (the stricter reading).
- Risk triggers apply inside a request only; consumers, tasks and saga steps
  keep their effect-level dedupe. Step-up is skipped on public routes (no
  session to step up; the route's own proof applies).
- An effect's step-up is checked at entry; when unsatisfied there it is
  re-checked at the effect's return and before the writing unit's COMMIT, so
  identity's current-password check inside the effect satisfies it.
- The automatic audit row rides on the request's writing unit (none when no
  unit writes); intent units are excluded; its `status` is the route's
  declared status code.
- A remote money command marks only the provider's scope (no caller-side
  propagation of the risk yet).
- The automatic idempotency guard wraps the endpoint call (after every
  dependency; no per-request dependency solve). Its durable record goes to the
  route's request unit (`writes()` picks among several); without a request
  unit (or a durable log on it) the fail-closed in-flight form.
- An `Account.org()` resolution that yields the caller's own personal tenant is
  indeterminate (403) instead of asking billing about the user's id as an
  organization.
- The personal-spend check reads the end user the contract runtime stamps on
  the contract unit (`contract_session().info["aja_contract_caller_user"]`);
  a public accessor (`caller_user_id`) exists in `aja.core.module_api._context`
  and needs an `aja.sdk.contracts` home (WS-B) before billing uses it by name.
