# Kernel boundary ADRs accepted: the legacy_tokens window as an accepted risk (2026-11-08)

- G-RISK-14 (new, gap M): the `legacy_tokens` compatibility mode of the initial
  `cpv` enablement (ADR-0014 decision 4) is recorded as an explicitly accepted
  temporary security risk before any code exists. `until` is the end of the
  actual transition; the window ends at `until + ACCESS_TOKEN_EXPIRE_MINUTES +
  JWT_LEEWAY_SECONDS`; the framework never extends `until` (a later value is a
  separate operator decision); after the window an old token with forged
  `roles` / `tenant_ids` grants no rights. KB-11 of
  `design/kernel-boundary/migration-plan.md` builds the mode and replaces the gap
  with the window-end test.

No guarantee weakens and no capability is removed. ADR-0012..0015 decision
headings move from `informational` to `planned` (listed under `planned_adrs`);
ADR-0014 decision 4 maps to G-RISK-14. No new SDK name, setting or risk id of
`SANDLOOM_ACCEPTED_RISKS`.
