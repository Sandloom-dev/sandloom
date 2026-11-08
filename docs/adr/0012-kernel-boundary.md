---
search:
  exclude: true
---

# ADR-0012: The kernel boundary — what the kernel may name, the standard composition outside it, one edge register

- **Status:** Accepted (2026-11-08, by the owner, after the Codex reviews of `c5efeac` and
  `787ca60`; tm `kb-adrs-accept`). Implementation follows the waves of
  `design/kernel-boundary/migration-plan.md`.
- **Date:** 2026-11-08
- **Deciders:** the owner, Sandloom maintainers
- **Decision id in the assessment:** decision A (alternative A1, in the smaller form the
  adversarial review left standing)
- **Amends:** [ADR-0001](0001-framework-distribution.md) (a sixth distribution,
  `sandloom-standard`, holds the standard composition root);
  [ADR-0007](0007-kernel-layers.md) (the kernel's layers gain an edge register next to the
  import contracts)
- **Related:** [ADR-0013](0013-contract-ownership.md) (decision B),
  [ADR-0014](0014-claims-trust-and-authorization-portability.md) (decision C),
  [ADR-0015](0015-module-settings.md) (decision D)
- **Research and evidence** (repository files, branch `arch/kernel-boundary`):
  `design/kernel-boundary/goals.md`, `requirements.md` (invariants I-01..I-24, hard
  constraints H-1..H-20), `baseline.md` (commit `b8d8506`), `guarantee-map.md`,
  `alternatives-a-boundary.md`, `adversarial.md`, `validation.md`,
  `poc/trust/results.md`, `owner-answers.md`, `owner-decisions-adr.md`, `edges.yml`,
  `migration-plan.md`

## Context

The kernel is `sandloom.core`, `sandloom.sdk`, `sandloom.shared` and `sandloom.main`. It
should be independent of the implementations of identity, tenancy, billing and admin, and own
the general protocol of security and isolation every module relies on (goals.md, "The goal").
The baseline at `b8d8506` counted what it actually knows:

| class | rows | verdicts (of the 46 judged rows) |
|---|---|---|
| (a) protocol: the kernel knows that modules exist (loader, entry points, claims contributors, fact contracts) | 18, plus A19 reclassified as disputed | — |
| (b) the kernel names a specific module or something it owns | 40 | 28 violation, 11 disputed, 1 allowed |
| (c) the kernel ships module-owned code or artifacts | 5 | 4 violation, 1 disputed |

