### dx-wave1-2026-11-24 (app mode)

| task | outcome | gates failed | fn checks | dev checks failed | uncaught (gates green) | tokens | turns | tools | min | kernel files read | edited outside |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | pass | — | 2/2 | — | — | 508,510 | 11 | 10 | 4.8 | 0 | 2 |
| t1_notes | pass | — | 4/4 | — | — | 668,316 | 13 | 12 | 4.7 | 0 | 3 |
| t2_welcome | pass | — | 3/3 | — | — | 1,506,258 | 25 | 24 | 7.6 | 2 | 2 |
| t3_quotas | pass | — | 4/4 | — | — | 1,471,397 | 22 | 21 | 6.6 | 0 | 3 |
| t4_resend_invitation | pass | — | 3/3 | — | — | 551,966 | 11 | 10 | 5.3 | 0 | 2 |
| t5_billing_contacts | pass | — | 2/2 | — | — | 514,074 | 11 | 10 | 3.6 | 0 | 2 |
| t6_team_overview | pass | — | 2/2 | sources_both_modules | sources_both_modules | 689,884 | 13 | 12 | 3.3 | 0 | 2 |
| t7_credits_event_v2 | pass | — | 3/3 | — | — | 403,923 | 9 | 8 | 4.7 | 0 | 2 |
| t8_purge_invitations | pass | — | 2/2 | at_least_once_opt_in | at_least_once_opt_in | 259,774 | 6 | 5 | 3.0 | 0 | 2 |
| t9_member_export | pass | — | 2/2 | — | — | 388,333 | 9 | 8 | 3.2 | 0 | 2 |

**dev checks failed: 2** over 10 tasks that ran (uncaught with green gates: 2)

Summary: mode=app, tasks=10, tasks_ran=10, errored=[], passed=10, dev_checks_failed=2, tasks_free_of_dev_failures=8, uncaught_deviations=2, tasks_free_of_uncaught_deviation=8, mean_tokens_total=696243, mean_turns=13.0, mean_tool_calls=12.0, mean_wall_min=4.7, mean_kernel_files_read=0.2, kernel_reads_total=2, framework_edits_total=0, other_module_edits_total=0, mean_edited_outside=2.2, tier_metrics_runs=10, mean_docs_lookups=3.2, mean_recipes_read=2.1, mean_skills_read=0.1, mean_first_green_turn=11.7, first_green_runs=10, t009_failures_total=0, advanced_markers_total=29, mean_tier_misses=2.6, tasks_that_created_a_dotenv=0, total_cost_usd=3.04

### dx-wave1-2026-11-24: tier and skills metrics

| run | docs lookups | names | recipes | skills read | first green turn | T009 hits | advanced markers | tier misses |
|---|---:|---|---|---|---:|---:|---:|---|
| t10_org_banner | 2 | `WallClock`, `record_audit` | `org-gate` | — | 9 | 0 | 4 | `sandloom.sdk.data.WallClock`, `sandloom.sdk.data.disable_tenant_rls`, `sandloom.sdk.data.enable_tenant_rls`, `sandloom.sdk.migrations.run_module_migrations` |
| t1_notes | 0 | — | `list-order`, `org-gate` | — | 12 | 0 | 3 | `sandloom.sdk.data.disable_tenant_rls`, `sandloom.sdk.data.enable_tenant_rls` |
| t2_welcome | 14 | `$n`, `Notification`, `Notifier`, `as_user`, `contracts`, `deliver`, `events`, `fake_contract`, `identity.contact.v1`, `identity.user_registered.v1`, `notifier`, `on`, `sandloom.sdk.effects`, `tenant_scope_exempt` | `call-contract`, `retries`, `test-service-or-task` | — | 23 | 0 | 4 | `sandloom.sdk.data.WallClock`, `sandloom.sdk.infra.Notification`, `sandloom.sdk.infra.notifier`, `sandloom.sdk.migrations.run_module_migrations`, `sandloom.sdk.module.ContractRequirement` |
| t3_quotas | 8 | `ConflictInsert`, `OrgRole`, `TenantScoped`, `WallClock`, `billing.credits.v1`, `conflict_insert`, `current_tenant_id`, `tenancy.membership.v1` | `call-contract`, `org-gate`, `retries` | — | 21 | 0 | 7 | `sandloom.sdk.data.disable_tenant_rls`, `sandloom.sdk.data.enable_tenant_rls`, `sandloom.sdk.migrations.run_module_migrations`, `sandloom.sdk.module.ContractRequirement`, `sandloom.sdk.module.settings_of`, `sandloom.sdk.tenant.current_tenant_id` |
| t4_resend_invitation | 2 | `bind_tenant` | `org-gate`, `retries`, `test-service-or-task` | — | 10 | 0 | 0 | — |
| t5_billing_contacts | 2 | `sandloom.sdk.errors` | `list-order`, `org-gate` | — | 10 | 0 | 1 | `sandloom.sdk.data.disable_tenant_rls`, `sandloom.sdk.data.enable_tenant_rls` |
| t6_team_overview | 3 | `billing.entitlements.v1`, `contracts`, `tenancy.member_added.v2` | `list-order`, `org-gate`, `projection-from-platform-events` | — | 12 | 0 | 4 | `sandloom.sdk.data.disable_tenant_rls`, `sandloom.sdk.data.enable_tenant_rls`, `sandloom.sdk.migrations.run_module_migrations`, `sandloom.sdk.tenant.current_tenant_id` |
| t7_credits_event_v2 | 0 | — | `event-v2` | `sandloom-event-version-roll` | 8 | 0 | 1 | `sandloom.sdk.events.publish_event_versions` |
| t8_purge_invitations | 0 | — | `periodic-task`, `test-service-or-task` | — | 5 | 0 | 4 | `sandloom.sdk.module.CapabilityRequirement`, `sandloom.sdk.tenant.unscoped` |
| t9_member_export | 1 | `Account` | `org-gate` | — | 7 | 0 | 1 | — |

