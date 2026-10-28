# Ledger delta: the api10 DX-eval fixes (2026-10-26)

Applied to `backend/guarantees.yml` by the integrator of the DX-eval fix list
(`Docs/design/dx-eval-api10-findings-2026-10-26.md`). Test paths are relative
to `backend/`.

## Changed entries (same strength, re-pointed)

| id | change | disposition |
| --- | --- | --- |
| C-MODULE-01 | names `aja add module <name>`: the legacy `aja scaffold` generator is removed (the owner: `aja add` is the one generator path). Enforced by `src/aja/core/tests/test_module_generation.py::test_add_module_generates_a_compliant_parseable_skeleton` and `::test_add_module_refuses_bad_names_and_overwrites` (the same assertions against `aja add module` output: parseable, ORM-free API, versioned channel, `TenantScoped` example, namespace-portable, declarative register, bad names and overwrites refused) | kept, re-pointed |
| G-MODULE-18 | "a generated module (aja add module)"; enforced by `test_module_generation.py::test_add_module_boilerplate_is_minimal` (framework tree and app) and `::test_add_module_register_is_declarative` | kept, re-pointed |
| C-OPS-13 | "Static CLIs (aja check, aja add)"; `test_static_cli_no_env.py` runs `aja add --help` and `aja add deploy --help` with an empty environment | kept, re-pointed |

## New entries

| id | what | enforced by |
| --- | --- | --- |
| C-DEPLOY-06 | `aja add deploy` deploys a module with unbound required contracts (topology read; the app path stays strict) and adds the worker / beat pair for periodic tasks | `test_scaffold_deploy.py`, `test_consumes_preflight.py::test_topology_read_records_a_missing_required_contract` |
| C-TEST-13 | `aja test` falls back to fewer workers / serial (Redis indexes short, no pytest-xdist) | `test_testing_resources.py`, `test_quickstart.py::test_aja_test_runs_serially_without_xdist` |
| C-TEST-14 | the static `schema & deploy` step (AJA-T002 from the syntax tree, AJA-T012, AJA-T010, AJA-T013 warning) | `test_run_tests_cli.py`, `test_model_indexes_migrated.py`, `test_deploy_profiles.py` |
| C-TEST-15 | `fake_contract` / `fake_access_fact` reach the kernel's `get_service` and the org billing atoms | `test_testing_seams.py` |
| C-TENANT-10 | `TenantScoped` drops its plain `tenant_id` index under a tenant-first unique key | `test_model_indexes_migrated.py` |
| C-GEN-08 | `aja add module` wires the module and gives framework-tree tests the kit | `test_sdk_generators.py` |
| C-GEN-09 | `aja add task` (idempotent task, schedule, beat wiring) | `test_sdk_generators.py` |
| C-GEN-10 | the quickstart passes as printed (one source with the docs) | `test_quickstart.py`, CI job `quickstart` |
| C-MODULE-10 | route-policy atoms add their contracts to the derived `consumes` | `test_sdk_discovery.py` |
| C-MODULE-11 | `identity.contact.v1` | `identity/tests/test_contact_contract.py` |
| C-SDK-03 | typed `aja.sdk.tasks.shared_task` and `cron` (no mypy override) | `test_sdk_generators.py::test_a_fully_generated_app_passes_the_static_gates` |
| C-MIGRATE-09 | `aja migrate new` against a scratch database | `test_migration_revision_cli.py::test_new_autogenerates_against_a_scratch_database` |
| C-PROJ-09 | `SourceGroup(tenant_from=)`; an optional version refused | `test_sdk_projections.py` |
| C-EVENT-04 | tenancy member events `.v2` (dual-published) and `data.plan` on billing subscription events | `tenancy/tests/test_registration_surface.py`, `billing/tests/test_entitlement_intentless.py` |

No guarantee weakened and no capability removed: the removed `aja scaffold`
generator's capability (C-MODULE-01, G-MODULE-18) is carried by
`aja add module` with the same assertions.

## Follow-up: the quickstart walk (2026-10-26)

| id | what | enforced by |
| --- | --- | --- |
| C-GEN-11 | `aja add module` generates a capped (`Query(le=...)`), tenant-scoped `GET /items` and its test | `test_sdk_generators.py::test_add_module_lists_items_capped_and_tenant_scoped`, the fully generated app's tests |
| C-GEN-12 | `aja new --framework-source path` writes editable sources (the live tree, never a stale cached build); the quickstart job checks the installed framework is the checkout | `test_quickstart.py::test_framework_path_takes_the_repository_root`, `::test_an_editable_framework_install_is_the_live_tree`, CI job `quickstart` |

No guarantee weakened.

## Follow-up: an `aja new` app splits into services (2026-10-26)

| id | what | enforced by |
| --- | --- | --- |
| C-DEPLOY-06 | re-pointed: `aja add deploy` now derives the whole profile (its tests were rewritten for the generator) | `test_scaffold_deploy.py::test_a_split_app_passes_the_framework_split_invariants`, `::test_a_module_with_periodic_tasks_gets_its_worker_and_one_beat` |
| C-DEPLOY-07 | an `aja new` app splits into microservices with no code change: `aja add deploy` creates the profile, gateway and init script from packaged templates, derives every service, binding, allowlist and route, is idempotent and incremental, keeps hand edits outside its blocks and refuses edits inside with a diff | `test_scaffold_deploy.py`, `src/tests/e2e/test_app_split.py`, CI job `app-split` |
| C-DEPLOY-08 | the split invariants (AJA-T014) check an app's profile in `aja test` as the framework's; every service boots alone with its bindings | `test_deploy_profiles.py::test_the_profile_passes_the_split_invariants_apps_are_checked_with`, `::test_the_split_invariants_report_what_breaks_them`, `::test_aja_test_checks_the_split_profile_of_a_project`, `::test_every_service_boots_alone_with_its_bindings` |

No guarantee weakened: the framework's own profile is now the generator's
output and passes every check it passed before.

## App-mode eval follow-up (2026-10-26)

| id | disposition |
| --- | --- |
| G-BILLING-22 | new: a credit spend of 0 (free items, promo codes) succeeds and is recorded like any spend (delta-0 ledger row, idempotent on its key, the spent event); negative spends and zero grants stay refused. Found by the app-mode eval (a free course broke enrollment). |

## Final-review fixes (2026-10-26)

Six findings of the final independent review, each reproduced by a failing
test first. Every change strengthens its statement; none is weakened.

| id | change | enforced by |
| --- | --- | --- |
| G-MSTORE-10 | a read key or filter naming another tenant never overrides the bound tenant (reads nothing, fail closed); a store read without a bound tenant is refused | `src/tests/sdk/test_sdk_projection_ops.py::test_a_store_row_of_another_tenant_is_refused_and_reads_are_per_tenant` (now with a foreign tenant in the key) |
| G-MSTORE-07 | a page read waiting on a token rereads its rows every poll round, so a cached row behind the token (not only a missing one) catches up within the budget | `test_sdk_projection_ops.py::test_a_waiting_read_rereads_until_its_group_catches_up` |
| G-MSTORE-08, G-MSTORE-09 | on a store target only a newer write of the group owning deletion clears a tombstone (as on a relational model); the `conditional_write` conformance kit checks it | `test_sdk_projection_ops.py::test_an_independent_group_never_resurrects_a_store_tombstone`, `src/aja/core/tests/test_projection_kernel.py::test_the_conformance_kit_catches_a_store_whose_upserts_clear_tombstones` |
| G-SAGA-12 | a forward command call whose transition rolled back after the provider committed (only the started marker survives) is recovered as a transient attempt from the marker's own time, so recovery after the dedupe TTL parks it (`outcome_unknown_after_ttl`) instead of re-issuing it | `src/aja/core/tests/test_sagas_recovery.py::test_a_crash_after_the_provider_committed_is_never_reissued_after_the_ttl` |
| G-SENS-04, G-SENS-05 | a risky command commits in its own unit: the request writes its `aja.sensitive_write` row even when its own units wrote nothing, the route runtime opening the request unit when the handler opened none; the money rules hold for such a route (the command key is its dedupe record) | `src/tests/integration/test_sensitive_risk.py::test_a_command_only_money_route_gets_its_audit_row` |
| G-UNIT-02, G-BILLING-19 | `external_call()` / `release_check()` wait for every contract call of the request already running in its provider unit (refused from inside one) and refuse a new one while open | `src/aja/core/tests/test_contract_read_scope.py::test_an_intent_window_never_runs_beside_a_running_provider_unit` |

## Final review, tooling side: items 2, 8, 9, 10 (2026-10-26)

From the final independent review of the Public API 1.0 implementation.

| id | change | disposition |
| --- | --- | --- |
| G-OPS-22 | new: no secret file the generators write enters an `aja new` app's image. The `.dockerignore` excludes env files and key files at any depth (the old one excluded root-level `.env*` only, so the split's `deploy/.env.microservices`, holding every service's private keys, shipped in every image); the Dockerfile copies only `pyproject.toml`, `uv.lock` and the package into the build stage and nothing into the runtime stage; `aja add deploy` patches an older `.dockerignore`; the app-split job exports the app's own build context with Docker and the smoke builds the service image from it, both asserting no secret file | `test_image_secrets.py` (incl. a Docker-exported context), `test_scaffold_deploy.py::test_deploy_patches_a_dockerignore_that_would_ship_the_env_file`, `src/tests/e2e/test_app_split.py` |
| G-DECL-01 | strengthened: the same-`.vN` compatibility gate resolves `$ref` / `$defs` and applies the additive-only rule at every depth (a referenced enum's values, a nested DTO's types, removals and newly required fields fail); an added defaulted field passes at any depth (the serialization schema lists it as required, it is optional to a producer) | `test_sdk_generated.py::test_a_referenced_enum_or_nested_type_change_is_incompatible`, `::test_an_added_optional_field_passes_at_every_depth` |
| G-ROUTE-04 | strengthened: `aja test` never writes a weaker route policy over the committed snapshot (authentication removed or made public, a risk removed, idempotency removed / no longer fail-closed / no longer durable, the session check weakened, the audit dropped, step-up removed or relaxed, an authorization dependency removed); it fails before writing, until `aja routes snapshot --write` approves it, naming each weakening it approves | `test_sdk_generated.py::test_every_weakened_route_policy_is_incompatible`, `::test_a_route_made_public_is_incompatible`, `::test_a_stronger_route_policy_is_compatible`, `::test_aja_test_refuses_a_weakened_route_snapshot_and_the_write_approves_it` |
| C-DEPLOY-09 | new: a service added to a running stack gets its database on the existing volume: every generated `migrate-<service>` runs `aja migrate upgrade-all --apply --create-databases` (idempotent, race-safe, with `citext` / `pg_trgm`), also in a profile generated before | `test_migrate_creates_database.py`, `test_scaffold_deploy.py::test_a_service_added_later_creates_its_own_database`, `test_deploy_profiles.py::test_microservices_db_per_service_and_bindings`, the split smoke (`src/tests/e2e/test_app_split.py`: a service added to the running stack) |

