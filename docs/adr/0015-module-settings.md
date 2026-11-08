---
search:
  exclude: true
---

# ADR-0015: Modules declare their own settings; the kernel validates them from one read

- **Status:** Proposed (2026-11-08). The owner accepts.
- **Date:** 2026-11-08
- **Deciders:** the owner, Sandloom maintainers
- **Decision id in the assessment:** decision D (alternative D2 in the D2a form, on top of PR
  #71; password hashing H1)
- **Amends:** [ADR-0006](0006-accepted-risks.md) (accepted-risk ids of modules are namespaced
  and declared by the module); [ADR-0010](0010-public-api-1.0.md) (five advanced SDK names
  under I-17 review)
- **Related:** [ADR-0012](0012-kernel-boundary.md) (B-C rows of the edge register; admin
  settings), [ADR-0014](0014-claims-trust-and-authorization-portability.md) (authorities are
  never settings, answer #9)
- **Research and evidence:** `design/kernel-boundary/research-settings.md` (commit `de53875`),
  `alternatives-d-settings.md` (commit `95f6479`), `adversarial.md` §5, `baseline.md` §4.4,
  `guarantee-map.md` §10; PR #71 (`816f587`: `8d3413a`, `2a85572`), PR #73 (`dae6883`), PR #74
  (`30104e8`) on `main`

## Context

The kernel holds the bundled modules' settings: `ModulesConfig` ("Settings of the bundled
business modules", 21 fields: Stripe, 2FA, SSO, Telegram, billing, admin console) mixed into the
one kernel `Settings`, plus `TENANT_CLAIMS_MAX_ORGANIZATIONS` in the auth config: 22
module-owned env names (B-C1, B-C2; the earlier count of 23 was off by one, alternatives-d O-1).
Their validators and accepted risks are kernel code: the TOTP key strength (B-C3), the billing
PSP requirements (B-C4), the risk `fake_psp_in_prod` in a closed tuple (B-C5), tiers and the
semantic ratchet (B-C6), the deploy-profile shared keys and dev env (B-C7), backup notes (B-C8)
and the console settings list (B-C9). A third-party module cannot add a setting, register an
accepted risk or classify a tier: it reads the environment itself, outside tiers, overrides,
risk logging, `config dump` and the profile-equality check (guarantee-map §10, probes 2-3).

The kernel validates configuration before it loads modules, and a module's production refusal
used to be a critical lifespan task reached only by the API. **PR #71 is merged and changes the
starting point:** identity's TOTP-key and `JWT_SECRET` refusals and billing's implicit-fake-PSP
refusal now raise `SystemExit(reason)` while the module registers, and the loader turns that
into `ConfigurationError` ("[core.config] Invalid configuration: module <name>: <reason>", exit
1) in every process that builds the composition: API, worker, beat, `sandloom migrate`, the
composed operator commands. `ConfigurationError` subclasses `SystemExit` and the loader's
per-module handler catches `Exception` only, so an optional module cannot swallow it. The
G-CONFIG-07 subprocess harness covers the matrix. PR #74 made the log redactor and `config dump`
share one secret-name rule (`TWOFA_ENCRYPTION_KEY` had been logged in clear). PR #73 made
identity declare `argon2-cffi` itself.

The owner accepted research option O2 (validate a module's settings before its `setup` takes
effect, cross-module checks after loading) and rejected a separate two-phase bootstrap
(owner-answers preamble).

## Decision

### 1. Declaration on registration (D2a)

A module hands a plain pydantic model to its registration:
`define_module(..., settings=BillingSettings)`, or `api.settings(BillingSettings)` in a
hand-written `register`. Fields keep their env names as aliases
(`Field(alias="BILLING_PROVIDER")`); secrets are `SecretStr`. Metadata sits in class attributes
holding the kernel's existing dataclasses: `semantic = (SemanticSetting(...),)`,
`risks = (Risk(...),)`, `removed = (RemovedSetting(...),)`, `shared`, `console`, `backup`.
Production checks are pydantic `model_validator`s that read `info.context["environment"]` and
`info.context["accepts_risk"]`. Readers call `settings_of(BillingSettings)`; a read with no app
bound raises, so a module-level read fails the import audit. Tests use a `sandloom.sdk.testing`
override instead of `setattr` on the kernel singleton.

New advanced SDK names (I-17 review): `settings_of`, `SemanticSetting`, `RemovedSetting`,
`Risk` (the last three exist in the kernel, newly exported) and the testing override.

### 2. One source read per process

`_load_into` first builds a frozen, case-insensitive source snapshot with pydantic-settings'
own sources and precedence (environment > `.env` > `SECRETS_DIR`), keeping each key's origin.
Kernel `Settings` and every module model validate over the snapshot; `SECRETS_DIR` files are
looked up only for declared aliases (kernel and loaded modules), never listed. No environment
read at module import (G-SDK-01). This replaces today's per-instance read and `config dump`'s
second read; no extra I/O.

### 3. Validation before `setup`, one check after loading

Inside the module's registration draft, before `setup`, the loader validates the model from the
snapshot with a validation context (`environment`, the accepted-risk set, a read-only view of
kernel `Settings`), stores the instance on the facade, then runs `setup`. A failure raises
`ModuleSettingsError`, a `ConfigurationError` subclass: fatal for an explicitly selected
optional module too (answer #29), the same message prefix and exit code as G-CONFIG-07.

One post-load check runs right after the load pass, the same function in every entry point that
builds the composition:

- one env name declared twice across the kernel and the loaded modules refuses, naming both
  (SANDLOOM-B036 proposed);
- accepted-risk ids by the namespace rule of decision 4;
- each loaded module's removed settings against the snapshot, with today's message;
- production checks that need more than one module (none today);
- the production typo check over the union of kernel and loaded names;
- override logging and the `sandloom_accepted_risk_active` metric for module semantic settings
  and risks.

Variables of a module absent from the process are neither validated nor shown and do not
interfere (answer #25); `sandloom check` catches them (decision 7).

### 4. Risk namespaces

Answers #27, #28. A module's risk id is `<module>.<name>` (`billing.fake_psp_in_prod`); kernel
ids stay flat (`relay_disabled`, `weak_password_hash`): only module ids were asked to move. An id
`<ns>.<name>` is:

1. checked against the module's declared risks when `<ns>` is a loaded module, refused if absent;
2. inert, and listed as "not applicable here" in diagnostics and doctor, when `<ns>` is known in
   the deployment but not loaded;
3. refused when `<ns>` is unknown, so a typo in a module or risk name never passes silently.

"Known in the deployment" is the installed `sandloom.modules` entry-point names, the loaded
modules and the app-local modules the composition root names; no new setting. A flat id that is
not a kernel id is refused; where its module is installed, the message names the namespaced
replacement from that module's declaration (importing an installed declaration is side-effect
free, a few ms), else a generic message names the rule and the upgrade notes. The migration is
one-time, documented, with no permanent alias (H-9).

### 5. Ratchets and completeness

Answers #7, #26. The kernel's semantic ratchet is recounted down: `SEMANTIC_BASELINE_COUNT` 13 →
12 and `SEMANTIC_COUNT_CEILING` 15 → 14 when `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` leaves.
Each module has its own semantic ratchet, run by a `sandloom.core.testing` helper in the
module's tests and by `sandloom test` for every module of the app. The kernel checks that every
setting is classified, not a sum of third-party settings: each field's tier is recorded in the
module's `module.snapshot.json` (written by `sandloom generate`, reviewed like the route
snapshot), so a semantic setting cannot slip in as ops unreviewed.

### 6. Env names

Answer #30. The existing module env names are kept byte-identical (22 today, 24 with decision
9). Collisions are forbidden at run time (decision 3) and statically (decision 7). A new name
must start with the module's prefix (`INVENTORY_`); `sandloom add module` scaffolds it. The
existing unprefixed names are grandfathered by the committed `module.snapshot.json`: a name
already recorded there passes, a new unprefixed name fails. No kernel list of module names.

### 7. Static gate in `sandloom check` and `sandloom test`

They import every module of the app (side-effect free), compare declared names across modules
whether or not they load together, check the prefix rule, run each module's ratchet helper, and
check each service of the chosen topology: wrong active module names in `SANDLOOM_MODULES`,
names set for a module the service does not load, and literal values against the declarations.
A deploy profile carries `${VAR:?…}` placeholders and no production secrets, so the static gate
validates names and literal values only (adversarial D-3); values are validated where the
process starts. The deploy-profile equality check (T014) unions the shared fields of the modules
each service loads: a shared billing value must be equal in every process that loads billing
(D-B2 wording, adversarial D-4).

### 8. `config dump` and what "activation" means

Answer #31. `config dump` lists kernel and module values with provenance (from the snapshot's
per-key origin) and redaction (the declared secret flag, with the name rule of PR #74 as the
backstop; `--show-secrets` stays refused in production). It builds the composition through
`load_composition_core` in a collect mode that reports a module's settings error as a diagnosed
entry instead of exiting. `--kernel-only` keeps today's output byte for byte.