One run per task in dx-wave1-2026-11-24: one sample cannot separate a change from run-to-run variation (`run --runs 3`).

### dx-wave1-2026-11-24: doc gaps

**kernel reads: 2 (target 0)**: other kernel 1, testing kit 1

| task | kernel reads (path → topic; ✗ = the path does not exist) | framework edits | other-module edits | wiring | rule | docs | other |
|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t1_notes | — | 0 | 0 | 0 | 0 | 0 | 3 |
| t2_welcome | `sandloom/core/infra/contracts.py` → other kernel<br>`sandloom/core/testing/events.py` → testing kit | 0 | 0 | 0 | 0 | 0 | 2 |
| t3_quotas | — | 0 | 0 | 0 | 0 | 0 | 3 |
| t4_resend_invitation | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t5_billing_contacts | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t6_team_overview | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t7_credits_event_v2 | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t8_purge_invitations | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t9_member_export | — | 0 | 0 | 0 | 0 | 0 | 2 |

### merged-main-2026-11-06 (same tasks) (app mode)

| task | outcome | gates failed | fn checks | dev checks failed | uncaught (gates green) | tokens | turns | tools | min | kernel files read | edited outside |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | pass | — | 2/2 | — | — | 1,234,548 | 22 | 20 | 10.9 | 0 | 2 |
| t1_notes | pass | — | 4/4 | — | — | 2,633,909 | 38 | 34 | 10.3 | 0 | 3 |
| t2_welcome | pass | — | 3/3 | — | — | 1,358,919 | 23 | 21 | 13.9 | 1 | 3 |
| t3_quotas | pass | — | 4/4 | — | — | 1,936,281 | 28 | 27 | 10.8 | 0 | 3 |
| t4_resend_invitation | pass | — | 3/3 | — | — | 860,948 | 17 | 15 | 13.3 | 0 | 2 |
| t5_billing_contacts | pass | — | 2/2 | — | — | 457,670 | 10 | 9 | 6.0 | 0 | 2 |
| t6_team_overview | pass | — | 2/2 | sources_both_modules | sources_both_modules | 751,174 | 16 | 15 | 9.7 | 0 | 2 |
| t7_credits_event_v2 | pass | — | 3/3 | — | — | 804,165 | 15 | 14 | 9.8 | 0 | 2 |
| t8_purge_invitations | pass | — | 2/2 | — | — | 346,976 | 8 | 7 | 4.4 | 0 | 1 |
| t9_member_export | fail | ci: generated files, sandloom-test | 2/2 | gate_checks_the_organization | — | 601,807 | 13 | 11 | 12.5 | 0 | 2 |

**dev checks failed: 2** over 10 tasks that ran (uncaught with green gates: 1)

Summary: mode=app, tasks=10, tasks_ran=10, errored=[], passed=9, dev_checks_failed=2, tasks_free_of_dev_failures=8, uncaught_deviations=1, tasks_free_of_uncaught_deviation=8, mean_tokens_total=1098639, mean_turns=19.0, mean_tool_calls=17.3, mean_wall_min=10.2, mean_kernel_files_read=0.1, kernel_reads_total=1, framework_edits_total=0, other_module_edits_total=0, mean_edited_outside=2.2, tier_metrics_runs=0, mean_docs_lookups=None, mean_recipes_read=None, mean_skills_read=None, mean_first_green_turn=None, first_green_runs=0, t009_failures_total=None, advanced_markers_total=None, mean_tier_misses=None, tasks_that_created_a_dotenv=0, total_cost_usd=3.89

| metric | merged-main-2026-11-06 | dx-wave1-2026-11-24 |
|---|---:|---:|
| mode | app | app |
| tasks | 10 | 10 |
| tasks_ran | 10 | 10 |
| errored | [] | [] |
| passed | 9 | 10 |
| dev_checks_failed | 2 | 2 |
| tasks_free_of_dev_failures | 8 | 8 |
| uncaught_deviations | 1 | 2 |
| tasks_free_of_uncaught_deviation | 8 | 8 |
| mean_tokens_total | 1098639 | 696243 |
| mean_turns | 19.0 | 13.0 |
| mean_tool_calls | 17.3 | 12.0 |
| mean_wall_min | 10.2 | 4.7 |
| mean_kernel_files_read | 0.1 | 0.2 |
| kernel_reads_total | 1 | 2 |
| framework_edits_total | 0 | 0 |
| other_module_edits_total | 0 | 0 |
| mean_edited_outside | 2.2 | 2.2 |
| tier_metrics_runs | 0 | 10 |
| mean_docs_lookups | None | 3.2 |
| mean_recipes_read | None | 2.1 |
| mean_skills_read | None | 0.1 |
| mean_first_green_turn | None | 11.7 |
| first_green_runs | 0 | 10 |
| t009_failures_total | None | 0 |
| advanced_markers_total | None | 29 |
| mean_tier_misses | None | 2.6 |
| tasks_that_created_a_dotenv | 0 | 0 |
| total_cost_usd | 3.89 | 3.04 |