No guarantee weakened.

### Item 11 (found twice by the DX eval)

| id | change | disposition |
| --- | --- | --- |
| C-GEN-11 | strengthened: the generated model's `created_at` defaults to `clock_timestamp()` (was `now()`, the transaction's start: rows created in one test transaction tied and a newest-first list was order-unstable), the list orders by `created_at` then `id`, and the generated test asserts the newest-first order of rows created in one transaction | `test_sdk_generators.py::test_add_module_lists_newest_first_deterministically_in_one_transaction`, the generated app's own tests (`::test_a_fully_generated_app_passes_its_tests_up_to_the_h1_stubs`, the quickstart job) |

No guarantee weakened.

## Re-review fixes (tooling/billing) (2026-10-26)

From the independent re-review of `main` (2e79a440). Each defect was
reproduced by a failing test first; every change strengthens its statement.

| id | change | enforced by |
| --- | --- | --- |
| G-ROUTE-04 | strengthened: route idempotency is compared by properties (present, fail-closed, durable) derived from every mode the snapshot writes (`none`, `none (<reason>)`, `automatic S`, `automatic B (<reason>)`, `explicit durable/cache/in-flight[, fail-closed]`), so losing any one is a weakening whatever the spelling. `automatic S` -> `explicit in-flight, fail-closed` passed before (only the literal `explicit durable` counted as durable). An unknown mode is flagged, never accepted silently | `test_sdk_generated.py::test_every_idempotency_mode_transition_that_loses_a_property_is_flagged`, `::test_an_idempotency_mode_that_keeps_every_property_is_compatible`, `::test_every_snapshot_idempotency_spelling_has_properties` |
| G-DECL-01 | strengthened: (1) a recursive DTO (self- or mutually recursive) is compared once per `($ref old, $ref new)` pair on a descent. The guard keyed on the ever-growing field path, so comparing such an event with itself raised `RecursionError` and blocked `aja test`. A change inside the cycle still fails. (2) A validation constraint that narrows the accepted values fails: a bound raised, lowered or added (inclusive and exclusive bounds compared as one), a size keyword tightened or added, `multipleOf` added or no longer dividing the old one, `pattern` added or changed, `uniqueItems` / `additionalProperties: false` added. Loosening or removing a constraint passes. Before, `Field(ge=0)` -> `Field(ge=1)` passed although the new reader rejects `{value: 0}` | `test_sdk_generated.py::test_a_recursive_dto_compares_without_recursing_forever`, `::test_a_tightened_constraint_is_incompatible`, `::test_a_loosened_or_unchanged_constraint_is_compatible`, `::test_a_changed_format_or_const_is_incompatible`, `::test_unique_items_and_forbidden_extras_narrow_a_nested_dto` |
| G-BILLING-22 | strengthened: a spend of 0 succeeds and is recorded (delta-0 ledger row under its key, the spent event, an idempotent replay) whatever the balance, including a negative one left by a refund or a dispute reversal. The debit's `balance >= amount` predicate applied to 0 too, so balance -1 with spend 0 raised `InsufficientCreditsError` with nothing recorded. A positive spend still needs cover | `src/aja/billing/tests/test_credits.py::TestCreditIdempotencyHardening::test_a_zero_spend_succeeds_on_a_negative_balance` |

## Re-review fixes (2026-10-26)

