### infra-portable-2026-11-05 (app mode)

| task | outcome | gates failed | fn checks | dev checks failed | uncaught (gates green) | tokens | turns | tools | min | kernel files read | edited outside |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | fail | module-tests | 2/2 | — | — | 584,271 | 12 | 11 | 8.2 | 0 | 15 |
| t1_notes | fail | module-tests | 4/4 | — | — | 1,083,322 | 21 | 19 | 10.8 | 0 | 15 |
| t2_welcome | fail | module-tests | 3/3 | — | — | 2,077,405 | 33 | 29 | 10.5 | 0 | 16 |
| t3_quotas | pass | — | 4/4 | — | — | 9,920,922 | 100 | 82 | 16.8 | 0 | 3 |
| t4_resend_invitation | pass | — | 3/3 | — | — | 816,195 | 16 | 15 | 12.2 | 0 | 2 |
| t5_billing_contacts | pass | — | 2/2 | — | — | 912,793 | 19 | 17 | 8.3 | 0 | 2 |
| t6_team_overview | fail | module-tests | 2/2 | sources_both_modules | — | 817,349 | 15 | 13 | 18.0 | 0 | 15 |
| t7_credits_event_v2 | pass | — | 3/3 | — | — | 410,839 | 9 | 8 | 10.0 | 0 | 2 |
| t8_purge_invitations | pass | — | 2/2 | — | — | 316,695 | 7 | 6 | 3.9 | 0 | 1 |
| t9_member_export | pass | — | 2/2 | — | — | 566,419 | 13 | 11 | 5.6 | 0 | 2 |

**dev checks failed: 1** over 10 tasks that ran (uncaught with green gates: 0)

Summary: mode=app, tasks=10, tasks_ran=10, errored=[], passed=6, dev_checks_failed=1, tasks_free_of_dev_failures=6, uncaught_deviations=0, tasks_free_of_uncaught_deviation=6, mean_tokens_total=1750621, mean_turns=24.5, mean_tool_calls=21.1, mean_wall_min=10.4, mean_kernel_files_read=0.0, kernel_reads_total=0, framework_edits_total=0, other_module_edits_total=53, mean_edited_outside=7.3, tasks_that_created_a_dotenv=0, total_cost_usd=4.59

### infra-portable-2026-11-05: doc gaps

**kernel reads: 0 (target 0)**

| task | kernel reads (path → topic; ✗ = the path does not exist) | framework edits | other-module edits | wiring | rule | docs | other |
|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | — | 0 | 13 | 0 | 0 | 0 | 2 |
| t1_notes | — | 0 | 13 | 0 | 0 | 0 | 2 |
| t2_welcome | — | 0 | 14 | 0 | 0 | 0 | 2 |
| t3_quotas | — | 0 | 0 | 0 | 0 | 0 | 3 |
| t4_resend_invitation | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t5_billing_contacts | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t6_team_overview | — | 0 | 13 | 0 | 0 | 0 | 2 |
| t7_credits_event_v2 | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t8_purge_invitations | — | 0 | 0 | 0 | 0 | 0 | 1 |
| t9_member_export | — | 0 | 0 | 0 | 0 | 0 | 2 |

### main-2026-11-05 (same tasks) (app mode)

| task | outcome | gates failed | fn checks | dev checks failed | uncaught (gates green) | tokens | turns | tools | min | kernel files read | edited outside |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | fail | module-tests | 2/2 | — | — | 1,230,375 | 22 | 20 | 14.0 | 0 | 13 |
| t1_notes | fail | module-tests | 4/4 | — | — | 1,033,548 | 19 | 17 | 11.9 | 0 | 12 |
| t2_welcome | fail | module-tests | 3/3 | — | — | 1,852,807 | 30 | 29 | 11.9 | 1 | 14 |
| t3_quotas | pass | — | 4/4 | — | — | 1,225,234 | 22 | 21 | 12.3 | 0 | 2 |
| t4_resend_invitation | pass | — | 3/3 | — | — | 492,727 | 12 | 11 | 3.6 | 0 | 2 |
| t5_billing_contacts | pass | — | 2/2 | — | — | 459,923 | 10 | 9 | 3.8 | 0 | 2 |
| t6_team_overview | fail | module-tests | 2/2 | sources_both_modules | — | 427,405 | 9 | 8 | 3.7 | 0 | 15 |
| t7_credits_event_v2 | pass | — | 3/3 | — | — | 734,276 | 15 | 14 | 7.4 | 1 | 2 |
| t8_purge_invitations | pass | — | 2/2 | — | — | 295,055 | 7 | 6 | 4.3 | 0 | 1 |
| t9_member_export | pass | — | 2/2 | — | — | 396,220 | 9 | 8 | 3.8 | 0 | 2 |

**dev checks failed: 1** over 10 tasks that ran (uncaught with green gates: 0)

Summary: mode=app, tasks=10, tasks_ran=10, errored=[], passed=6, dev_checks_failed=1, tasks_free_of_dev_failures=6, uncaught_deviations=0, tasks_free_of_uncaught_deviation=6, mean_tokens_total=814757, mean_turns=15.5, mean_tool_calls=14.3, mean_wall_min=7.7, mean_kernel_files_read=0.2, kernel_reads_total=4, framework_edits_total=0, other_module_edits_total=45, mean_edited_outside=6.5, tasks_that_created_a_dotenv=0, total_cost_usd=3.19

| metric | main-2026-11-05 | infra-portable-2026-11-05 |
|---|---:|---:|
| mode | app | app |
| tasks | 10 | 10 |
| tasks_ran | 10 | 10 |
| errored | [] | [] |
| passed | 6 | 6 |
| dev_checks_failed | 1 | 1 |
| tasks_free_of_dev_failures | 6 | 6 |
| uncaught_deviations | 0 | 0 |
| tasks_free_of_uncaught_deviation | 6 | 6 |
| mean_tokens_total | 814757 | 1750621 |
| mean_turns | 15.5 | 24.5 |
| mean_tool_calls | 14.3 | 21.1 |
| mean_wall_min | 7.7 | 10.4 |
| mean_kernel_files_read | 0.2 | 0.0 |
| kernel_reads_total | 4 | 0 |
| framework_edits_total | 0 | 0 |
| other_module_edits_total | 45 | 53 |
| mean_edited_outside | 6.5 | 7.3 |
| tasks_that_created_a_dotenv | 0 | 0 |
| total_cost_usd | 3.19 | 4.59 |
