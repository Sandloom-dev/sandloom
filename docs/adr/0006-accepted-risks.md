# ADR-0006: One accepted-risks list instead of prod opt-out flags

- **Status:** Accepted
- **Date:** 2026-10-24
- **Deciders:** Sandloom maintainers
- **Supersedes:** — (amends the configuration tiers of `sandloom.core.config.tiers`;
  implements recommendations 4–5 of
  `docs/design/effect-model-best-practices-2026-10-24.md`)

## Context

The configuration tiers (`sandloom.core.config.tiers`) classify every setting as
*semantic* (changes a guarantee; its value is chosen per profile), *ops* or
*migration*. When the tiers were introduced the semantic tier held 30
settings. The best-practice review of 2026-10-24 found two kinds of inflation:

1. **Seven escape hatches with one meaning.** `BILLING_ALLOW_FAKE_PSP_IN_PROD`,
   `NOTIFIER_ALLOW_LOG_IN_PROD`, `STORAGE_ALLOW_LOCAL_IN_PROD`, the event-bus
   instance option `allow_volatile` (+ the dev-only
   `SANDLOOM_ALLOW_VOLATILE_CONSUMERS`), `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`,
   `PASSWORD_HASH_ALLOW_WEAK` and `JWT_ALLOW_SHARED_SECRET_SPLIT` each say
   "admit a weaker thing in production". They were spread over the settings
   reference, reviewed one by one, and only visible as separate overrides.
2. **Knobs nobody needs.** Single-valued semantic flags whose non-default
   value is never valid in production (`SCHEMA_VALIDATION_SOFT`,
   `MODULE_REQUIRE_LISTED`, `BEAT_LEASE_ENABLED`, `CONTRACT_READ_SCOPE_MEMO`,
   `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS`) and the three migration flags whose
   0.8 removal was already promised (`LEGACY_UNSCOPED_EFFECTS`,
   `DB_LAZY_BEGIN`, `INFRA.database.*.verify_roles`). Xu et al. (FSE 2015)
   show such knobs mostly cause support issues, not flexibility.

The review also found one *ops* setting that changes a guarantee:
`OUTBOX_RELAY_ENABLED=off` in a production app process downgrades the outbox
from at-least-once delivery to the post-commit dispatch alone.

## Decision

1. **`SANDLOOM_ACCEPTED_RISKS` is the one place a deployment accepts a weaker
   guarantee** — a list of stable risk ids registered in
   `sandloom.core.config.accepted_risks` (id, what it weakens, which check consults it),
   modelled on Django's `SILENCED_SYSTEM_CHECKS`. Every check that used to
   read a boolean calls `settings.accepts_risk(id)`. An unknown id fails
   startup. Each accepted risk in effect is reported like a semantic
   override (`kind="accepted_risk"`): a WARNING at every start,
   `sandloom_accepted_risk_active{risk}` (and `sandloom_config_override_active`),
   `/diagnostics/core` → `configuration.accepted_risks`, `sandloom doctor`. A
   prod-only risk accepted outside prod is inert: it is listed, not reported
   as an override.
2. **The old booleans are deprecated aliases** until 0.9 (migration tier,
   `removed_in="0.9"`): each maps onto its id and is reported as an active
   migration flag with the replacement. `SANDLOOM_ALLOW_VOLATILE_CONSUMERS` keeps
   its dev-only meaning (an alias of `volatile_event_bus` outside prod
   only); an outright removal would break dev stacks on Redis Pub/Sub for no
   production benefit, and folding it costs nothing.
3. **`OUTBOX_RELAY_ENABLED` is semantic.** `off` in a production app process
   is refused at startup (`app_factory.outbox_relay_problem`) unless the risk
   `relay_disabled` is accepted. The gunicorn config marks non-leader workers
   `delegated` (the process group still runs one relay) instead of `off`.
4. **Removed settings fail loudly.** The removed names live in
   `config_tiers.REMOVED`; still set to their inert value they log a
   warning, any other value fails startup (the behaviour asked for is gone).
   `verify_roles` in a database instance's options fails its option schema.
5. **`BILLING_DENY_ON_PAST_DUE` is product policy** (ops tier): both values
   are valid in production and the framework has no opinion.

Kept semantic, with reasons: the RPC auth settings (removed in 0.9 by the
D-14 schedule, then derived from `RPC_AUTH_MODE`); `SCHEMA_REQUIRE_ALL_VERSIONED`
(profile-chosen, a real dev convenience — removed in 0.8 by the DX plan: a
missing schema now fails in every profile); `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS`
(an operator workflow for PSP-created subscriptions, not a legacy shape);
`INTERNAL_ENDPOINTS_OPEN_IN_DEV` (dev-only, never honoured in prod);
`SMTP_USE_TLS` (local mail catchers need it off).

## Consequences

- The semantic tier shrinks from 30 to 18 settings (16 baseline + the two
  argued here: `SANDLOOM_ACCEPTED_RISKS`, `OUTBOX_RELAY_ENABLED`); the ratchet
  test pins a ceiling of 18 that only goes down (16 since the amendment
  below). The migration tier held the eight aliases (all `removed_in="0.9"`)
  until the amendment below removed them with the tier itself.
- A security review reads one list per deployment instead of grepping for
  `*_ALLOW_*`; alerting needs one rule on `sandloom_accepted_risk_active`.
- New weakenings are added as a risk id, never as a new boolean — the
  ratchet makes a new semantic flag need its own ADR.
- Dev workflows that relied on soft schema validation or unsigned fake-PSP
  webhooks now fail loudly; tests sign webhooks with
  `sandloom_billing.psp.sign_stripe_payload`.
- Upgrade steps: `docs/guides/upgrading-0.8.md`.

## Amendment (2026-10-25)

Sandloom carries no backward compatibility before its first release, so the
deprecation period of Decision 2 is dropped:

- The boolean aliases `BILLING_ALLOW_FAKE_PSP_IN_PROD`,
  `NOTIFIER_ALLOW_LOG_IN_PROD`, `STORAGE_ALLOW_LOCAL_IN_PROD`,
  `SANDLOOM_ALLOW_VOLATILE_CONSUMERS` (its dev-only meaning included),
  `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`, `PASSWORD_HASH_ALLOW_WEAK` and
  `JWT_ALLOW_SHARED_SECRET_SPLIT` are removed. They are entries of the
  removed-settings list (`sandloom.core.config.tiers.REMOVED`, Decision 4):
  startup refuses them unless set to their inert value, and the error names
  the risk id to accept instead.
- `SANDLOOM_ACCEPTED_RISKS=["<id>"]` is the only way to accept a production risk
  (ids: `fake_psp_in_prod`, `log_notifier_in_prod`, `local_storage_in_prod`,
  `volatile_event_bus`, `unversioned_ddl_repair`, `weak_password_hash`,
  `shared_hs_secret_split`, `relay_disabled`,
  `command_dedupe_below_retry_horizon`, `saga_horizon_beyond_command_dedupe`
  — the last one added by ADR-0008).
- The *migration* tier and its "deprecated value" reporting are gone: every
  setting is *semantic* or *ops*. The RPC auth settings kept semantic above
  are removed too (`RPC_AUTH_MODE`, `RPC_RESPONSE_BINDING`; ADR-0004
  amendment). The semantic tier holds 16 settings; the ratchet ceiling is 16.

## Related reading

- [Configuration tiers](../reference/configuration-tiers.md)
- [Security model](../reference/security-model.md)