Six defects an independent re-review verified on main (A-F), each reproduced
by a failing test first, then fixed at the root. Every statement is
strengthened; no guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-UNIT-02, G-BILLING-19 | (A) a REMOTE contract call obeys the intent windows exactly like a local one: the generated RPC proxy refuses a call inside `external_call()` / `release_check()` before sending it (AJA-R008; the authority callback's reads excepted, the proxy describes the contract first to tell a read from a command) and counts a running call, so a window entered meanwhile waits for it | `src/aja/core/tests/test_rpc_intent_window.py::test_a_remote_command_inside_an_intent_window_is_refused`, `::test_an_intent_window_waits_for_a_running_remote_command` (the signed RPC path, in-process ASGI) |
| G-MSTORE-07 | (B) a relational read waiting on a token reloads the rows its session already holds (`populate_existing`): the ORM identity map served the cached v1 row for every reread, so a v2 committed 10 ms into a 100 ms wait came back as v1 with `stale=True` (a `get` even returned v1 with `stale=False`) | `src/tests/sdk/test_sdk_projection_ops.py::test_a_waiting_relational_read_reloads_the_rows_it_already_holds` |
| G-SENS-04 | (C) the RPC response's `rk` carries every risk the remote execution OBSERVED (`{risk: [source, ...]}`, nested calls included), not only the outer method's declared risk: an untagged command calling a money command marks the caller's request identically after a split, so its audit unit opens | `src/aja/core/tests/test_command_risk_parity.py::test_a_nested_money_command_marks_the_callers_request_locally_and_remotely` |
| G-BILLING-20 | (D) the command dedupe record is scoped to the calling end user (the request's subject locally, the signed `usr` over RPC), for every `@command`: another user reusing a spender's operation key runs the body and gets its own authorization decision (403), never the stored result; the spender's retry still replays | `src/tests/integration/test_durable_command_idempotency.py::test_a_replay_never_serves_another_callers_result`, `::test_a_personal_spend_replay_is_the_spenders_only` |
| G-MSTORE-09 | (E) the rebuild marker counts running rebuilds (`projection_watermarks.rebuilds_active`, core revision `0012_projection_rebuilds_active`; the in-memory store alike): `rebuild_started_at` stays set until the last rebuild ends, so rebuild A finishing never re-enables the purge while rebuild B runs | `src/aja/core/tests/test_projection_kernel.py::test_concurrent_rebuilds_keep_the_purge_excluded_in_memory`, `src/aja/core/tests/test_projection_watermarks_db.py::test_concurrent_rebuilds_keep_the_purge_excluded_in_the_table`, `::test_racing_rebuild_marks_are_counted_exactly` |
| G-MSTORE-03 | (F) `allow_multi_db_write`: a declared secondary is never the request's writer, so writing it before the write store is admitted (was AJA-R007); the write store's session is solved before every secondary's (startup reorder), so the secondaries commit first and the write store last whatever the endpoint's parameter order | `src/tests/integration/test_units_and_admission.py::test_a_secondary_written_before_the_write_store_is_admitted`, `src/tests/integration/test_route_runtime.py::test_the_secondary_commits_before_the_write_store_whatever_the_order` |

No guarantee weakened.

## Round-3 fixes (RPC parity) (2026-10-26)

Four defects the round-3 review verified share one theme: a split deploy
must behave exactly like the monolith for security context. One design
replaces the separate `rc` / `usr` / `rk` / `sr` fields and the
provider-side `_caller_user` variable (`aja.core.rpc.context`):

- **Out: the caller facts** (`usr` in the MAC-covered context) of a call
  made for an end user: `sub`, `iat_us`, `auth_time`, `ra`
  (re-authenticated), `rc` (the commit-time re-check reaches command units),
  `op` (the request's operation key) and `ik` (the client sent an
  `Idempotency-Key`). The provider binds them as the call's request scope,
  for reads and commands, so its checks read them exactly as for a local
  call and its nested calls carry them on. No facts means a system call.
- **Back: the call effects** (`fx` in the MAC-bound response, on success
  AND error): `rk` (observed risks), `sr` (committed self-revocations),
  `ra`, `su` (pending step-ups), merged before the caller raises.
- **In doubt: no window.** The proxy records a call whose attempt the
  provider never answered; `external_call()` / `release_check()` then refuse
  (503 `RemoteCallInDoubtError`, AJA-R008). Fail closed was chosen over a
  status probe (it cannot tell "not started yet" from "never will start"
  without a fence) and over a caller-sent provider deadline (a stalled
  provider still holds its unit past it): neither is airtight, fail closed
  is by construction. The client retries with its `Idempotency-Key`; the
  command replays or waits for the running one.

Each defect was reproduced first by a failing test on the signed RPC path
(`src/tests/integration/test_rpc_caller_parity.py`; in-process ASGI, the
timeout case on a loopback uvicorn server). The parity matrix runs 6
principals x 5 scenarios local and split and compares outcome, risks, the
audit row (or the commit's refusal), re-authentication and pending step-ups.

| id | change | enforced by |
| --- | --- | --- |
| G-UNIT-02, G-BILLING-19 | (1) strengthened: the intent-window counter tracked transport completion, so after a timeout (the provider's command still running) a caller that caught the 503 opened `external_call()` beside the provider unit. A remote call with an unanswered attempt now refuses every later window of the request (503, AJA-R008, fail closed); an answered failure leaves it open | `test_rpc_caller_parity.py::test_no_window_opens_while_a_timed_out_remote_command_may_run`, `::test_an_answered_remote_failure_leaves_the_window_open` |
| G-SENS-04 | (2) strengthened: error responses carry the call effects (MAC-bound) and the proxy merges them before raising, so a wrapper that raises 409 after its nested money command committed still marks the request and owes its audit row after a split. A risky effect inside a local contract unit now also marks the request's audit as owed (it commits in its own unit), which the split path already did | `test_rpc_caller_parity.py::test_risks_of_a_failing_command_mark_the_request_local_and_split`, `::test_a_split_call_matches_the_local_one` |
| G-BILLING-20 | (3) strengthened: the end user crosses every hop. Without a re-check context (an operation-intent route) the first provider kept the user in a side variable and its outbound call sent none, so Alice charging Bob's personal account through a split wrapper got 77 instead of 403 | `test_rpc_caller_parity.py::test_the_end_user_crosses_every_hop`, `::test_a_split_call_matches_the_local_one` |
| G-SENS-03 | (4) strengthened: step-up is enforced alike over RPC. With no provider request scope the decorator skipped it (stale auth: 401 locally, success remotely), and with one only `sub` / `iat_us` crossed (fresh auth: 401 remotely). `auth_time` and the request's re-authentication now cross out, a re-authentication and pending step-ups cross back | `test_rpc_caller_parity.py::test_step_up_is_enforced_alike_local_and_split`, `::test_a_reauthentication_in_a_remote_command_reaches_the_request`, `::test_a_split_call_matches_the_local_one` |

No guarantee weakened. The wire format changed (unreleased, no shim): `rc`
became `usr.rc`, `rk` / `sr` moved into `fx`.

## Round-4 fixes (RPC) (2026-10-26)

Three parity gaps the fourth independent review verified on main
(`407b6b9d`), each reproduced by a failing test on the signed RPC path first,
then fixed at the root within the round-3 design (caller facts out, call
effects back). Every statement is strengthened; no guarantee is weakened and
no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-UNIT-02, G-BILLING-19 | (A) strengthened: in-doubt state crosses hops. Timeout tracking wrote only to the request's intent state, and a provider's reconstructed request scope has none, so root -> wrapper (RPC) -> blocked command (RPC) whose wrapper caught its timeout and answered OK let the root open `external_call()` beside the still-running command. A served call now observes its own unanswered remote calls (any depth, caught or not) and returns them MAC-bound (`fx.id`); every caller merges them into its intent state and its own observer. A local wrapper catching a nested remote timeout already left the request in doubt (verified). The uncertainty is not kept with a command's dedupe record: a replay after the unit finished opens the window (as the monolith's replay) | `test_rpc_caller_parity.py::test_a_nested_remote_timeout_keeps_the_root_in_doubt` (1 and 2 hops, then the replay), `::test_a_local_wrapper_catching_a_remote_timeout_leaves_the_request_in_doubt` |
| G-SENS-04 | (D) strengthened: a replay marks the caller like the first execution. A replay skips the body and the record kept only the result, so an untagged wrapper whose nested money command ran, replayed after a lost answer (the proxy's automatic retry) or by the same caller's next request, left the caller with no risk and no audit obligation. The command's dedupe record now keeps the call effects the execution left (`contract_command_log.effects`, core migration `0013`; the cache fallback's record alike) and the replay re-applies them, locally and over RPC: risks, the subject's committed self-revocation, pending step-ups. Not kept: a re-authentication (a replay proves none: later step-ups fail closed) and calls in doubt (a running unit, not a committed effect) | `test_rpc_caller_parity.py::test_a_replayed_command_marks_the_request_like_its_first_execution` (cache fallback), `test_durable_command_idempotency.py::test_a_durable_replay_marks_the_request_like_its_first_execution` (PostgreSQL) |
| G-REVOKE-23 | (E) strengthened: the acknowledged self-revocation exemption crosses outbound RPC. The caller facts omitted it, so after an acknowledged self-revoking command a following local command committed and the remote equivalent was 401. The facts carry `usr.sr` (MAC-covered, signed on commands) and the provider binds it as its scope's self-revocation, so its re-check skips the subject exactly as a local unit's; a replay of the self-revoking command acknowledges it alike (D) | `test_durable_command_idempotency.py::test_an_acknowledged_self_revocation_crosses_rpc`, `::test_a_durable_replay_keeps_the_acknowledged_self_revocation` |
| G-CMD-04 | (residual, documented) a failed command leaves no record, so an EARLIER attempt of the same call still in flight (attempt 1 unanswered, attempt 2 answered with a failure) may run afresh later and succeed. Accepted as the semantics of a client retry after a failure: a fence ordering a call's attempts would need a per-call attempt record surviving the failed attempt's rollback (a new dedupe store, ADR-0005) and would refuse the legitimate retry after a failure the guarantee promises; the unanswered attempt keeps the caller's request in doubt (G-UNIT-02). Statement made precise, unchanged in strength | `Docs/concepts/contracts.md` (Guarantees, precisely: Commands) |

No guarantee weakened. Wire format (unreleased, no shim): `usr.sr` and
`fx.id` added; the command log gained a nullable `effects` column.

## Round-5 fixes (2026-10-27)

Three P2 defects the fifth independent review verified, each reproduced by
a failing test first, then fixed at the root. Every statement is
strengthened; no guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-UNIT-02, G-BILLING-19 | (1) "answered" now means "authenticated effects merged". The proxy marked an `RpcResponseTooLarge` attempt answered although its body (and with it `fx.id`) was never read, so root -> wrapper (RPC) -> blocked gate (RPC), the wrapper swallowing its timeout and answering a result over the root's bounded client limit, let the root catch the 502 and open `external_call()` beside the still-running gate (`in_doubt=[]`). The proxy now notes the call in doubt unless it merged the envelope's effects: an oversized answer, an unkeyed (unauthenticated) refusal, a bound reply without a well-formed envelope (non-msgpack, a 5xx body, neither `ok` nor an `error` object) and malformed `fx` (not an object, or `fx.id` not a list: 502) all count as unanswered; a well-formed success or error envelope, with or without `fx`, settles it. Audit of the other paths: transport failures, timeouts, cancellation and a response bound to another request (MAC mismatch) were already unanswered. Decision: fail closed at the caller rather than have the provider enforce the limit and answer an error carrying `fx` -- `RPC_MAX_RESPONSE_BYTES` is the caller's own setting (the provider cannot know it), and nothing in an unread or unauthenticated body can be trusted; an oversized answer is a deployment defect, so failing closed costs nothing on the normal path | `src/tests/integration/test_rpc_caller_parity.py::test_an_oversized_answer_keeps_the_nested_doubt` (loopback uvicorn, production `HttpxRpcClient` with a 4 KiB limit), `::test_an_answer_whose_effects_were_not_merged_is_in_doubt` (6 cases, re-bound with the provider's pair key), `::test_a_well_formed_answer_settles_the_call` |
| G-DECL-01 | (2) a model allowing extras is an open object. `_struct` treated `additionalProperties: true` (pydantic `extra="allow"`) as a DTO emitting only declared fields, so adding `score: int = 0` to a nested model whose old producer sent `score="free text"` as an extra passed although the new validator rejects the old event. Only a closed DTO (no `additionalProperties`, or `false`) may now gain a field no old producer sent; on an open model the new field is compared with the old `additionalProperties` (`true`: only a field accepting anything passes); the "defaulted field is optional to its reader" exception holds for every model. The differential test gained a fourth base (`extra="allow"` models, one with typed extras), `additionalProperties` true / schema mutations on every object, a mutation declaring a property beside the existing ones (the old "property-added" replaced them, so it never reached this rule), and a sampler sending undeclared keys for an open model: it reports the escape against the old rule and none after (11,658 pairs; a 150-sample hunt over the whole corpus and 4,000 composed chains found no further escape) | `src/tests/sdk/test_sdk_schema_differential.py::test_a_field_declared_on_an_open_model_must_accept_its_old_extras` (real Event classes), `::test_a_change_the_rule_accepts_rejects_no_old_payload`, `::test_composed_changes_the_rule_accepts_reject_no_old_payload` |
| G-SENS-04 | (3) a replay carries what the command's `on_commit` callbacks left. The durable record was written in the unit (before the callbacks) and the cache record right after the COMMIT (before them too), so a keyed `@sensitive_effect("money")` callback marked money + the audit obligation on the first execution and nothing on the replay. Decision: the durable record keeps committing atomically with the command, with the body's effects plus what each queued callback DECLARES (a `@sensitive_effect` callable, a `partial` of one or a bound method: its risk under the source its run records, and its step-up when the caller's auth does not satisfy it), fail closed; after the callbacks ran, the runtime replaces the record's effects with what the execution actually left when they differ (a closure's effects; `CommandLogStore.amend_effects`, one UPDATE in a unit of its own, never raising: a failure is logged and the record keeps the declared value). A command without callbacks, or whose callbacks only did what they declared, writes nothing more. Stored when the process dies between the commit and the correction: the body's effects plus the declared callbacks' (they may have run: an extra audit risk at worst), nothing of a closure (it ran in a request that never answered, or not at all). A callback that fails is no special case: it ran, and what it marked before failing is kept. The cache record is written after the callbacks (in a `finally`, so a cancelled dispatch still leaves the committed command's record) | `src/tests/integration/test_durable_command_idempotency.py::test_a_durable_replay_marks_the_request_with_its_on_commit_callbacks` (tagged, closure, failing; local and RPC; PostgreSQL), `::test_a_crash_before_the_callbacks_leaves_a_fail_closed_record`, `src/tests/integration/test_rpc_caller_parity.py::test_a_replay_marks_the_request_with_its_on_commit_callbacks` (cache record; local and RPC) |

Cachegrind, `L6W6` (the command write path, `CG_K=200`, two interleaved pairs): base `b8dc70f5` 9,186,234 / 9,178,639
instr/op, fixed 9,229,008 / 9,191,195 (+0.14% to +0.47%; the fixed build's
two runs differ by 38k on their own). The command path with no `on_commit`
callback adds one callback-queue lookup and two branches; the proxy changes
are off this rung.

No guarantee weakened. Storage API (unreleased, no shim):
`CommandLogStore.amend_effects` added.

## Round-6 fixes (2026-10-27)

Three defects the sixth independent review verified on `842c6960` (two P2,
one P3, one of them a regression of round 5), each reproduced by a failing
test first, then fixed at the root. Every statement is strengthened; no
guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-SENS-04 | (1) a cancelled `on_commit` callback no longer loses the effects it completed. The durable amendment ran after the dispatch with no `finally`, so a closure that completed a money effect, then blocked and was cancelled (a request timeout), left `stored_fx=None`: the live request had money, a later replay owed no audit. The runtime now writes the correction on EVERY exit after the COMMIT: with what the recorder observed until then, plus what the queued callbacks DECLARE when they did not all finish (fail closed: they may have run; a crash test's record is unchanged). The write runs in a task of its own (`_finish_record`), shielded from the caller's cancellation (anyio cancels again at every await, so an unshielded `finally` is cut short at its first I/O), bounded by 5 s, and never raises into the caller (a failure or timeout is logged; the record keeps its committed value). The cache path, already in a `finally`, now goes through the same shielded write with the same effects; its `committed` flag is set before any await, so a cancellation never releases the key of a committed command | `src/tests/integration/test_durable_command_idempotency.py::test_a_cancelled_on_commit_callback_keeps_its_effects_on_the_record` (`once` and `level` cancellation, PostgreSQL; the real call runtime), `src/tests/integration/test_rpc_caller_parity.py::test_a_cancelled_on_commit_callback_keeps_its_effects_on_the_cache_record` (`once`, `level`; the store write suspends), `src/aja/core/tests/test_contract_finish_record.py` (never raises, bounded, outlives its cancelled caller) |
| G-DECL-01 | (2) the compatibility rule now holds both directions. Round 5 routed a field removed from an open model (`extra="allow"`) through its unrestricted extras, so removing a required `metadata: Any` passed although a NEW producer's `{"info": {"name": "n"}}` fails the OLD reader. The root cause was wider: the rule only checked "a new reader accepts an old producer's payload", while the documented contract (spec §5.2, `concepts/events.md`: on the same `.vN` only an added optional field passes, "any other change fails") means producers and consumers deploy independently, so "an old reader accepts a new producer's payload" matters too. `schema_problems` now runs one comparison per direction (`_SchemaDiff(old, new)` and, with the schemas swapped, `forward=True`), reporting a forward problem only at a path the first did not: a loosened or removed constraint fails (`loosened ... (old -> new)`), a model's field is never removed (open or closed), an optional field added where the old schema forbids extra keys fails, and an added field is compared with the old reader's governing schema (`{}` accepts anything, so the additive case still passes). Docs and rule reconciled with the spec (the docstring, `AJA-T005`'s explanation and `events.md` said loosening passes; the spec never did). The differential test now checks both directions over the whole corpus and gained field removals (`-property.<name>`, on closed DTOs and open models; a defaulted field's only one way, its other direction being the spec's defaulted-field exception) and a required `metadata: Any` on its open model: against the round-5 rule it finds 1,267 escapes in 40 samples per pair, all new-producer -> old-reader (loosenings under every applicator, removed `required` names, and the reported open-model removal `loose:/$defs/_Open:-property.metadata`); none after (a 200-sample hunt over the corpus found none either) | `src/tests/sdk/test_sdk_schema_differential.py::test_a_change_the_rule_accepts_rejects_no_payload_either_way`, `::test_composed_changes_the_rule_accepts_reject_no_payload_either_way`, `::test_a_field_removed_from_an_open_model_breaks_its_old_readers`, `::test_a_loosened_constraint_breaks_its_old_readers`, `src/tests/sdk/test_sdk_generated.py::test_a_loosened_constraint_is_incompatible_too`, `::test_a_loosened_applicator_is_incompatible_too` (the old "loosened is compatible" pins, inverted) |
| G-UNIT-02 | (3) every `fx` entry is validated. `_merge_call_effects` checked that `fx.id` was a list but not its entries, so an authenticated `fx={"id": [None]}` settled the call (the entry was silently dropped) and allowed an intent window. `well_formed_call_effects` now checks every field: only `rk` / `id` / `sr` / `ra` / `su`; `rk` a dict of known risks to at most 64 non-empty strings of at most 256 characters; `id` at most 16 and `sr` at most 16 such strings; `ra` exactly 1; `su` at most 16 positive integers (not booleans). Anything else counts as not merged (in doubt, 502); its well-formed risks still mark the request (fail closed), its `ra` never does | `src/tests/integration/test_rpc_caller_parity.py::test_an_answer_whose_effects_were_not_merged_is_in_doubt` (23 new cases: malformed entries of every field, over-long and over-count lists, an unknown risk and an unknown field), `::test_a_well_formed_answer_settles_the_call` (a full well-formed `fx` still settles) |

Cachegrind, `L6W6` (`CG_K=200`, interleaved): base `842c6960` 9,210,293 /
9,209,474 instr/op, fixed 9,215,940 / 9,210,835 (+0.01% to +0.07%, within
run-to-run noise). A command without `on_commit` callbacks takes the same
path as before; the shielded write runs only after a command whose
callbacks ran (durable) or on the cache fallback.

No guarantee weakened.

## Round-11 fixes (2026-10-27)

Four P2 and two P3 the eleventh independent review verified on `30998e35`,
all "the wire form does not round-trip". Each is reproduced by a failing
test first, and the remaining degrees of freedom of the closed set are
closed rather than patched: what a class may declare is narrowed so that
EVERY class that defines round-trips, and a property test over the whole
set holds it to that. G-DECL-01 is strengthened; no guarantee is weakened.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | Rules (`aja/sdk/_decl/_event_wire.py`, AJA-B015 at class definition, with the field path and the fix). **Scalar unions:** `X \| None` (any allowed `X`, however pydantic spells it), or a union whose members are all `Literal` / `Enum` (or `None`) and none of which reads another member's published value -- checked by validating every member's wire values with every other member's lenient validator, so the reader returns the member the producer held in any `union_mode`, strict or not (`Literal["a"] \| Literal[1]`, `Color \| Level \| None` pass; `Literal[1] \| Literal[True]` and `IntEnum \| Literal["1"]` fail, since lax `1` reads `True` and an `IntEnum` reads `"1"`); every other union of scalars (`int \| str`, `UUID \| str`, `float \| int`, `datetime \| str`, `date \| datetime`, `Decimal \| float`) is refused, fix "use one type, or a discriminated DTO". An allowed scalar union counts as a scalar (a set of it is allowed). **Wire names:** each field's wire name is the serialization alias if set (`""` included: it is no longer read as absent), else the name; it must be non-empty, equal the key the reader looks up first, be unique in its model (case-sensitive; an `alias_generator` mapping two names to one key is caught), and, in a model that also reads by name (every event), not be another field's name (a payload without the field's own key would fill it from the other field's). **Configuration:** each model's `model_config` (the event's and every DTO's) may set only whitelisted keys -- `title`, the title generators, `json_schema_extra` (the mark stays framework-owned), `use_attribute_docstrings`, `json_schema_serialization_defaults_required`, `extra`, `frozen`, `alias_generator`, `populate_by_name` / `validate_by_name`, `validate_by_alias`, `serialize_by_alias`, `loc_by_alias`, `str_to_lower` / `str_to_upper` / `str_strip_whitespace` / `str_min_length` / `str_max_length`, `strict`, `validate_assignment` / `validate_default` / `validate_return`, `revalidate_instances`, `from_attributes`, `arbitrary_types_allowed`, `ignored_types`, `protected_namespaces`, `hide_input_in_errors`, `validation_error_cause`, `cache_strings`, `defer_build`, `plugin_settings`, and `allow_inf_nan` only `False`; the known encoding options (`ser_json_timedelta`, `ser_json_temporal`, `ser_json_inf_nan`, `ser_json_bytes`, `val_json_bytes`, `val_temporal_unit`, `coerce_numbers_to_str`, `regex_engine`, `use_enum_values`, `polymorphic_serialization`, `json_encoders`, `json_schema_mode_override`, `schema_generator`, `url_preserve_empty_path`) only at pydantic's default; any other key is refused. Decisions: the validation-only string options are idempotent, so construction and every read normalise alike; `strict` stays allowed (the reader drops it, and with disjoint unions that changes no branch); `use_enum_values` is refused because pydantic gives a plain `Enum` a custom serializer under it; `regex_engine` because the JSON-schema gate cannot follow a `python-re` pattern; `json_schema_mode_override` because the wire schema must stay the serialization schema. **Field options:** any node serializer is refused (`SerializeAsAny` publishes the runtime class's fields), so is `coerce_numbers_to_str`; a `float` (and a `Decimal` set to allow them) that may be NaN or infinite is refused -- JSON has neither and the transport (orjson) publishes `null`, which the float reader refuses; the `Event` base now sets `allow_inf_nan=False`, so an event's own floats are finite, and a DTO states it. **P3:** `from_wire` reads JSON data; a DTO instance in a payload is refused (`model_type`) where the strict class takes it as is -- no delivery carries one (the envelope's payload is decoded JSON), so the behaviour is documented and tested rather than mirrored; the reader cache is keyed by the class's core schema (`_READERS[cls] = (schema, reader)`), so a class rebuilt after a configuration change is checked again and read by a reader of its new schema on its next decode (one identity comparison per decode). **Generic guard:** `test_sdk_event_roundtrip.py` builds random Event classes with hypothesis -- every scalar with edge values (unicode and empty strings, unbounded ints, `-0.0`, extreme floats, leap-day and year-1 / 9999 datetimes naive and with fixed offsets, times with offsets, extreme timedeltas, Decimals with exponents, UUIDs, str / int / float / plain enums, literals of str, big ints, bools and enum members, `Any` JSON values), `X \| None`, allowed scalar unions, lists, variadic and fixed tuples, sets, frozensets, `dict[str, T]`, DTOs nested to any depth with random fields, aliases, alias generators, defaults (omitted or set), extra keys and random whitelisted configurations, discriminated unions -- and, in a second pool, the refused shapes and options (overlapping scalar unions in smart and left_to_right mode, empty, one-way and colliding wire names, the encoding options with a field they act on, NaN-capable floats, `coerce_numbers_to_str`). Property: a class is refused at definition (AJA-B015 with a fix), or every valid instance built by field name survives `to_wire` -> `json.dumps(allow_nan=False)` -> `from_wire` with the same value AND type at every depth (floats bit-exact, Decimals by their string, datetimes / times with their offsets, set elements typed), and the strict class's `model_validate_json` of the same JSON agrees with `from_wire`, fields set included; a class built only from allowed ingredients always defines. On the round-10 base, with the pool restricted to one item at a time, hypothesis found each: (1) a constructed `UUID` read back as `str`, and a `Decimal \| float` union turning `Decimal("Infinity")` into a float `inf` that is not JSON; (2) `serialization_alias=""`: `{"": ""}` read back as `None` / missing / refused as an extra key by the strict class; (3) two fields aliased `"w"` decoding the second field's value into the first; (4) `ser_json_temporal="milliseconds"` turning `time(0, 2)` into `120000.0` (refused) and `ser_json_timedelta="float"` turning `timedelta(microseconds=1)` into a millisecond, plus a DTO float `inf` that is not JSON; the full pool also found the base wrongly refusing a plain `Enum` under `use_enum_values` and a set of a disjoint literal union. Residual (documented, not refusable at definition): a `datetime` / `time` is published with its UTC offset to the minute, so a zone name and a sub-minute offset (historical LMT zones) are not carried; an `Any` field holds JSON values (a `UUID` put into one reads back as a `str`); an after validator runs on every read and must be idempotent; an `int` beyond 64 bits is JSON but the Redis Streams transport (orjson) cannot encode it. Cost (`from_wire`, 50k-loop best of 5, same process, round-10 lookup / round-11 lookup with the schema check): flat event 1.23 / 1.29 us, nested strict list of three 2.33 / 2.38 us | `src/tests/sdk/test_sdk_event_reader.py` (`test_round11_a_union_of_overlapping_scalars_is_refused`, `test_round11_a_disjoint_literal_union_round_trips`, `test_round11_an_empty_wire_name_is_refused`, `test_round11_colliding_wire_names_are_refused`, `test_round11_a_config_that_changes_the_wire_form_is_refused`, `test_round11_field_options_that_change_the_wire_form_are_refused`, `test_round11_the_allowed_configuration_keys_round_trip`, `test_round11_from_wire_reads_json_data_not_instances`, `test_round11_a_rebuilt_class_gets_a_fresh_reader`); `src/tests/sdk/test_sdk_event_roundtrip.py` (`test_every_event_that_defines_round_trips`, `test_every_allowed_event_defines_and_round_trips`, `test_the_property_holds_on_the_round11_repros`); `src/tests/sdk/test_sdk_schema_differential.py::test_every_refused_shape_raises_at_definition` (+13 refused shapes and fields), `::test_the_gate_agrees_with_the_real_readers` (the scalar-union entry is now `Literal["a", "b"] \| Literal[1, 2]`, plus `Color \| Level \| None`) |

Bundled modules and the example apps use none of the newly refused shapes
(no event or event DTO has a float, a scalar union, an alias collision or a
configuration beyond the whitelist); nothing was adjusted.

## Round-10 fixes (2026-10-27)

Four P2 the tenth independent review verified on `498a253e`. Three rounds
in a row found new reader edge cases because events accepted arbitrary
pydantic types, so the set of event wire shapes is now CLOSED (integrator's
decision) instead of chased case by case. G-DECL-01 is strengthened:
refusing more shapes at class definition narrows what can reach a reader;
no guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | The closed set (`aja/sdk/_decl/_event_wire.py`, checked on the class's core schema -- what pydantic validates -- at every depth when an `Event` class is defined, AJA-B015 naming the field path, the problem and the fix). Allowed: `str` / `int` / `float` / `bool` / `None` with constraints, `UUID`, `datetime` / `date` / `time` / `timedelta`, `Decimal`, an `Enum` / `Literal` of JSON scalars, `Any` as an opaque JSON value; `X \| None` and unions of scalars; `list`, variadic and fixed `tuple`, `set` / `frozenset` of scalars, `dict[str, T]`; `BaseModel` DTOs (any `extra`, `frozen`, `alias=` / an alias generator, `exclude` with a default, after validators); unions of DTOs only discriminated by a `Literal` tag field. Refused: an undiscriminated union holding a DTO or a container (any `union_mode`), a callable discriminator, a DTO overriding `__init__` / `__new__`, a `mode="before"` / `"wrap"` validator over a DTO (model or field level; on a DTO-free field it stays allowed: it cannot build a DTO or see a nested unknown key), `mode="plain"`, custom serializers (field, model, `PlainSerializer`) and computed fields, a wire name that does not round-trip (the name `to_wire` emits must be the one the reader looks up first: `serialization_alias` or `validation_alias` alone, `AliasChoices` / `AliasPath`, an alias with `validate_by_alias=False`), an excluded field without a default, dataclasses, `TypedDict`, `NamedTuple`, `RootModel`, unparametrised generics, non-JSON scalars (`bytes`, URLs, paths, IPs, `Json`), non-`str` dict keys (bare `dict` included), sets of non-scalars, a forward reference unresolved at definition. Decisions: before / wrap validators are refused only where a DTO is validated below them (the bypass needs one); `Any` is allowed as an opaque leaf (the reader passes it through, the gate's schema is `{}`); dataclasses and `TypedDict` are refused rather than supported (the set stays small, a `BaseModel` covers them). Round-10 items: (1) concurrent first decodes could cache a strict reader (the round-9 reader flipped `__pydantic_complete__` on every class while compiling, `model_rebuild()` outside the lock) -- now the reader is built at class definition and compiling it modifies no class: its `model` nodes name per-class factory classes whose `__new__` allocates the real class; pydantic-core never uses a prebuilt validator for a non-pydantic class, compiles the node from the lenient copy and fills / post-inits the real instance as for any model. No private flag is flipped, so no lock protects a mutation (one lock only de-duplicates builds); a self-check on a canary (nested unknown key ignored, lax strings read, real classes, equality, fields set, private state, the class itself still strict) runs before the first reader is stored and fails loudly naming the pydantic and pydantic-core versions. Twin `extra="ignore"` subclasses converted with `model_construct` were rejected: a conversion walk per value, after validators seeing twin instances, and `model_construct`'s alias lookup mis-reads a field whose alias is another field's name. The reader also drops `strict` everywhere (a `strict=True` DTO reads the UUID / datetime / Decimal strings its producer publishes). (2) `A \| dict[str, Any]` / `A \| B` (smart or left_to_right): refused at definition, fix: a discriminator. (3) a custom `__init__` wrapper over a strict child: every custom `__init__` / `__new__` is refused (open or strict). (4) a `serialization_alias`-only field: refused, fix `Field(alias=...)`. Also found while closing the set: a `Decimal` is published as a string with no format, so retyping it to `str` passed the gate while the old `Decimal` reader rejects the new producer's `"abc"`; the wire schema now marks it `format: decimal` (formats are compared as types). Cost (`from_wire`, 50k-loop best of 5, same machine, round-9 reader / closed-set reader): flat event 1.53 / 1.56 us, nested strict list of three 2.40 / 2.54 us, the same with a newer key at every depth 2.40 / 2.53 us (one Python `__new__` call per DTO instance) | `src/tests/sdk/test_sdk_event_reader.py` (the four repros: `test_round10_concurrent_first_decodes_cache_a_lenient_reader`, `test_round10_an_undiscriminated_union_with_a_dto_is_refused`, `test_round10_a_custom_init_or_new_is_refused_at_any_depth`, `test_round10_a_wire_name_that_does_not_round_trip_is_refused`; the strict-DTO, forward-reference and self-check tests); `src/tests/sdk/test_sdk_schema_differential.py::test_the_gate_agrees_with_the_real_readers` and `::test_the_wire_schema_is_what_the_real_reader_accepts` enumerate the whitelist (27 scalar / container shapes, 12 DTO containers, strict / lax / open / aliased / recursive / validated / nested DTOs, Decimal / UUID / Enum retypings) both directions through the real readers -- the sampler now emits format-valid strings and duplicate-free sets, and an added key's value is like its siblings' (a dictionary's values are typed; the lax reader coerces `True` to `1`, which no producer sends: AJA-R004), and at least 10 of 40 samples must read; `::test_every_refused_shape_raises_at_definition` (39 refused shapes) and `::test_every_bundled_event_is_in_the_closed_set`. Replaced (their shapes are now refused): `test_a_union_keeps_the_value_another_branch_calls_unknown`, `test_unions_nested_in_containers_ignore_unknown_keys`, `test_a_callable_discriminator_reads_a_newer_payload`, `test_aliases_read_by_every_name_the_class_accepts`, `test_strict_dataclasses_and_typed_dicts_ignore_unknown_keys`, `test_a_strict_model_with_a_custom_init_is_refused` |