**Activation**, which the dump must not do: entering the application lifespan; starting
consumers, relays, schedulers or tasks; running startup tasks; opening a network or database
connection. A module's `register` and `setup` are not activation as long as they open no socket;
building a client object is allowed, connecting is not. The PR proves it with a socket spy over
the bundled modules (billing builds its PSP client in `setup`). A module whose `setup` opens a
connection is fixed to connect lazily.

The collect mode is a context variable set by `config dump` before it imports the composition
root (the root builds the app at import time, so no argument reaches the loader). It is never
read from the environment, and a test asserts that every serving entry point runs with it unset
(adversarial D-1). Doctor uses the same mode to list every module's problem. The dump's wall time
is measured against today and reported (research estimate about 0.9 s, unmeasured).

### 9. Refresh-session settings move to identity

Owner decision #13. `REFRESH_TOKEN_EXPIRE_DAYS` and `MAX_SESSION_DAYS` become identity's
declared settings; refresh sessions are the issuer's (answer #16). The console settings list and
T014's shared keys read them from identity's declaration. Access-JWT verification, signing and
revocation parameters (`JWT_*`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `AUTH_REVOCATION_*`) stay in the
kernel, as do `TENANCY_MODE`, `SINGLE_TENANT_ID`, `FRONTEND_URL` and `NOTIFIER_LOG_LINKS`
(I-21, H-14).

