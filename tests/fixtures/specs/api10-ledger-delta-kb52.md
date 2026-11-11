# Billing declares its own settings (KB-52, 2026-11-09)

- G-BILLING-18: Stripe without its keys and the fake PSP in prod are one
  production check of billing's own settings (`BillingSettings`), validated by
  the loader before billing's `setup` in every entry point. The accepted risk
  is the module risk `billing.fake_psp_in_prod`. A process that does not load
  billing no longer parses or refuses either. The kernel-validator test becomes
  `test_the_kernel_settings_do_not_validate_billing`; the registration test
  moves to billing's settings tests.
- G-RISK-03: the registry holds twelve kernel ids; `fake_psp_in_prod` left it
  and is declared by billing as `billing.fake_psp_in_prod`.
- G-RISK-07: the same refusal, lifted by `billing.fake_psp_in_prod` only (no
  flat alias); `startup_checks` names the settings error raised before `setup`.
- G-CONFIG-07: the module refusals gain the explicit fake PSP and Stripe without
  keys; a process without billing boots with either set.
- G-OPS-14: `BILLING_DENY_ON_PAST_DUE` leaves the shared env anchor. Only
  billing reads it (other processes read the rule through the access fact
  billing describes), so it is passed to the billing service, as
  `REFRESH_TOKEN_EXPIRE_DAYS` is to identity.

No new SDK name, risk kind, setting or dedupe mechanism. The one-time rename of
the accepted risk id is in the CHANGELOG upgrade notes.