Bundled modules and the four example apps already use only allowed shapes
(identity's `EventActor.ip` is `exclude=True` with a default; tenancy's
`exclude_if` fields have defaults): no event changed.

No guarantee weakened.

## Round-9 fixes (2026-10-27)

Three P2 and two P3 the ninth independent review verified on `a4d90e1e`,
each reproduced by a failing test first, then fixed at the root. G-DECL-01
is strengthened; no guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | (1-3) the round-8 reader was error-driven: it validated, collected the `extra_forbidden` / `unexpected_keyword_argument` locations, deleted those keys and validated again (at most 8 passes). (1) Extras of EVERY failed union branch were deleted from the shared input: strict `A(x=0) \| B(y=0)` read `{"x": 42, "new": 1}` as `A(x=0)` (B called `x` unknown, so the valid value vanished) and `{"x": "bad"}` as `A(x=0)` too; required-field variants were rejected. (2) Error locations are not payload paths: a discriminated `Email \| Sms` with an `email: str` field reported `('item', 'email', 'new')` and the cleanup descended into the string; `loc_by_alias=False` gave Python names that cannot address aliased input. (3) `Annotated[list[Strict], FailFast()]` with a newer key per item failed at 9 items. Root fix, structural: `from_wire` validates ONCE with a lenient twin of the class's validator, built once per class (cached): a copy of the class's core schema (definitions included) whose every `extra="forbid"` -- a node's `extra_behavior`, a model / `TypedDict` / dataclass config's `extra_fields_behavior` -- reads `"ignore"`; `extra="allow"` containers keep their extras; aliases, discriminators, validators and serializers are the class's own; no error parsing and no iteration bound. pydantic-core builds a `model` / `dataclass` node of a complete class from that class's prebuilt strict validator, ignoring the schema given (the root and every nested model alike; verified: a rewritten copy alone stays strict), so each class in the schema is marked incomplete only while the twin is compiled (under a lock, flags restored in `finally`); the twin then creates instances of the real classes, so downstream code is unchanged. Twin classes (`extra="ignore"` subclasses) were rejected: their instances are not equal to the real ones. A strict model with its own `__init__` is built by calling it with its strict validator, so no reader can ignore keys in it: an event containing one is refused at class definition (AJA-B015). In a plain union, an all-default branch accepts any object once unknown keys are ignored -- exactly what the wire schema (`anyOf` of open objects) accepts -- so `{"x": "bad"}` reads as `B()`, never as a branch with `x` deleted; required-field variants raise. Also found by the extended differential: with `json_schema_serialization_defaults_required` a `TypedDict`'s `NotRequired` key was listed as required in the wire schema (adding one failed the gate although a producer omits it); the wire schema now lists it as optional. Cost (`from_wire`, 50k-loop best of 5, same machine): flat event 2.53 -> 1.45 us, event with a nested strict list 4.04 -> 2.48 us, the same with a newer key at every depth 14.02 -> 2.45 us (one validation instead of re-validation); the twin is built once per class on its first decode | `src/tests/sdk/test_sdk_event_reader.py` (all 12 tests: the three repros, nested unions, callable discriminator, aliases, dict / tuple / recursive, dataclass / `TypedDict`, open models, validators run once and real errors raised, real classes and strict construction, custom `__init__` refused); `src/tests/sdk/test_sdk_schema_differential.py::test_the_gate_agrees_with_the_real_readers` and `::test_the_wire_schema_is_what_the_real_reader_accepts` gain 12 pairs (plain, nested and required-field unions, Literal and callable discriminators, aliases, FailFast list, tuple, recursive, dataclass, `TypedDict`): before the fix they found the required-union and `TypedDict` cases (5 failures) |
| G-DECL-01 | (P3) the live schema registry's classifier (`classify_schema_change`) built sets of `enum` members, so a list- or object-valued member crashed it (`unhashable type`), mixed types could not be sorted, and `true` equalled `1`. Members are now compared as canonical JSON values (`1.0` is `1`, `true` is not) | `src/tests/integration/test_review_hardening.py::test_enum_values_compare_as_json_values` |

