### merged-main-2026-11-06 (app mode)

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

Summary: mode=app, tasks=10, tasks_ran=10, errored=[], passed=9, dev_checks_failed=2, tasks_free_of_dev_failures=8, uncaught_deviations=1, tasks_free_of_uncaught_deviation=8, mean_tokens_total=1098639, mean_turns=19.0, mean_tool_calls=17.3, mean_wall_min=10.2, mean_kernel_files_read=0.1, kernel_reads_total=1, framework_edits_total=0, other_module_edits_total=0, mean_edited_outside=2.2, tasks_that_created_a_dotenv=0, total_cost_usd=3.89

### merged-main-2026-11-06: doc gaps

**kernel reads: 1 (target 0)**: other kernel 1

| task | kernel reads (path → topic; ✗ = the path does not exist) | framework edits | other-module edits | wiring | rule | docs | other |
|---|---|---:|---:|---:|---:|---:|---:|
| t10_org_banner | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t1_notes | — | 0 | 0 | 0 | 0 | 0 | 3 |
| t2_welcome | `sandloom/core/infra/contracts.py` → other kernel | 0 | 0 | 0 | 0 | 0 | 3 |
| t3_quotas | — | 0 | 0 | 0 | 0 | 0 | 3 |
| t4_resend_invitation | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t5_billing_contacts | — | 0 | 0 | 0 | 0 | 0 | 2 |
| t6_team_overview | — | 0 | 0 | 0 | 0 | 0 | 2 |
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

| metric | main-2026-11-05 | merged-main-2026-11-06 |
|---|---:|---:|
| mode | app | app |
| tasks | 10 | 10 |
| tasks_ran | 10 | 10 |
| errored | [] | [] |
| passed | 6 | 9 |
| dev_checks_failed | 1 | 2 |
| tasks_free_of_dev_failures | 6 | 8 |
| uncaught_deviations | 0 | 1 |
| tasks_free_of_uncaught_deviation | 6 | 8 |
| mean_tokens_total | 814757 | 1098639 |
| mean_turns | 15.5 | 19.0 |
| mean_tool_calls | 14.3 | 17.3 |
| mean_wall_min | 7.7 | 10.2 |
| mean_kernel_files_read | 0.2 | 0.1 |
| kernel_reads_total | 4 | 1 |
| framework_edits_total | 0 | 0 |
| other_module_edits_total | 45 | 0 |
| mean_edited_outside | 6.5 | 2.2 |
| tasks_that_created_a_dotenv | 0 | 0 |
| total_cost_usd | 3.19 | 3.89 |

### Comparison with main-2026-11-05 (read before the deltas)

- **Not comparable:** main-2026-11-05 was scored before the app-mode module-tests fix. Its
  `module-tests` failures and edited-outside counts of 12–15 on t1, t2, t6 and t10 are a scoring
  artefact, so `passed` (6 → 9), `other_module_edits_total` (45 → 0) and `mean_edited_outside`
  (6.5 → 2.2) do not measure a change in the framework.
- **Comparable:** fn checks are the same (all tasks complete in both runs). Kernel reads went from 4 to 1;
  t2 still reads `sandloom/core/infra/contracts.py`. Dev checks failed went from 1 to 2:
  - t6 `sources_both_modules`: the same choice in both runs. Members come from a projection over
    tenancy's v2 events, and the plan is read live from billing's contract, because "billing's
    events carry no version". In the old run a red module-tests gate hid this deviation; here the
    gates are green, so it now counts as uncaught (0 → 1).
  - t9 `gate_checks_the_organization`: new. The agent gated with
    `SubscriptionActive(of=Account.bound_tenant())` rather than `Account.org(param=...)`, because the
    invites routes have no `org_id` path parameter. The checker only accepts `Account.org(` or
    the org fact, so this may be a checker false negative and needs a review.
  - t9 gates: the agent's last `sandloom generate && sandloom test` passed all ten steps. Scoring
    then found `.sandloom/inspection.json` stale (SANDLOOM-T015), although nothing was edited after
    that run. This is the only task whose route derives a `requires_contract` edge
    (billing.entitlements.v1) from a gate. Unconfirmed lead: the inspection output depends on the
    environment or is nondeterministic.
- **Cost and time:** tokens, turns and minutes went up (mean tokens 0.81M → 1.10M, +35%; t1
  1.0M → 2.6M, t3 1.2M → 1.9M, t4 0.5M → 0.9M). Five of the ten tasks changed tokens by less than
  30% (t2, t5, t7, t8, t10); t1, t3, t4, t6 and t9 changed by more. With one sample per task
  these numbers cannot tell a regression from run-to-run variation. t1 has the largest change.
- **t2's report:** the agent writes that the `dedupe_key` of `welcome:<user_id>` means "a retry
  after a failed commit can't send a second copy". That is not so: `Notification.dedupe_key` is a
  key a driver *may* use (`sandloom/core/infra/contracts.py`), and the SMTP driver ignores it, so
  a send that succeeded before a failed commit is sent again on retry. The report is kept as the
  agent wrote it; no check scores this claim.