### 10. Password hashing stays a kernel mechanism (H1)

Owner decision #12. The universal hashing mechanism (`core/auth/hashing.py`, argon2id on a
bounded executor), its work factor (`PASSWORD_HASH_*`), the OWASP floor and the risk
`weak_password_hash` stay in the kernel as a platform crypto capability; `password_hasher`,
`run_password_hashing` and `rehash_password_if_needed` stay public. Accounts, password policy,
refresh sessions and recovery belong to identity, which already holds them. No identity-specific
rule in the shared crypto implementation; the one identity rule the kernel still reaches (the
CLI's import of identity's password policy, B-I1) is removed by ADR-0012.

### 11. What moves

| owner | settings and checks | baseline rows |
|---|---|---|
| billing | `BILLING_PROVIDER`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_DENY_ON_PAST_DUE`, `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` (semantic), `BILLING_INTENT_RECONCILE_AFTER_MINUTES`, `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`; Stripe keys as a model validator, the fake-PSP refusal as a production check (one message replaces two); risk `billing.fake_psp_in_prod`; its two removed settings; console-visible `billing_provider` | B-C1, B-C4, B-C5, B-C6, B-C9 |
| identity | `TWOFA_ENCRYPTION_KEY` (presence and strength), the eight SSO client settings, `TELEGRAM_BOT_TOKEN`, `SSO_ALLOWED_REDIRECT_URIS`, `REFRESH_TOKEN_EXPIRE_DAYS`, `MAX_SESSION_DAYS` | B-C1, B-C3 |
| tenancy | `TENANT_CLAIMS_MAX_ORGANIZATIONS` | B-C2 |
| admin | `ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST`; the kernel CLI stops reading them and tests whether the composed app mounts the console route (generic, like the start page's probe) | B-C1, B-C10 |
| kernel tools | tiers and `/diagnostics/core`, `config dump`, doctor, backup notes per module secret, the console list, T014 shared keys and the dev env, the docs generator, the deploy scaffold's env | B-C7, B-C8, B-N8 |

### Mechanisms

| # | mechanism | kind | removes | compared against |
|---|---|---|---|---|
| D-M1 | settings declaration on registration | registration call | B-C1, B-C2; I-13 | kernel fields (fail I-13); module-local `BaseSettings` (D1: no shared source, no collision check, kernel tools blind) |
| D-M2 | per-process source snapshot | settings source | the second read; ~17 ms per module with `SECRETS_DIR` | per-module reads |
| D-M3 | post-load configuration check | startup check family | B-C3, B-C4 kernel validators; closed-registry and kernel-only typo checks | critical startup tasks (no parity); kernel validators (I-02) |
| D-M4 | namespaced risk registry with the known-namespace rule | registry | B-C5, T10 | flat ids (cannot tell an absent module from a typo); a kernel list of module ids |
| D-M5 | `sandloom check` / `test` settings gate | startup check family (static) | D-F2 across modules, D-B1, #30 prefix, #26 ratchets | a runtime warning for unloaded modules (answer #25 asks for none) |
| D-M6 | `config dump` collect mode (context variable) and `--kernel-only` | CLI change; process-scoped switch | F2; D-H5 | doctor alone (no values or provenance) |

Kernel tools reading declarations (B-C6..B-C9, B-N8) change input; they are not a mechanism.

## Alternatives considered

| alternative | why not |
|---|---|
| **D1** module-local pydantic | fewest mechanisms, but fails 8 of the 16 decision-D scenarios and passes 6 only by discipline; I-18 stays violated (H-1) |
| **D2b** a `ModuleSettings` base with `setting()`, `@prod_check`, `.current()` | same runtime as D2a with more SDK names and concepts; its advantages (co-located metadata, every production problem listed) are DX, decided by the comparative DX task (P12) if the owner asks |
| **D3** static metadata validated before loading | no import saved (importing the declaration imports the package), a new entry-point group or generated file per module; the fallback if `config dump` proves too slow |
| **D4** one composed `Settings` | a bootstrap subset, a dynamic class, mixins from modules: mechanisms that remove no violation D2 leaves (H-12) |
| a separate two-phase bootstrap | rejected by the owner (preamble of owner-answers.md); O2 gives the order without it |
| nested settings models | renames the 22 env names (answer #30) |
| an explicit list of known namespaces (K2) | a new setting and drift against the profile; K1 serves the shipped one-image profile |
| a `core.` prefix for kernel risk ids | a migration nobody asked for |
| compatibility aliases for old risk ids | H-9, answer #28 |
| per-field exemption for unprefixed names | a declaration field for 22 names; the committed snapshot already records them |
| **H2** work factor to identity, floor in the kernel | the work factor protects every stored password whoever stores it; H1 keeps H-2 and I-20 |
| **H3** hashing to identity | removes three public names and makes the floor optional per issuer |

## Consequences

**Benefits.** A third-party module declares settings, risks and tiers with no kernel change
(I-13), and gets the same guarantees as a bundled one: one read, validation before `setup`,
refusal parity across entry points, redaction, provenance, the ratchets. The kernel stops
validating billing in processes without billing. A risk-id typo refuses instead of passing.
`config dump` shows module values.

**Costs.** About 50 module files import the kernel singleton today, not all for module fields;
readers change to `settings_of`; about 25 test files set module fields and switch to the testing
override. Five advanced SDK names. `config dump` gains one composition build (measured in KB-55).
Deployments rename `fake_psp_in_prod` to `billing.fake_psp_in_prod` once.

**New risks.** A module setting set where the module is not loaded is silent at run time and
caught only by the static gate (answer #25 accepts it). A per-service image without a module's
wheel refuses that module's risk id: the safe direction, a refusal. A `setup` that connects
breaks the dump's no-activation rule until fixed.

**Migration.** Upgrade steps: the risk id rename; no env name changes; a prod `sandloom migrate`
one-shot needs the module env (already true after PR #71). The settings PoC (P1-P16 of
alternatives-d §11) was not run. It is the gate KB-49 of the migration plan: all 16 scenarios run
and get a recorded result before any of KB-50..KB-56 starts, and each of those PRs then turns its
scenarios into repository tests. A failed scenario reopens this ADR or picks its fallback (D2b on
P12, D3 for the dump on P11, H2 on P16) first.

**Conditions to revisit.** `config dump` with the composition is too slow (then D3 for the dump
only). The socket spy finds `setup` I/O that cannot be made lazy. A deployment cannot serve K1's
known namespaces. The DX task shows D2b clearly cheaper for authors.

## Evidence

The PoCs ran on the bases named below. The migration PRs measure against the merged baseline
`c9b9b05`: `main` with PRs #71-#74 merged into `arch/kernel-boundary` (tm `kb-adrs-revise`);
the merge changed registration and boot (PRs #71, #72: refusals at registration, owner checks),
packaging (#73) and log redaction (#74), and added one owner comparison when a remote facts
descriptor is read (#72); it added no call, query or RPC on a request path. The PoC numbers stay
the reference for decisions; each PR re-measures base against candidate on `c9b9b05` or later
(M-boot in particular).

| item | where | result |
|---|---|---|
| boot order per entry point; what reads settings | `research-settings.md` §1-§3 | the kernel validated before loading modules; refusals reached only the API (F1); `config dump` builds no composition (F2) |
| cost of reads | `research-settings.md` §3 | kernel `Settings()` 41.8 ms cold; a module `BaseSettings` over its own sources 17.3 ms; `model_validate` over a shared snapshot ~0.001 ms; entry-point scan 3.6 ms; `create_app` with three modules ~440 ms |
| refusal parity | PR #71 (`8d3413a`, `2a85572`), `core/tests/test_boot_configuration_checks.py` | app, worker, beat, `migrate`, composed CLIs exit 1 with the same reason; `config dump` builds no composition; asymmetric-issuer `JWT_SECRET` case |
| an optional module cannot swallow a refusal | `core/config/common.py`, `core/loader/plugin_loader.py` (alternatives-d F-c) | `ConfigurationError` subclasses `SystemExit`; the per-module handler catches `Exception` |
| one secret rule | PR #74 (`30104e8`), `core/tests/test_secret_redaction_agreement.py` | every `Settings` field: log redactor and dump agree |
| alternatives and verdict | `alternatives-d-settings.md` §5, §10; `adversarial.md` §5.1, §9 | D2a stands with the collect switch counted, activation defined, the static gate's scope stated |

## Fitness tests

| test | enforces | exists / new |
|---|---|---|
| fixture module outside `src/` with a secret, a bounded ops setting, a semantic setting, a namespaced risk, a production check and a removed setting; adding it edits no kernel file | decision 1; I-13 (P1) | new (KB-50) |
| G-CONFIG-07 process matrix extended to module settings errors, risk ids and collisions | decision 3; I-18 parity (P2, P5) | extends `core/tests/test_boot_configuration_checks.py` |
| one `.env` parse and one secrets lookup per declared alias per process | decision 2 (P7) | new (KB-50) |
| risk namespace rule (a)-(d) in a split with one shared anchor | decision 4 (P4) | new (KB-51) |
| kernel ratchet 12/14; per-module ratchet helper; tier recorded in `module.snapshot.json` | decision 5 (P8) | extends `core/tests/test_config_tiers.py` |
| `sandloom check` flags a name declared by two modules never loaded together, an unprefixed new name, a wrong module name in `SANDLOOM_MODULES`, a shared value differing between services | decisions 6, 7 (P6, P9) | new (KB-56) |
| `config dump`: module values with provenance and redaction; an invalid module value diagnosed with exit 0; `--kernel-only` byte-identical; socket spy finds no connection; collect mode unset in every serving entry point; wall time reported | decision 8 (P11) | new (KB-55) |
| bundled modules ported: G-BILLING-18, G-RISK-07, G-AUTH-19, G-CONFIG-07 pass; the console list shows `billing_provider` only where billing is loaded | decision 11 (P10) | KB-52..KB-54 |
| import audit under `python -I`: a module-level `settings_of` call fails | decision 1; G-SDK-01 (P15) | extends `tools/checks/import_audit.py` |
| `create_app` with the bundled modules plus the fixture, 10 alternating runs: median within base-against-base noise | no extra I/O (P13) | new (KB-50) |
| G-AUTH-15..18 unchanged | decision 10 (P16) | exist |