Admin contracts (P3): `admin/src/api/generated.ts` lacked
`/api/v1/users/{user_id}/reset-2fa`, present in `admin/contracts/openapi.json`
(`npm run contracts:check` failed); regenerated with `npm run contracts`'s
generator (openapi-typescript 7.13.0). Why CI did not catch it: the only
gate running `contracts:check` is `admin.yml`, which `scripts/ci_local.sh`
(the merge gate) does not reproduce, and `ci.yml`'s `dashboard-checks`
filtered on `dashboard/**`, a directory that no longer exists. `ci.yml` now
has an `admin-contracts` job (path filter `admin/**`, `backend/src/aja/**`,
`backend/uv.lock`; on in full validation) running `npm run contracts:check`,
mapped in `scripts/ci_local.py` (act; the host fallback has Node), and the
dead `dashboard-checks` job is removed.

No guarantee weakened.

## Round-8 fixes (2026-10-27)

Two P2 and one P3 the eighth independent review verified on `ac686834`,
each reproduced by a failing test first, then fixed at the root, plus the
stale tutorial output it noted. G-DECL-01 is strengthened; no guarantee is
weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | (1) removing a default breaks. `tags: list[str] = Field(default_factory=list)` becoming `tags: list[str]` leaves the serialization schema's `required` unchanged (a defaulted field is always emitted), so `schema_problems` and the registry's `classify_schema_change` passed it, yet a producer from before `tags` existed sends `{"x": 1}` and the new reader (`from_wire`) rejects it. Root fix: a field required on both sides whose `x-aja-default` mark is gone is reported "lost its default" (reader direction only: adding a default is compatible, every producer of the old schema sends the field); the registry counts it as newly required and now also walks every `$defs` entry both sides share (a nested DTO's fields, an enum's values), which it never compared before | `src/tests/sdk/test_sdk_schema_differential.py::test_removing_a_default_breaks_a_producer_from_before_the_field` (literal and factory, top level and nested), `::test_the_registry_refuses_a_removed_default` (top level and `$defs`) |
| G-DECL-01 | (2) design (a), per spec §5.2 ("the same `.vN` may gain an optional field"): readers ignore unknown keys at every depth. The wire schema stripped `additionalProperties: false` from nested DTOs while `from_wire` filtered unknown keys at the top level only, so a nested `extra="forbid"` DTO gaining a factory-default field passed the gate and the OLD reader failed with `extra_forbidden` at `dto.tags`. Root fix: `from_wire` drops every key a nested strict type (model or `TypedDict` with `extra="forbid"`, dataclass) reports as unknown and validates again (bounded passes; union branch tags in the location are skipped; any other error is raised as is; `extra="allow"` models keep their extras; construction stays strict), and the wire schema strips `additionalProperties: false` at every depth, inline objects included. A runtime `extra="ignore"` override was rejected: it also drops the extras of `extra="allow"` models | `::test_an_old_strict_nested_reader_ignores_a_newer_producers_field` (nested, list, optional; extras of an open model kept; a real error below an ignored key still raised) |
| G-DECL-01 | (3) the mark is framework-owned: `Field(json_schema_extra={"x-aja-default": True})` on a required field bypassed the gate. A `json_schema_extra` that sets the mark anywhere in an Event's validation or serialization schema (a field, a nested model, a callable, a model-level extra) is refused at class definition with AJA-B015 | `::test_a_user_cannot_forge_the_default_mark` |
| G-DECL-01 | the differential with the REAL readers: 17 pairs of real Event classes (additions in strict, lax and open nested DTOs, in lists, unions and dicts; default removals literal, factory, nested and in a list; an added required and a removed field) are compared by the gate and sampled both ways through `from_wire` of the old and new class; and every such class's wire schema must accept exactly what its real reader accepts, with an undeclared key at every depth. The JSON-schema corpus now removes defaults too (`-default`) and its sampler omits defaulted fields as a producer from before them would (readers fill them). Before the fixes it found the strict nested DTO mismatch (12 schema/reader cases, 4 gate/reader pairs) and the default-removal escapes (4 pairs, plus 3 corpus pairs) | `::test_the_gate_agrees_with_the_real_readers`, `::test_the_wire_schema_is_what_the_real_reader_accepts`, `::test_a_change_the_rule_accepts_rejects_no_payload_either_way` |