Static imports of a business module from shipped kernel code: 0. Dynamic imports naming one:
1 (B-I1). The other 45 judged rows are names: contract names, claim sources, settings, the
admin manifest gate, CLI commands, generated stubs and event schemas. An import rule alone
(G-KERNEL-10, widened by `kb-guards-honest` at `fd7b058`) sees 1 of the 46. No row had a
recorded status, and the guards that existed were narrower than they claimed (problems.md #8).

Three facts shape the decision:

1. **The shipped default app is kernel.** `sandloom.main` is what the Docker image runs
   (`deploy/docker/Dockerfile`), the default composition root of the worker, beat and
   `sandloom-migrate` (`core/composition.py`, `DEFAULT_COMPOSITION_ROOT`), and the root of
   `sandloom[standard]`. Under decision C every protected claim needs an authority assigned in
   the composition root (answer #9). A map naming identity, tenancy and billing inside
   `sandloom.main` is a kernel edge: the trust PoC put it there and failed
   `test_the_kernel_names_no_module` by design (`poc/trust/results.md` §6 finding 3, run 5).
   The adversarial review rated this the one blocker shared by decisions A and C (X1).
2. **Some guarantees live in a battery.** The durable applier of
   `core.auth.tokens_revoked.v1` and the end of refresh sessions on a block exist only as
   identity's consumers (`sandloom_identity/__init__.py`, consumers `sessions@tokens_revoked`,
   `sessions@user_blocked`). Without identity a mark lost after the post-commit retries is never
   applied, with a counter as the only signal (guarantee-map D1).
3. **The admin battery is a client, not a kernel part.** The owner decided (tm reflection on
   `kb-alt-boundary`, 2026-11-08) that `sandloom_admin` is an optional UI battery and a
   privileged API client with no special database access. Platform authorization, audit,
   idempotency, tenant isolation, revocation and the safety rules of event and infrastructure
   operations stay in the kernel; removing admin changes no kernel guarantee. Today the kernel
   gates manifest generation on the name `sandloom_admin` and holds the manifest compiler
   (B-N1, B-N2).

What changed since the alternatives were written, merged on `main` and treated here as done:

| PR | merge | what it settled |
|---|---|---|
| #70 roles-claim-trust | `e264b09` | a second writer of a claim key and the reserved keys (`tenant_id`, `org_id`, provenance) refused at mint |
| #71 module-prod-checks-everywhere | `816f587` (commits `8d3413a`, `2a85572`) | identity's and billing's production refusals, and identity's `JWT_SECRET` refusal, raised at registration and reached by API, worker, beat, `migrate` and the composed operator commands (G-CONFIG-07 process matrix) |
| #72 registration-ownership | `206fa2d` (commits `a51a59d`, `971f80a`) | small fix A: `AccessFact.owner_module`, `owner=` and the reserved families `<m>.claims.v1` / `<m>.facts.v1` checked against the registering module, SANDLOOM-B034 |
| #73 identity-argon2-dep | `dfde67a` (commit `dae6883`) | identity declares `argon2-cffi`; a wheel may import a third-party distribution through a first-party dependency only when that dependency provides it |
| #74 log-redactor-twofa | `bf77bf9` (commit `30104e8`) | the log redactor and `config dump` share one secret-name rule (`TWOFA_ENCRYPTION_KEY` was logged in clear) |

## Decision

### 1. What the kernel may name

Shipped kernel runtime code (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`,
`sandloom.main`; tests excluded) and the built `sandloom` wheel contain no name a specific
module owns: no module or distribution name, contract or event channel, fact name, setting,
claim key outside the kernel protocol vocabulary, CLI command, file name, URL, role vocabulary
or generated artifact of a module. They import nothing whose name is fixed in kernel source.

The kernel names protocol concepts in its own vocabulary, and these are not edges:

- the claim keys `roles`, `permissions`, `tenant_ids`, the reserved keys (`sub`, `user_id`,
  `tenant_id`, `org_id`, `iat_us`, `jti`, `auth_time`, provenance, `cpv`), the platform role
  `admin`;
- the families `<m>.claims.v1` and `<m>.facts.v1`, whose names the kernel builds from
  descriptors at run time (recorded as retained class (a) rows, adversarial A-9);
- the `core.*` events (`core.auth.tokens_revoked.v1`, `core.auth.user_blocked.v1`);
- generic primitives with one consumer today that name no module: password hashing
  (ADR-0015, H1), `MoneyLedger`, `operation_intent`, `release_check`, `external_call`.

Templates, recipes, starter text and documentation are composition text: scanned in their own
scope and reported apart, not runtime edges (goals.md, answer #2). Kernel tests are outside
the shipped boundary; kernel unit and architecture tests run with no battery installed
(answer #2).

### 2. One edge register, checked

Every remaining place where the kernel names a module is one row of
`tests/architecture/kernel_edges.yml`, seeded from `design/kernel-boundary/edges.yml`. The
register absorbs `tests/architecture/kernel_module_imports_allowlist.json` and keeps its rule
(shrink only, a stale entry fails).

A row carries the baseline id, class, type, scope (`runtime` or `composition_text`), the
owner module, the baseline verdict, a **current status** (`open`, `fixed`, `retained`: the state
of the code now), a **target** (`fixed`, `retained`, `deferred`: where the decisions take it),
the decision that carries it, the PR, and anchors (path plus literal or symbol, never a line
number). The register is seeded with every one of the 46 rows at `current_status: open`, since
each finding exists when the check lands; a PR that carries a row to its target sets its current
status in the same PR. A `retained` row needs an ADR id and a reason; class (a) rows are
retained with the reason "protocol".

A row has a **kind**: `runtime` (the default) or `documentation`. A runtime row records a literal,
import or file the scan below finds, and rules 1-3 apply to it strictly. A documentation row
records a name the kernel mentions only in a docstring or comment (B-N20, a docstring example of
a generic mechanism): the scan excludes docstrings, so such a row never has a runtime finding and
does not need one. It is tracked separately: it must be `retained` with an ADR id and a reason,
its anchor file must exist and still contain the literal (else it fails as stale, "update or
remove the row"), and it never covers a runtime finding: the same literal in code is a finding no
row's anchors match (rule 1).

The kernel-edge check (one new T code, SANDLOOM-T017 proposed) scans the AST of shipped kernel
code: string literals outside docstrings, matched whole against module names discovered from
the source layout and installed `sandloom.modules` entry points, `sandloom_\w+`, contract and
event channels whose first segment is not `core`, and the settings and claim keys that decisions
C and D declare. Dynamic imports are allowed only at the listed loader call sites with a
descriptor-sourced argument; an argument whose origin the check cannot resolve is refused
(I-02). The built wheel is scanned for class (c): a generated file whose owner is not the
kernel is a finding. Rules:

1. every finding maps to exactly one runtime row's anchors, else it fails with the path, the
   pattern and the class it would have;
2. a runtime row with current status `fixed` and a finding fails (regression);
3. a runtime row with current status `open` or `retained` and no finding fails ("set its current
   status to `fixed`"); the target never makes the check pass or fail;
4. a documentation row is checked only as stated above (retained, ADR id and reason, anchor
   present), never against findings;
5. `--report` prints the counts by kind, class, type, scope, current status and target, and the
   rows a change adds, fixes or retains, for the PR description (I-03).

### 3. The standard composition lives in `sandloom-standard`

The owner's decision #1 (X1, option 3). A new distribution `sandloom-standard` holds the
standard composition root and the standard trust map:

- `sandloom_standard.main` exposes `app` and `core`: the kernel factory, every installed
  module, and `claim_authorities` for the standard batteries (ADR-0014): `roles`,
  `permissions`, `email`, `email_verified` and the issuer to identity; `tenant_ids` to
  tenancy; `subscription_status`, `plan_code`, `features`, `limits` to billing.
- The map names a standard battery only when that battery is part of the deployment's
  composition: loaded in the process, or bound in `SERVICE_BINDINGS` through its
  `<m>.claims.v1`. A key without an authority in the composition stays unassigned, and an
  unassigned protected key is a definite deny (ADR-0014). Because the policy version is per
  key (ADR-0014, decision 4), filtering one battery out changes no other key's trust: the
  split parity problem of a per-process filter (trust PoC finding 3) does not arise.
- `sandloom[standard]` depends on `sandloom-standard`; the Docker image, the shipped compose
  profiles, the worker, beat, `sandloom-migrate` and the composed operator commands name
  `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`, so every entry point builds the same
  composition (answer #B, owner #1). Generated applications keep `<app>.asgi` and write their
  own map there (`sandloom new`).
- The kernel keeps `sandloom.main` as the lean root: installed modules, no authorities. An
  application can assign its own authorities without installing `sandloom-standard`. A lean
  root with an issuer whose contributor is critical and writes a protected key refuses every
  mint (503) rather than minting untrusted claims silently (trust PoC T-6), and startup logs
  that no authority map is set.

### 4. The admin battery

As the owner decided: `sandloom_admin` owns the console, the resource schema, the manifest
compiler and its settings; the kernel owns the platform operations API and the platform
admission whether a console exists or not (AD-1, AD-4).

- **Manifest generation** becomes a generation extension on the existing module system: a
  module declares on its registration a file that `sandloom generate` writes, rendered from the
  composed app, checked by `generate --check` and `sandloom test` (A1's M2). The kernel
  collects every loaded module's resource declarations, which it already imports as the
  loader (`<module>.admin`, class (a) A7), and hands admin's renderer data, never modules:
  admin imports no other module (adversarial A-3, H-3). `core/admin/compiler.py` and
  `core/admin/models.py` move to `sandloom_admin`. Removes B-N1, B-N2.
- **`ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST`** become admin's declared settings
  (ADR-0015).
- **The console's identity test** (`sandloom_admin/api.py`, a module named `identity` in the
  manifest) becomes "the composition has an assigned issuer", read from the authority map
  (ADR-0014, X2). How the console logs an operator in with an alternative issuer is decided in
  the admin PR (KB-41) under one rule: no consumer-defined contract (adversarial A-5). The owner
  confirms (decision.md, X4) with its condition: **the issuer declares its login entry point**
  (its own login contract, or the route of its login page, in its registration), the console
  finds the issuer through the authority map's `issuer` entry and consumes that declaration
  optionally, and a composition whose issuer declares none serves the console with a
  configuration error naming the issuer. The console never chooses an issuer from the `iss` (or
  any other claim) of a token it has not verified: an unverified token is the caller's input.

### 5. Revocation: the kernel applies, the issuer ends its sessions

Owner decision #11, answer #16.

- **Kernel applier.** The kernel registers the durable consumer of
  `core.auth.tokens_revoked.v1` in every composition that has an event bus and can publish the
  channel, independent of identity. Its consumer group is
  `sandloom:auth@tokens_revoked@<service id>`. Every service applies every revocation
  (idempotent writes of the mark into its own cache), so one group must never be shared by two
  independent receivers: a shared group splits the events between them and each misses the
  other's share.
- **Service id, required.** A required durable consumer needs a stable service or deployment id,
  unique among the independent receivers on one event bus and equal across the replicas of one
  service: `SANDLOOM_SERVICE_NAME`. There is **no fallback to the database identity**: two
  services may share one database (a split profile on one Postgres) and one bus, and a group
  derived from the database would merge them into one group. A composition that needs the
  applier and has no `SANDLOOM_SERVICE_NAME` refuses to start in every entry point with a
  configuration error (`INVALID_CONFIGURATION`, naming the setting and the consumer); outside
  production too, since a silent merge is the failure. The shipped compose profiles and
  `sandloom add deploy` already set one per service (`deploy/compose/monolith.yml`,
  `microservices.yml`); the deploy-profile check (T014) refuses two services of one profile with
  the same id. A fixed group was refused by the consumer runtime in a split (trust PoC §6
  finding 4); the per-deployment group passed split conformance and e2e, and the PoC's
  database-identity fallback is withdrawn.
- **Scenario: two services, one database, one bus** (KB-09). Services `api` and `billing`
  share one Postgres and one Redis stream, each with its own revocation cache: with
  `SANDLOOM_SERVICE_NAME` set per service, each runs its own group and a revocation published
  once is applied in both (both refuse the older token); with the name unset in either, that
  service refuses to start naming `SANDLOOM_SERVICE_NAME`; with the same name in both, T014
  refuses the profile. Three replicas of `api` share `api`'s group and apply each event once.
- **Readiness.** A composition that needs the applier (an event bus is configured and any
  loaded module or the kernel can publish the channel) and has none is never ready: `/ready`
  fails naming it. A lean `create_app()` with `SANDLOOM_MODULES=[]` and no event bus registers
  none and answers `/ready` 200 (I-01, A-H1).
- **Issuer obligation.** The issuer is named by the authority map (ADR-0014, X2); a module that
  declares `issues_tokens=True` and is not the map's issuer, or the reverse, refuses startup.
  The issuer must register a durable consumer of `core.auth.user_blocked.v1`, or declare that
  it holds no refresh sessions (a stateless issuer); otherwise startup refuses
  (adversarial A-6, trust PoC R2).
- **A stateless issuer's declaration is explicit and verifiable** (owner's confirmation A8,
  with its condition). It is a registration field (`define_module(refresh_sessions=False)`),
  never inferred from an absent consumer, and recorded in `module.snapshot.json`. The startup
  check verifies it against what the module registers: an issuer that declares no refresh
  sessions and still registers a refresh route, a refresh-session model or table, or a consumer
  of `core.auth.user_blocked.v1` refuses startup naming the contradiction; the console and
  `sandloom doctor` show the declaration. A test asserts that a stateless issuer's refresh
  attempt is refused (no session exists to end).
- **What observes "executed"** (owner's confirmation A9, with its condition): the consumer's
  lag and dead-letter state in `/ready` (G-REVOKE-08), with criteria that tell a critical state
  from a transient backlog. `/ready` fails, naming the consumer, when (a) the oldest pending
  revocation or block event is older than the access-token lifetime
  (`ACCESS_TOKEN_EXPIRE_MINUTES`; past it, a token the event should have stopped may still be
  accepted), or (b) an event of either channel is in the dead-letter state after its retries
  (an unrecoverable failure: it is never applied without an operator), or (c) the consumer is not
  running in a composition that needs it. A backlog younger than that bound, with the consumer
  running and no dead letter, is transient: `/ready` stays 200, and the lag is exported
  (`consumer_lag_seconds`) and logged at WARNING above half the bound. KB-09 tests each state:
  a backlog under load stays ready, an aged backlog and one dead letter fail, recovery returns
  `/ready` to 200.
- **Proof required in the PR** (KB-09): scaling to 3 replicas, restart, replay, a version
  change of the channel, a cache failure during apply (redelivered, then applied), and two
  deployments sharing one event bus each receiving every event.

### 6. The CLI phase comes later

Owner decision #14. The identity-specific commands (`sandloom admin create | invite | list`,
`sandloom security revoke-sessions | force-password-reset | grant-admin`) move in a separate
phase after ADR-0012..0015. Until then rows B-R4, B-R5, B-N3 and B-N4 are `deferred` rows of
the register. The one import (B-I1) is removed now without moving a command: the kernel CLI
stops importing `sandloom_identity.password_policy` and relies on the provider's
`password_problem` answer, which it already calls over `identity.security.v1`
(adversarial A-2). When the phase comes, user commands are kept where possible through the
standard composition or a CLI extension, with no hard import and no identity name left in the
kernel. A1's contributed-command mechanism (M3) is not part of this ADR: it needs a new
entry-point group and cannot serve app-local modules without importing the composition root
(adversarial A-1).

### 7. Status of the 46 judged rows

`design/kernel-boundary/edges.yml` gives every row its current status (all 46 `open` on the
merged baseline `c9b9b05`), target, owner, carrying ADR, PR, invariants, tests and anchors.
Targets:

| target | rows | count |
|---|---|---|
| fixed | B-I1, B-R1, B-R2, B-R3, B-R6, B-S1, B-S2, B-C1..B-C10, B-N1, B-N2, B-N8, B-N12..B-N15, B-N17..B-N19, C1..C5, A19 | 33 |
| retained | B-S3 (the tenant claim as a protocol key with an assigned authority), B-N16 (rights in the subject), B-N20 (docstring example; the one documentation row, decision 2) | 3 |
| deferred | B-R4, B-R5, B-N3, B-N4 (CLI phase); B-N5, B-N6, B-N7, B-N9, B-N10, B-N11 (starter, templates, recipes) | 10 |

### 8. Mechanisms (one ledger for the four ADRs, adversarial A-8)

| # | mechanism | kind (requirements §4.2) | removes | ADR |
|---|---|---|---|---|
| A-M1 | edge register and kernel-edge check | startup check family (static), one T code | I-02, I-03 missing proof; replaces `test_module_names.py`'s literal scan and the import allowlist | 0012 |
| A-M2 | generation extension for module-declared generated files | new kind of generated artifact | B-N1, B-N2 | 0012 |
| A-M3 | kernel-registered revocation applier with a per-service group (`SANDLOOM_SERVICE_NAME` required, no database fallback) | process-wide default | D1, T8 first half | 0012 |
| A-M4 | issuer-obligation startup check | startup check family | T8 second half | 0012 |
| A-M5 | `sandloom-standard` distribution | new package or wheel | X1 | 0012 |
| B-M1..M3, C-M1..M6, D-M1..M6 | see the other ADRs | | | 0013, 0014, 0015 |

## Alternatives considered

| alternative | why not |
|---|---|
| **A0** the import rule only | sees 1 of 46 rows (B-I1); fails I-03, I-13, I-20, I-22. Kept as a layer, not as the definition |
| **A1 as first written**, with contributed CLI commands (M3) | M3 needs a new entry-point group or imports every module for `--help`, cannot reach app-local modules without importing the composition root, and pulls the CLI phase into decision A (adversarial A-1, A-2). B-I1 is removed without it |
| **A2** kernel ports for platform duties | four of its five candidate ports fail the platform capability rule (H-18); the fifth (issuer-agnostic incident revocation, P1) passes only if that is a platform guarantee, which the owner placed in the CLI phase (decision #14). Adds a `platform.*` port family against H-17 |
| **A3** declared vocabulary ownership | its startup gate duplicates small fix A (merged as #72) and the claim-key manifest answer #15 defers; adds a vocabulary format versioned with the generator |
| **A4** distribution and behaviour boundary only | cannot express I-03 (a literal or a computed import passes); about ten rows have no behavioural symptom. Its checks are adopted as fitness tests of this ADR |
| Default map in `sandloom.main` (X1 option 1) | a kernel edge that must vary with the installed subset or refuse in 15 of 16 subsets (I-19); failed `test_the_kernel_names_no_module` in the trust PoC |
| Default derived from installed first-party distributions (X1 option 2) | trust by name, which answer #9 excludes; research-trust e-5 |
| The issuer registering the revocation applier | D1 stays for any composition without identity |
| A startup refusal without identity instead of a kernel applier | tenancy alone, or a replacement issuer, could not run |
| One fixed consumer group for the kernel applier | refused by the consumer runtime in a split (one group, two databases' inboxes) |
| A group derived from the database identity when no service id is set (the PoC's fallback) | two services on one database and one bus would share a group and split the revocations between them; a missing id is a configuration error instead |
| The console choosing the issuer from a token's `iss` | the token is unverified input at that point (X4) |
| Building the admin manifest at console start | a split process does not compose every module |
| Admin's own command writing the manifest | outside `generate --check` (H-11, SANDLOOM-T016) |

## Consequences

**Benefits.** A module-owned name in shipped kernel code fails a check with its path; the
register shows every remaining edge with a status and a decision. The kernel wheel ships no
module artifact once ADR-0013 lands. A user application chooses its batteries and its
authorities without the kernel knowing them. The revocation guarantee holds without identity.
Removing admin changes no kernel guarantee, and a third-party module's admin resources reach
the console with no kernel or admin change (AD-5).

**Costs.** One new distribution to build, test and release with the others. Every shipped
entry point and deploy template sets `SANDLOOM_COMPOSITION_ROOT`. About 64 register rows (46
judged plus 18 class (a)) to maintain; a row per new edge, reviewed. The manifest compiler and
its tests move to admin. The kernel applier runs in every composition with an event bus, one
consumer per deployment.

**New risks.** `retained` used as an escape hatch: refused without an ADR id. A deployment
whose services disagree on `SANDLOOM_COMPOSITION_ROOT`: the deploy profile check (T014) asserts
one root per profile. A per-deployment group name that changes between releases would replay
from the start: the name is the configured service id, and replay is idempotent. A deployment
that relied on the PoC's database-identity fallback does not exist (it never shipped); a
deployment without `SANDLOOM_SERVICE_NAME` and an event bus now refuses to start, loudly. The
literal scan misses a contract name built at run time from a module name; the replaceability
fixtures catch it where it matters.

**Migration.** `pip install "sandloom[standard]"` keeps working; images and compose files gain
`SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main` (upgrade step). A deployment that composed
`sandloom.main` with the standard batteries and no map switches root or writes its own map;
otherwise every mint with a critical issuer refuses (fail closed, never silent). Identity loses
its `sessions@tokens_revoked` consumer; its `sessions@user_blocked` consumer stays.
`sandloom_admin` gains the compiler. CLI commands are unchanged until the CLI phase.

**Conditions to revisit.** The owner rules issuer-agnostic incident revocation a platform
guarantee (then A2's P1 port joins, replacing A-M4). The applier PoC rows of KB-09 fail on a
real broker. A third-party issuer cannot serve the console's login without a consumer-defined
contract (X4). The register grows instead of shrinking over two releases.

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
| baseline of 46 judged rows and 18 protocol rows | `baseline.md` at `b8d8506`; transcripts `_evidence/` | 32 violation, 13 disputed, 1 allowed |
| replaceability: identity, tenancy, billing replaced outside the kernel | `validation.md` §1, `poc/replace/out/head.txt`, commit `47c31de` | rights, tenant grants and an unrelated fact work with no kernel change, mono and split; every failure is a trust gap (ADR-0014) or a module name in kernel code (this ADR) |
| default map in `sandloom.main` is a kernel edge | `poc/trust/results.md` T-17, finding 3; commit `627c9c9` | `test_the_kernel_names_no_module` fails by design; 6504 passed otherwise |
| kernel revocation applier with a per-deployment group | `poc/trust/results.md` T-16, finding 4 | without identity: group `sandloom:auth@tokens_revoked@url:…` running, cache out → redelivered, back → mark written, older token 401; split conformance passes with identity's applier removed; a fixed group raised `ConsumerConfigurationError` in split. The PoC named the group from the database identity (`url:…`) when no service name was set; decision 5 withdraws that fallback, so KB-09 re-runs the rows with `SANDLOOM_SERVICE_NAME` set and adds the two-services-one-database scenario |
| issuer obligation | `poc/trust/results.md` T-16 | `issues_tokens` module without a `core.auth.user_blocked.v1` consumer refused (B035 in the PoC) |
| adversarial verdict | `adversarial.md` §2.1, §9, commit `53701d9` | A1 stands in a smaller form: M3 out, M2 passes data, M4 needs lean `/ready` and group placement, X1 answered |
| owner decisions | `owner-decisions-adr.md` #1, #11, #14; tm reflection on `kb-alt-boundary` (admin battery) | applied in decisions 3, 4, 5, 6 |

## Fitness tests

| test | enforces | exists / new |
|---|---|---|
| kernel-edge check over `kernel_edges.yml` (rules 1-5, runtime and documentation rows), run by `sandloom test` | decisions 1, 2; I-02, I-03 | new (KB-03), absorbs `kernel_module_imports_allowlist.json` |
| dynamic-import origin rule | I-02 | extends `tests/architecture/test_shared_purity.py` |
| import-linter `kernel-imports-no-module`, `test_shared_purity.py` | I-02 | exist |
| class (c) scan of the built kernel wheel | decision 1; I-11 | extends `tools/packaging/check_package_distributions.py` |
| kernel unit and architecture tests in a venv with no module wheel | answer #2 | new (KB-05); 195 ids fail today at `ceb3d66` |
| 16 module subsets × install profiles P2/P3 in clean venvs | I-19, A-B7 | new (KB-04); extends `core/tests/test_module_subset.py` (4 subsets today) |
| replaceability fixtures (`test_identity`, `grants`, `inventory`, `forged`) as integration tests, mono and split | I-13, I-22, A-H5 | new (KB-05), from `design/kernel-boundary/poc/replace` |
| process matrix: API, worker, beat, `migrate`, composed CLIs of the shipped image build the same composition from `sandloom_standard.main` | decision 3; owner #1 | new (KB-08), same harness as G-CONFIG-07 |
| lean `create_app()` answers `/ready` 200 with no event bus; a composition with a bus and no applier fails `/ready` | decision 5; I-01, I-20 | new (KB-09) |
| applier rows: 3 replicas, restart, replay, version change, cache failure, two deployments on one bus | decision 5; owner #11 | new (KB-09), split conformance |
| two services, one database, one bus: both apply every revocation; `SANDLOOM_SERVICE_NAME` unset refuses startup in every entry point; equal names refused by T014 | decision 5 | new (KB-09) |
| `/ready` criteria: transient backlog ready; backlog older than the access-token lifetime, a dead letter, or a stopped consumer not ready | decision 5; A9 | new (KB-09) |
| stateless issuer declaration contradicted by a refresh route, model or `user_blocked` consumer refused | decision 5; A8 | new (KB-09) |
| console: issuer from the map and its declared login entry point; no declaration → configuration error; a forged `iss` changes nothing | decision 4; X4 | new (KB-41) |
| `sandloom check` and `test_module_boundaries.py` see no admin → module import | decision 4; H-3 | new (KB-40) |
| `inventory` fixture declares admin resources; `generate` adds its section with no kernel or admin change | decision 4; AD-5 | new (KB-40) |
