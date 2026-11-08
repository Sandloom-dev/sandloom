# Module production refusals in every entry point (2026-11-08)

- G-AUTH-19: identity's missing `TWOFA_ENCRYPTION_KEY` in prod is refused while
  identity registers, not by a critical lifespan task. The loader turns the
  module's `SystemExit` into the kernel's `INVALID_CONFIGURATION` refusal, so the
  app, the worker, beat, `sandloom migrate` and the operator commands all stop.
  The registration test is renamed and the subprocess matrix cites it.
- G-BILLING-18: the prod part (the implicit fake PSP) moves the same way; the
  explicit `BILLING_PROVIDER=fake` settings validator is unchanged.
- G-RISK-07: the same refusal, still lifted by the accepted risk
  `fake_psp_in_prod`; `startup_checks` now names the module loader.
- G-CONFIG-07: the parity statement gains the module refusals and the composed
  entry points (worker, beat, migrate, `outbox`, `streams`, `projection`,
  `sagas`, `backup`); `sandloom config dump` still builds no composition.

No new SDK name, risk id, setting or dedupe mechanism. The deploy profiles and
scaffolds already give the migrate one-shots the module environment.