Tutorial output: the finished tutorial application (`Docs/tutorial-app/tasklist`)
had none of the files `aja test` generates (module, route and contract
snapshots, the exported `tasks.item.created.v1` schema) and a stale contract
stub, and the how-to snippets' two contract stubs (`Docs/snippets/generated`)
predated the current stub renderer. Both are regenerated by the new
`scripts/docs_generated.py`, which `src/tests/architecture/test_docs_generated.py`
runs with `--check` in the backend test jobs (no workflow change).

Generated files: no bundled or example event schema changes (none has an
inline nested object with `additionalProperties: false` or a forged mark);
no snapshot reports a breaking change.

No guarantee weakened.

## Round-7 fixes (2026-10-27)

One P2 and one P3 the seventh independent review verified on `bba8e915`,
each reproduced by a failing test first, then fixed at the root. Both
statements are strengthened; no guarantee is weakened and no capability
removed.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | (1) an optional field added with a default FACTORY passes. The serialization wire schema lists every defaulted field as required (always emitted), and `schema_problems` exempted a newly required field only when its property carried a JSON `default`; `Field(default_factory=list)` has none (nor does a default pydantic cannot serialize), so adding `tags: list[str] = Field(default_factory=list)` to an event with `x: int` was reported "is required now" although spec §5.2 allows it (and the bundled `event_id` / `occurred_at` fields already had that shape). Root fix: the wire schema (`Event.wire_schema()`, a `GenerateJsonSchema` whose `default_schema` marks the field) records a default of any kind, literal or factory, as the framework annotation `"x-aja-default": true` (`aja.core.contracts.schema_registry.DEFAULTED`) on the property, at every depth; the rule's exemption reads that mark instead of the `default` key, and treats it as an annotation beside a `$ref`. The live schema registry's classifier (`classify_schema_change`, used on re-registration) read `required` alone and called the same addition breaking; it now reads the mark too. Both directions hold with the real readers: a new reader (`from_wire`) fills the field in an old payload, an old reader ignores it in a new one, and the old schema accepts the new payload; the same field added without a default still fails, top-level and nested. Contract DTOs do not go through this rule (their compatibility is the stub / `contract_compat` comparison), so nothing changes there | `src/tests/sdk/test_sdk_schema_differential.py::test_a_field_added_with_a_default_factory_is_compatible_both_ways` (list, dict, set and model factories, top-level, in a nested DTO emitted like an event and in a plain nested DTO), `::test_a_field_added_without_a_default_is_still_required_now`, `src/tests/integration/test_review_hardening.py::test_the_registry_accepts_a_field_added_with_a_default_factory` |
| G-UNIT-02 | (2) `fx.ra` is exactly the integer 1. `well_formed_call_effects` checked `value == 1 and not bool`, so `{"ra": 1.0}` was well formed; it now requires `type(value) is int and value == 1` | `src/tests/integration/test_rpc_caller_parity.py::test_an_answer_whose_effects_were_not_merged_is_in_doubt` (`{"ra": 1.0}`, `{"ra": True}` added) |

Generated files: every bundled event schema gains the mark on its
defaulted properties (`event_id`, `occurred_at`, `event_type`,
`schema_version`, `trace_id`, actor fields), purely additive: 34
`aja/shared/events/*.json`, the identity, tenancy and billing
`module.snapshot.json`, and the helpdesk example's 34 copies of the
shared event schemas. The example apps' own module snapshots are unchanged
(their events declare no defaulted field). No snapshot reports a breaking
change. The differential corpus skips the mark like `default` (dropping an
annotation is no mutation), and its one-way removal of a defaulted field
keys on the mark.

No guarantee weakened.

## Round-3 fixes (data/tooling) (2026-10-26)

Five defects the third independent review (Codex, round 3) verified on main
(`1d5cccbb`), each reproduced by a failing test first (on PostgreSQL where
the reviewer used it), then fixed at the root. Every statement is
strengthened; no guarantee is weakened and no capability removed.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | (1) every JSON-schema applicator is traversed with the narrowing rule: `propertyNames` (a dict key `min_length=1 -> 2` passed although the new reader rejects `{"values": {"x": 1}}`), `patternProperties` (a value change under a pattern fails; an added or changed key pattern fails, a removed one passes), `prefixItems` per position, `contains` / `minContains` / `maxContains`, `then` / `else`, `dependentSchemas` / `dependentRequired`, `unevaluatedItems` / `unevaluatedProperties` and `items: false`; any change of a `not` or `if` subschema fails | `src/tests/sdk/test_sdk_generated.py::test_a_tightened_dictionary_key_constraint_is_incompatible`, `::test_a_regex_keyed_dictionary_is_compared_per_pattern`, `::test_every_applicator_follows_the_narrowing_rule`, `::test_a_loosened_or_unchanged_applicator_is_compatible` |
| G-SAGA-03 | (2) saga state is tenant-scoped by the runtime itself, not only by RLS: `saga_status` and `start_saga`'s create-or-get filter on the bound tenant (none bound: only a tenant-less saga), a nudge moves an instance only of the tenant it names, and every instance read takes a required `tenant=` (`saga_store.ANY_TENANT` only on the system channel and in the harness). On a database without tenant RLS tenant B asking for A's business id got A's instance id, status and step | `src/aja/core/tests/test_sagas_ops.py::test_saga_state_never_crosses_tenants_without_rls` |
| G-MSTORE-09 | (3) a rebuild's entry never interleaves with a purge: `purge_tombstones` checks the rebuild marker, raises the watermark and deletes inside the store's purge window (relational: a per-projection `pg_advisory_xact_lock`, which `begin_rebuild` takes before counting itself; in memory: an asyncio lock). Before, the purge read "no rebuild", a rebuild started, and the purge raised the watermark and deleted under it | `src/aja/core/tests/test_projection_kernel.py::test_a_rebuild_never_enters_a_running_purge_in_memory`, `src/aja/core/tests/test_projection_watermarks_db.py::test_a_rebuild_never_enters_a_running_purge_in_the_table`, `::test_the_purge_window_holds_through_the_operations_units` |
| C-DEPLOY-09 | (4) `ensure_database` is race-safe for real: the exists check, `CREATE DATABASE` and the extensions run under an advisory lock on the maintenance database keyed by the target name, and every caller ensures `citext` / `pg_trgm` (`IF NOT EXISTS`) inside it. Before, B returned False while creator A was between `CREATE DATABASE` and the extensions, and B's tenancy migration failed on `CITEXT` | `src/aja/core/tests/test_migrate_creates_database.py::test_a_concurrent_caller_never_sees_an_uninitialized_database` |
| C-DEPLOY-07 | (5) route collection covers every route a service's app serves: `module_router(prefix="")` routes `/` and `/{slug}` were dropped (no literal prefix) and the gateway 404ed them. The generated gateway gives `/` a `location = /` and root-level parameter paths the catch-all `location /` (now generated, with explicit 404s for the internal surfaces when proxied); two services claiming the same prefix, `/` or root-level parameter paths are refused with AJA-T014 naming them; a static `location /` left outside the routes block is refused (nginx would refuse the duplicate). The template and the framework gateway moved the fallback into the generated block | `src/aja/core/tests/test_scaffold_deploy.py::test_a_root_level_module_gets_the_root_and_the_catch_all`, `::test_two_services_claiming_root_level_paths_conflict`, `::test_a_hand_written_catch_all_outside_the_block_is_refused` |
| C-DEPLOY-08 | (5) the split check compares the gateway with every route the service's app serves (root-level ones included), not the prefix-filtered topology, so a gateway that drops `/` or `/{slug}` fails AJA-T014 | `src/aja/core/tests/test_scaffold_deploy.py::test_the_checker_finds_a_root_route_the_gateway_drops` |

## Partial-commit metric and nested commit order (2026-10-26)

`Docs/concepts/write-admission.md` rule 4 (and spec §6.1, the multistore
design) promised that a best-effort-1PC partial commit is counted in
`aja_multi_store_partial_commit_total{route,store}`; nothing emitted it.
Reproduced by a failing test first (a real PostgreSQL COMMIT failure of the
write store, a deferred unique constraint, after the secondary committed).

| id | change | enforced by |
| --- | --- | --- |
| G-MSTORE-03 | strengthened: on an `allow_multi_db_write` route, a unit's commit (its `before_commit` checks or `COMMIT`) failing after a writing unit of the same request committed increments `aja_multi_store_partial_commit_total{route,store}` once per committed store (`route` is the route template, never the raw path; `store` a configured database name: bounded labels) and logs a `uow.partial_commit` warning naming the route, the committed stores and the failing store. A request that commits everything, or nothing (the secondary's own commit fails, the write store rolls back), is never counted | `src/tests/integration/test_route_runtime.py::test_a_write_store_commit_failing_after_the_secondary_committed_is_counted` |
| G-MSTORE-03 | strengthened: the secondaries commit before the write store however the sessions are nested in dependencies. A dependency injecting `(analytics, primary)` next to the endpoint's own `analytics` committed `['default', 'analytics']` (verified on 1d5cccbb with real PostgreSQL): the startup reorder moved that dependency but not its children. It now orders the whole dependant tree: at each level every branch opening the write store precedes the first branch opening only secondaries, recursively into each branch opening both, so FastAPI's depth-first solve (cached sub-dependencies once) reaches the write store's session first and unwinds it last | `src/tests/integration/test_route_runtime.py::test_the_secondary_commits_before_the_write_store_whatever_the_order` (nested variants: a dependency opening secondary-then-store or store-then-secondary, after or before the endpoint's own secondary, and two levels deep) |

No guarantee weakened.

## Round-4 fixes (schema/gateway/saga) (2026-10-26)

Three defects the fourth independent review verified (P2), each reproduced
by a failing test first, then fixed at the root. Every statement is
strengthened; no guarantee is weakened.

| id | change | enforced by |
| --- | --- | --- |
| G-DECL-01 | (B) `required` is compared in every subschema, with or without `properties`: `dependentSchemas={"promo": {"required": ["total"]}}` on a `dict` field returned no problem although the framework validator rejects the old producer's `{"values": {"promo": 1}}`. An added property is compared with the schema that governed that key on the old side (a dictionary's `additionalProperties` or matching `patternProperties`, `{}` below an overlaying applicator: `allOf`, `then` / `else`, `contains`, `dependentSchemas`); only a DTO (declared `properties`, no schema for other keys, not under an overlay) may gain an optional field. A differential test now holds the rule to the validator: 8,494 schema pairs (three real pydantic wire schemas, every narrowing and loosening keyword at every subschema position and under every applicator, both directions) plus 300 hypothesis-composed mutation chains; for each pair the rule accepts, payloads sampled from the old schema must pass the new one. Against the round-3 rule it found 118 escapes in five classes, all fixed: keywords beside a `$ref` dropped by the resolution (`{"$ref": X, "minProperties": 3}`, every applicator placed there), `required` without `properties`, a property added to a dictionary or under an overlay, a removed `patternProperties` entry whose keys fall back to a narrower `additionalProperties`; and removing a `not`, or an `if` left without `then` / `else`, is now accepted (it only loosens) | `src/tests/sdk/test_sdk_generated.py::test_a_constraint_on_a_node_without_properties_is_compared`, `::test_a_constraint_beside_a_ref_and_a_removed_key_pattern_are_compared`, `src/tests/sdk/test_sdk_schema_differential.py::test_a_change_the_rule_accepts_rejects_no_old_payload`, `::test_composed_changes_the_rule_accepts_reject_no_old_payload`, `::test_the_corpus_covers_every_applicator_position` |
| C-DEPLOY-07 | (C) the generated gateway bounds each route prefix at its segment, as Starlette matches: `location = /shop` and `location /shop/` instead of an unbounded `location /shop`, which sent `/shopping` (home's `/{slug}`) to store. The exact location goes to the service whose routes match the bare prefix (identity serves only `/auth/...`, so `/auth` is the catch-all's `/{slug}`) and keeps nginx from 301-ing it. The framework gateway is regenerated; AJA-T014 conflict detection is unchanged | `src/aja/core/tests/test_scaffold_deploy.py::test_a_route_prefix_matches_only_its_own_segment`, `::test_a_split_app_passes_the_framework_split_invariants`, `::test_the_framework_profile_is_the_generators_output` |
| C-DEPLOY-08 | (C) the gateway check samples adversarial paths: each path parameter also takes every literal segment some route has at that position, bare and suffixed (`/shop`, `/shopx` for `/{slug}`), and the gateway's upstream must be the service whose app routes match the path (Starlette `compile_path`, the most literal route winning). Before, only `x` was substituted, so an unbounded prefix passed with no conflict and no problem | `src/aja/core/tests/test_scaffold_deploy.py::test_the_checker_samples_paths_that_collide_with_another_prefix` |
| G-SAGA-03 | (F) `saga_status` outside a unit read the tenant before opening its own unit, which binds `SINGLE_TENANT_ID` under `TENANCY_MODE=single`: the lookup used `None` and a canonical-tenant saga read as absent. The tenant is now read once the unit is open. `start_saga` (needs the caller's unit, tenant already bound), the nudge path and the operator commands (the row's or the nudge's tenant) were audited and do not capture before binding | `src/aja/core/tests/test_sagas_ops.py::test_single_tenant_status_and_start_see_the_canonical_tenants_saga` |

No guarantee weakened.

## Test isolation (2026-10-26)

An external review found the suite order-dependent: a large serial selection
gave 1899 passed / 16 failed while the same files alone passed. The gates
(`aja test --full`, CI) ran xdist with a fixed `loadfile` distribution, which
hid the leaks. Every test must now pass alone and in any order, serially or
in parallel.

How the leaks were found (base `213abcee`): the full suite serially in
default order (4484 passed, 3 errors: Redis connect timeouts under machine
load, not reproducible); every one of the 389 test files alone (5 files
failed); the full suite serially in a seeded random order (seeds 11 and 22:
7 and 5 failures); the four example apps in five seeded orders each (linkly,
helpdesk, projecthub, academy failed). Each class is fixed at the root:

| class | tests that failed | root cause | fix |
| --- | --- | --- | --- |
| baseline runtime | `test_api_robustness.py` (8 idempotency tests), `test_revocation_windows.py` (4), `test_revocation_immediate_and_corrupt.py` (6), `test_transport.py::test_consumer_inbox_dedupes_on_event_id`, `integration/test_review_hardening.py::test_rate_limit_key_gets_ttl_atomically`; in the examples every `client` test after a module without one (AJA-R003 "missing schema", "Infrastructure registry not bound") | a test that resolves the process's active core / infra (the idempotency cache, the revocation channel's schema, the rate limiter) without asking for `aja_app` passed only after a test that built the harness app; the module-isolation baseline was "the state when first asked", i.e. unbound when the first module had no `aja_app` | the harness builds the composition-root app before the first module (`harness_app`) and every module starts from its binding (`aja.core.testing.fixtures._aja_isolate_module_globals`) |
| core registrations | `identity/tests/test_contract_diff_endpoint.py` (`new_channel` after `existing_*`, `existing_breaking` alone), `tenancy/tests/test_declared_operations.py` (3, after `test_unsupported_operation_is_general_with_the_reason_logged`) | a test registered a probe channel / a startup task and a declared operation on the session app and never removed it; another test relied on a channel registered by an earlier one | the harness rewinds the bound core after every test (`CoreFacade.snapshot_registrations` / `restore_registrations`, which now also cover the planner's declared operations); the diff tests register their baseline in their own fixture |
| class-level flags | `test_tenant_rls_paths.py::test_tenant_rls_off_changes_nothing`, `::test_hook_installs_no_engine_events` | `event.listen(Engine, ...)` sets `Engine._has_events` and `event.remove` never clears it (`integration/test_route_runtime.py`'s commit-order hook did not restore it) | the harness puts SQLAlchemy's `Engine` / `Connection` `_has_events` back after every test (`isolation.CLASS_FLAGS`) |
| process caches | `test_contract_cache_shadow.py::test_real_contract_reads_feed_the_request_and_stale_counters[rpc]` (fails alone), `::test_off_path_never_reaches_the_bookkeeping[rpc]`, `test_rpc_pair_mac.py::test_downgrades_are_refused` (fails alone) | the RPC client's described-contract cache is process-wide: an undescribed method's first call counts as a possible write (signed, never measured), so the tests passed only after an earlier RPC test described the contract | the harness empties the cache after every test (`isolation.PROCESS_CACHES`), so every test starts cold, as alone; the shadow tests describe the method first (shadow off), the downgrade test strips every credential header instead of relying on what the recorded call carried |
| definition state | `test_sagas_protocol.py::test_start_publishes_the_first_nudge_in_the_callers_transaction` (fails alone) | the test read `ENROLL.saga_type` (known once registered) before `saga_harness.start` registered it; an earlier test of the module had registered the module-level definition | read it after `start` |
| settings re-read | `integration/test_units_and_admission.py::test_a_session_passed_to_another_task_is_detected[delete]` / `[merge]` (found by the shuffled xdist CI legs, seeds 56014930 / 81845222) | the `owner_checks` fixture set the module flag `write_admission._owner_checks` but not `DB_SESSION_OWNER_CHECKS`; `install_write_admission` re-reads the setting whenever an engine is installed, so an engine first built inside the test (no earlier test had built it) switched the checks back off | the fixture sets the setting too |
| fresh database | `test_pipelined_dispatch.py` (11 tests; found by the new `backend-tests-isolation` job, seed 5612450, on CI's fresh database) | the test creates the shared metadata itself (`metadata.create_all`, `citext` columns); a serial run's database got the framework's extensions only from the first `db_engine` (or an xdist worker's `ensure_database`), so the file passed only after a test that used `db_engine` | a serial run's database is prepared at session start like a worker's (`session_checks._prepare_serial_database`: created if missing, extensions installed) |
| in-file order | `test_testing_isolation.py::test_step_1_*` / `test_step_2_*` | step 2 asserted on state step 1 left in a module global | both run their two steps in a fixed order in a `pytest` subprocess on a tiny project (the real plugin fixtures) |

The guard (`aja.core.testing.shuffle`): `--aja-shuffle=<seed>|random` orders
the files, then the tests inside each file, by a seeded hash of their node
ids (replayable on any host; the seed is printed in the header and the
summary).

- `aja test` shuffles every test step with a fresh seed, printed before the
  steps and after a failure (`--seed N` replays, `--no-shuffle` keeps file
  order): zero extra cost.
- CI `backend-tests` (all four legs) and `examples` pass `--aja-shuffle=random`.
- The new CI job `backend-tests-isolation` (ci_local job of the same name)
  runs the whole framework suite serially in one process in a fresh order:
  about 10-11 minutes on the shared development machine (seeds 101 and 202:
  676 s and 693 s), 18 minutes under heavy load.

| id | change | enforced by |
| --- | --- | --- |
| G-OPS-10 | strengthened: every test module starts from the harness app's runtime whatever ran before; what a test registers on the bound core is rewound after it; SQLAlchemy's class-level event flags are put back and the RPC client's description caches emptied; the test steps of `aja test` and CI run in a seeded random order, and CI runs the whole framework suite serially in one shuffled process. `test_step_1` / `test_step_2` now run their steps in a subprocess (fixed order) instead of relying on file order | `src/aja/core/tests/test_testing_isolation.py::test_step_1_a_test_builds_its_own_app`, `::test_step_2_the_next_test_gets_the_previous_runtime_back` (a module without `aja_app`, a module with its own app, the next module), `::test_a_registered_channel_is_gone_after_the_test`, `::test_class_flags_and_process_caches_are_put_back`, `::test_restore_puts_back_identity_and_content`, `src/aja/core/tests/test_testing_shuffle.py::test_a_seed_fixes_the_order_and_is_printed_for_replay`, `::test_files_stay_contiguous_and_their_tests_are_shuffled`, `src/aja/core/tests/test_run_tests_cli.py::test_the_test_steps_run_in_a_seeded_random_order` |

No guarantee weakened.

## Timestamps and ids (2026-10-27)

The final DX eval found a flaky list test: a hand-written model copied
`server_default=func.now()` and `default=uuid.uuid4`. `now()` is the
transaction's start, so the rows of one transaction (every test: the
`client` fixture runs a test in one) tie, and random ids order them at
random. The owner rejected an `aja check` rule and approved a framework
idiom instead: `new_id` (UUIDv7) and `CreatedAt` join the beginner tier
(29 names), `UpdatedAt` is advanced (`aja.sdk.data`). The generator, the
bundled modules (identity, tenancy, billing: one default-only revision
each), the four examples (one revision per module), the DX seed app and the
tutorial app use them; `aja docs recipe list-order` and
`Docs/concepts/modules.md` explain them.

| id | change | enforced by |
| --- | --- | --- |
| C-SDK-06 | new: `new_id()` is an RFC 9562 UUIDv7, strictly increasing in a process (same-millisecond burst, clock step back, full counter) and time-ordered across processes; `CreatedAt` / `UpdatedAt` default to `clock_timestamp()`, compose with `TenantScoped`, autogenerate as `sa.text('clock_timestamp()')`; rows of one transaction list in creation order; the bundled chains migrate the defaults | `src/tests/sdk/test_sdk_columns.py` (10 tests), `src/tests/integration/test_migrations_per_chain.py::test_chains_reproduce_orm_schema_exactly` (now also compares every `clock_timestamp()` default with the migrated database) |
| C-GEN-11 | strengthened: the generated model uses `default=new_id` and `created_at: Mapped[CreatedAt]` | `test_sdk_generators.py::test_add_module_lists_newest_first_deterministically_in_one_transaction` (asserts the idiom), `test_sdk_columns.py::test_rows_of_one_transaction_list_in_creation_order` |
| G-SDK-04 | the beginner tier is 29 names (was 27); test renamed `test_the_beginner_tier_is_the_29_names`, which also pins `new_id` and `CreatedAt` in it | `test_sdk_generators.py::test_the_beginner_tier_is_the_29_names` and the three tier tests it had |

No guarantee weakened.

## Example apps pass their own gate (2026-10-27)

`aja test --full` (and `--ci`) failed inside example apps and the tutorial
app while the CI `examples` job stayed green: it ran `aja check`, ruff
check, pytest and the generated-files check, never the app's own gate. The
failures, each fixed at its root:

- projecthub `0004_taskboard` built its index names with f-strings in a
  loop; AJA-T002 reads revisions as source, so it did not see them. The
  revision now names each index literally, as autogenerate writes it (the
  only such revision in the repository); the AJA-T002 message and
  `aja explain AJA-T002` say a revision names each index literally.
- the tutorial app's stats baseline created a plain `tenant_id` index the
  model no longer declares (its unique key leads with `tenant_id`, so
  `TenantScoped` drops the index): AJA-T006 drift. The tutorial shows the
  baseline verbatim and has the reader edit it before the first deploy, so
  the baseline drops the index and the text says why.
- academy's two contract stubs failed `ruff format`: `render_stub` counted
  `: ...` against the line length even when a docstring body follows (the
  line ends in `:`), and wrapped a long reference declaration only one way.
  It now emits `ruff format`'s own layout; every stub is regenerated.
- `aja test --ci` ran `ruff check` without `--no-fix`, so an app's
  `fix = true` fixed a violation in CI and passed: `--ci` now never fixes.

The CI `examples` job now runs each app's own `aja test --ci --full` (it
replaces the `aja check`, generated-files, lint and pytest steps, which it
subsumes) and the tutorial app joins its matrix; `Docs/tutorial-app/**`
triggers it.

| id | change | enforced by |
| --- | --- | --- |
| C-GEN-14 | new: every example app and the tutorial app pass `aja test --ci --full` in the CI `examples` job, whose matrix lists every such app; `--ci` never fixes; a generated stub is exactly what `ruff format` writes | `src/tests/architecture/test_ci_local_runner.py::test_every_example_and_the_tutorial_app_run_their_own_gate_in_ci`, `src/aja/core/tests/test_run_tests_cli.py::test_ci_never_writes_so_ruff_check_never_fixes`, `src/tests/sdk/test_sdk_generated.py::test_a_rendered_stub_is_what_ruff_format_writes` (six line-length edges), `::test_a_generated_stub_is_ruff_formatted`, CI job `examples` |

No guarantee weakened.
