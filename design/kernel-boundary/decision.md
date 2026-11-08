# Kernel boundary: decision gate

tm `kb-adrs` (parent `kb-assess`), 2026-11-08; revised by tm `kb-adrs-revise` after the Codex
review of `c5efeac`, on the merged baseline `c9b9b05` (`main` with PRs #71-#74). The checklist
the owner reviews before accepting ADR-0012..0015 and the ADR-0010 amendment. Each item has a
status and a link to where it is settled. Statuses:

- **done**: evidence recorded or change merged;
- **decided**: the ADR states it; the owner accepts it with the ADR;
- **confirm**: the ADR proposes an answer the owner has not given yet; accept or change it;
- **PR**: decided, proven only by the named PR of [migration-plan.md](migration-plan.md);
- **open**: not settled by this work.

## 1. The ADRs

| ADR | decision | status |
|---|---|---|
| [ADR-0012](../../docs/adr/0012-kernel-boundary.md) | A1 smaller form: names rule, edge register, `sandloom-standard`, admin battery, kernel revocation applier, CLI phase later | Proposed |
| [ADR-0013](../../docs/adr/0013-contract-ownership.md) | B1 delivered as B4: consumer-owned copies, `consumes` as the single source, expectations | Proposed |
| [ADR-0014](../../docs/adr/0014-claims-trust-and-authorization-portability.md) | TA-1 + PA-2: assigned authorities, trusted view, per-key `cpv`, sealed mint; contract-anchored access checks | Proposed |
| [ADR-0015](../../docs/adr/0015-module-settings.md) | D2a on PR #71, H1: declared module settings, one read, post-load check, risk namespaces | Proposed |
| [ADR-0010 amendment](../../docs/adr/0010-public-api-1.0.md) (2026-11-08) | generated consumer API category; new imports of `OrgRole`, `SubscriptionActive`, `Credits`, `Subscribed`; typing check | Proposed |
| [edges.yml](edges.yml) | 46 rows, every one `current_status: open`; targets 33 fixed, 3 retained, 10 deferred | Proposed |
| [migration-plan.md](migration-plan.md) | 35 PRs and one PoC gate (KB-49) in eight waves W0-W7, plus 2 in the later CLI phase | Proposed |

## 2. Gate items from the process

| # | item | status | where |
|---|---|---|---|
| G1 | baseline closed with verdicts on all 46 judged rows | done | [baseline.md](baseline.md) (`b8d8506`), tm `kb-baseline-close` |
| G2 | requirements approved with the owner's 31 answers | done | [requirements.md](requirements.md), [owner-answers.md](owner-answers.md) |
| G3 | at least three alternatives per decision, incl. the minimal sufficient one, each mechanism counted | done | `alternatives-{a,b,c,d}-*.md` |
| G4 | adversarial review; every blocker and major answered | done | [adversarial.md](adversarial.md); §3 below maps each finding |
| G5 | PoCs for B and C run, SHAs and measurements recorded | done | [validation.md](validation.md) §2; ADR-0013, ADR-0014 "Evidence" |
| G6 | PoC for decision D (settings P1-P16) | PR: not run yet; KB-49 runs all 16 scenarios and blocks every settings PR KB-50..KB-56 until each has a recorded result | migration-plan KB-49; ADR-0015 "Migration" |
| G7 | owner's 14 pre-ADR decisions applied | decided | [owner-decisions-adr.md](owner-decisions-adr.md); §4 below |
| G8 | PRs #71-#74 reflected as done; `main` merged into the branch | done: merge `c9b9b05` (import-layering check keeps both the shrink-only first-party rule and PR #73's `provides`); full suite green | ADR-0012 "Context" table; each ADR's "Evidence" |
| G9 | one mechanism ledger for the four ADRs (adversarial A-8) | decided | ADR-0012 decision 8 and each ADR's "Mechanisms" |
| G10 | every ADR has context, decision, rejected alternatives with reasons, consequences, evidence, fitness tests | done | the four ADRs |
| G11 | register of 46 edges with status, owner, ADR, PR, invariants, tests, anchors | done | [edges.yml](edges.yml) |
| G12 | migration in small, independently green PRs with no-extra-I/O and before/after measurements | done | [migration-plan.md](migration-plan.md) |
| G13 | latency threshold (I-14 part 4) | decided: the procedure that sets it (dedicated bench, base-vs-base noise band, a threshold never below the band, re-measured on change); until it has run, latency is reported and does not gate. The number itself is set by the owner from the bench | migration-plan "Latency threshold"; requirements §6.2 |
| G14 | requirements.md wording follow-ups: module-owned env names 22 (24 after owner decision #13), D-B2 "every process that loads billing", A-B2 wording without a kernel-hosted command | done (`kb-adrs-revise`; 22 counted in code at `c9b9b05`) | requirements §4 settings, A-B2, D-B2, answer #30; adversarial D-4, D-5, A-7 |
| G15 | Codex review of `c5efeac`: three errors | done: (1) P0 `previous=` and the trusted view as one condition, with the identity → acme_identity test; (2) P1 revocation group without a database fallback, missing service id a configuration error; (3) P1 staged initial `cpv` enablement with a bounded compatibility mode | ADR-0014 decisions 3, 4; ADR-0012 decision 5; §7 below |

## 3. Adversarial findings

| finding | answer | status |
|---|---|---|
| X1 blocker: default authority map of the shipped app | `sandloom-standard` holds it (owner #1) | decided, PR KB-08, KB-10 |
| C-1 blocker: live adapters removed without H-2 exception | approved exception after G-AUTHZ-05 equivalence, third-party capability kept (owner #2) | decided, PR KB-13, KB-34 |
| A-1, A-2 contributed CLI commands | out of decision A; B-I1 removed without a move; CLI phase later (owner #14) | decided, PR KB-06, KB-60 |
| A-3 admin importing modules | the kernel hands resource data to admin's renderer | decided, PR KB-40 |
| A-4 kernel applier vs lean `/ready`, group placement | per-service group from `SANDLOOM_SERVICE_NAME`, required, no database fallback (Codex P1); registered only with an event bus; readiness rule | decided, PR KB-09 |
| A-5, X4 console login with an alternative issuer | no consumer-defined contract; the issuer declares its login entry point, never `iss` of an unverified token | decided (§7 X4), PR KB-41 |
| A-6 stateless issuer | an explicit, verifiable "no refresh sessions" declaration | decided (§7 A8), PR KB-09 |
| A-9 family templates | retained class (a) rows | decided, PR KB-03 |
| B-1, X4 kernel families under `strict` | compared against the kernel schema | decided, PR KB-21 |
| B-2 consumed set from imports | `consumes` is the single source (owner #9); imports only checked | decided, PR KB-20 |
| B-3, C-4, X3 contract-name ownership | access-bearing contracts owned by their first segment or the `access_providers` assignment, checked by real provider identity; others first-registrant | decided (§7 X3), PR KB-13 |
| B-4 closed DTOs | `extra` recorded; added field on a closed DTO is breaking | decided, PR KB-20 |
| B-5 generate without composing the provider | shown by contracts PoC proof 7 | done |
| B-6, X9 two fixes for first-request describe | K6 boot seeding of every remote binding; B's local expectation covers compatibility, not the proxy cache | decided, PR KB-36 |
| B-7 two copies in mono | allowed; registry per contract and shape | decided, PR KB-23 |
| C-a1 whole-map `cpv` | per-key `cpv`, `previous=` window as one condition with the trusted view, staged initial enablement, issuer × verifier matrix (owner #10, Codex P0/P1) | decided (§7 previous=), PR KB-11 |
| C-a2 sealed mint inputs | issuer resolved from the calling module; `precomputed` limited; issuer fields protected at the read | decided, PR KB-12 |
| C-a3, X2 who is the issuer | the map names it; `issues_tokens` checked against it | decided, PR KB-10 |
| C-a4 status precedence | 401 `token_policy_outdated`, then 503, then 501 (the review proposed 503 first); 401 only where a refresh fixes it, one controlled refresh | decided (§7 C7), PR KB-11 |
| C-a5 test authority scope | refused wherever the testing plugin is inactive | decided, PR KB-12 |
| C-a6 impersonation through `collect_claims` | recorded as a limit outside the threat model | decided |
| C-a7 claims rung inside noise | micro rung measured +0.57 %; per-key re-measured | PR KB-11 |
| C-2 K1 metadata source | assigned tenant authority's witness from the consumer's generated copy (owner #7) | decided, PR KB-31 |
| C-3 `Credits` not equal by construction | measured: 1 call / 4 DB both sides | done (Z-2) |
| C-5 K7 refresher start state | escalate to live or 503 before the first fetch; K7 optional | decided, PR KB-38 |
| C-6 `Account` ownership | kernel concept with neutral semantics, proven on a third-party access check | decided (§7 C4), PR KB-30 |
| C-7 risk metadata is the provider's word | lowering is T004; route classification stays the backstop; describe extension counted (C-M4) | decided, PR KB-30, KB-32 |
| C-8 PA-3 split cost unmeasured | measured: equal calls, +20 % / +29 % instructions | done (Z-2, Z-6) |
| D-1 collect-mode switch | a context variable set by `config dump`, never from the environment; counted (D-M6) | decided, PR KB-55 |
| D-2 activation | defined in ADR-0015 decision 8; socket spy | decided, PR KB-55 |
| D-3 topology check scope | names and literal values only | decided, PR KB-56 |
| D-4, D-5 wording and counts | see G14 | done |
| D-6 hashing evidence | H1 on H-2 and I-20 (owner #12) | decided |

## 4. Owner decisions of 2026-11-08 (pre-ADR) and where each is applied

| # | decision | applied in | status |
|---|---|---|---|
| 1 | X1: `sandloom-standard` holds the standard composition and trust map | ADR-0012 decision 3 | decided, PR KB-08, KB-10 |
| 2 | C-1 approved exception, after G-AUTHZ-05 equivalence, third-party capability kept | ADR-0014 decisions 6, 14 | decided, PR KB-13, KB-34 |
| 3 | billing (c): non-critical for minting, absent never allows, 503 / 501 | ADR-0014 decision 7 | decided, PR KB-10, KB-30 |
| 4 | `Subscribed` without provider 501; 501 vs 402/403 vs 503 | ADR-0014 decisions 7, 11 | decided, PR KB-30 |
| 5 | `statuses=`, `claim_key=` kept, bound to the provider's declaration | ADR-0014 decision 11 | decided, PR KB-30 |
| 6 | four beginner names to the generated consumer API; ADR-0010 amended | ADR-0010 amendment; ADR-0014 decision 8 | decided, PR KB-34 |
| 7 | `tenant_kind` from the assigned tenant authority, four kinds kept | ADR-0014 decision 13 | decided, PR KB-31 |
| 8 | incompatible access-check contract → indeterminate 501; user input 422 | ADR-0014 decision 12 | decided, PR KB-30 |
| 9 | `consumes` as the single source incl. arguments, DTOs, wire | ADR-0013 decisions 2, 3 | decided, PR KB-20, KB-21 |
| 10 | `cpv` per key; issuer × verifier rollout and rollback matrix | ADR-0014 decision 4 | decided, PR KB-11 |
| 11 | kernel revocation applier, per-deployment group, readiness | ADR-0012 decision 5 | decided, PR KB-09 |
| 12 | H1 | ADR-0015 decision 10 | decided |
| 13 | refresh settings to identity | ADR-0015 decision 9 | decided, PR KB-53 |
| 14 | identity CLI in a later phase | ADR-0012 decision 6 | decided, PR KB-60 |

## 5. Other owner points from adversarial §8, as proposed

| # | point | proposed in | status |
|---|---|---|---|
| A1 | issuer-agnostic incident revocation | stays with the CLI phase; A2's P1 port only if ruled a platform guarantee | open (CLI phase) |
| A3 | scope of the CLI move | B-I1 now; commands later (owner #14) | decided |
| A4 | operators in split | identity's commands from identity's image (CLI phase) | open (CLI phase) |
| A5 | set-password link target | CLI phase; KB-54 removes the setting read | open (CLI phase) |
| A6 | `Permissions`, `Roles` | move to identity under I-17 review | decided (§7 A6), PR KB-34 |
| A7 | A-B2 wording | holds wherever the commands are hosted (G14) | done |
| A9 | what observes "executed" | consumer lag and dead-letter in `/ready`, with criteria for a critical state | decided (§7 A9), PR KB-09 |
| B1 | no expectation for a required remote contract | refuse / `contract_unverified`; kernel families against the kernel schema | decided |
| B2 | provider version in provenance | omitted | decided |
| B3 | applications: every composed provider or `consumes` | `consumes`, with dynamic calls covered by static or stated method lists | decided (§7 B3), PR KB-20 |
| B4 | name of the generated package | `<module>/_generated/{contracts,events}` | decided |
| B5 | C-GEN-03 text | the exact normative text in ADR-0013 decision 10 | decided (§7 B5), PR KB-22 |
| B6 | C5 generic primitive | only with a second producer | decided |
| C8 | declared claim keys (M6) | not adopted now | decided |
| C9 | `Feature`, `WithinLimit` home | billing, after KB-33's research; never removed automatically | PR KB-33, then the owner |
| C10 | `past_due` freshness | today's path kept (Z-14 equal); K7 optional | decided |
| C11 | test authority scope | refused wherever the testing plugin is inactive | decided |
| D2 | refresh settings | owner #13 | decided |
| D3 | D-F4 (d) without the module loaded | installed declaration names the replacement; generic message otherwise | decided |
| D4 | password hashing | H1 (owner #12) | decided |
| D5 | activation | ADR-0015 decision 8 | decided |
| D6 | kernel risk ids | flat | decided |
| D7 | completeness of classification | tier in `module.snapshot.json` | decided |
| D8 | grandfathering unprefixed names | the committed module snapshot | decided |
| D9 | known namespaces | installed entry points ∪ loaded ∪ app-local | decided |
| D10 | identity's `JWT_SECRET` refusal | done in PR #71 (`2a85572`) | done |

## 6. What accepting the ADRs starts

1. The ten confirmations are recorded with their conditions (§7); the ADRs move to Accepted when
   the owner accepts the revised text.
2. Waves W0..W7 of [migration-plan.md](migration-plan.md) become tm tasks under a new parent, one
   task per PR (and one for the KB-49 PoC gate), with the PR's scope as the claim area; KB-50..KB-56
   are each blocked by KB-49.
3. The edge register (KB-03) lands first in W0 with every row `current_status: open`; every later
   PR sets its rows' `current_status`.

## 7. Owner confirmations (2026-11-08, after the Codex review of `c5efeac`)

The owner confirmed the ten items the first version left at **confirm**, each with the condition
Codex attached. Each condition is part of the decision: a PR that cannot meet it reopens the item.

| # | confirmed | condition | recorded in | PR |
|---|---|---|---|---|
| X3 | ownership of access-bearing contract names: first segment or `access_providers`; other names first-registrant | the provider is checked by its real identity: loaded short name and import path in-process, pinned `service_id` remotely; never a name alone. Before access checks exist, access-bearing contracts are classified from the consumer side with no domain constant in the kernel | ADR-0014 decision 6; migration-plan KB-13 | KB-13 |
| X4 | console login with an alternative issuer, no consumer-defined contract | the issuer declares its login entry point in its registration; the console finds the issuer through the map's `issuer`; never the `iss` of an unverified token; no declaration → configuration error naming the issuer | ADR-0012 decision 4 | KB-41 |
| C4 | `Account` stays a kernel concept with neutral semantics | proven on a third-party access check (`has_seat` on `Account.org(param=)` and `bound_tenant()`) with billing absent, decisions and counts equal, no billing term in `Account` | ADR-0014 decision 15 | KB-30 |
| C7 | status order 401, then 503, then 501 | 401 only for `token_policy_outdated` and only where a refresh fixes it (no `cpv`, or a token issued before the verifier's map took effect); after that 503 `claims_policy_mismatch`; one controlled refresh per client | ADR-0014 decision 3 | KB-11 |
| A6 | `Roles` and `Permissions` move to identity | under an I-17 review: public API and beginner-tier diff, upgrade step, no alias | ADR-0014 decision 15 | KB-34 |
| A8 | a stateless issuer may declare "no refresh sessions" | an explicit registration field, recorded in the snapshot, verified at startup against refresh routes, session models and `user_blocked` consumers | ADR-0012 decision 5 | KB-09 |
| A9 | "executed" observed through consumer lag and dead letters in `/ready` | criteria: not ready when the oldest pending event is older than the access-token lifetime, an event is dead-lettered, or the consumer is not running; a younger backlog is transient (ready, exported, WARNING above half the bound) | ADR-0012 decision 5 | KB-09 |
| B3 | applications generate copies for the union of `consumes` | every call covered by a static or stated method list; an unresolved dynamic call refused with a fix naming `consumes=`; uncovered applications keep per-provider copies | ADR-0013 decision 2 | KB-20 |
| B5 | C-GEN-03 loses its kernel-home clause | the exact normative text, quoted in ADR-0013 decision 10, written into the ledger unchanged, with its test changes | ADR-0013 decision 10 | KB-22 |
| `previous=` | the previous-authority window of decision 4 | only after the P0 fix: `cpv`, the allowed current or previous authority and the verified source are one condition for one authority; tested with identity → acme_identity, a real old token, rollout and rollback | ADR-0014 decisions 3, 4 | KB-11 |

The three errors of the review, as the ADRs now read:

1. **P0, ADR-0014:** a protected key is trusted only when one authority A, the current one or the
   declared `previous=` one, satisfies all of: the token's `cpv` digest for the key is that of
   (key, A's verified identity), and A is in `claims_sources` and not in `claims_sources_failed`.
   Parts satisfied by different authorities trust nothing.
2. **P1, ADR-0012:** the revocation applier's group is `sandloom:auth@tokens_revoked@<service
   id>`, from `SANDLOOM_SERVICE_NAME`, with no database-identity fallback; a composition that
   needs the applier without it refuses to start (configuration error); two services on one
   database and one bus each apply every revocation; T014 refuses equal ids in one profile.
3. **P1, ADR-0014:** the initial `cpv` enablement is staged: verifiers learn `cpv` in a bounded
   compatibility mode (`legacy_tokens=LegacyClaimsTokens(until=...)`), issuers switch, and
   enforcement starts one token lifetime after the last old issuer stopped; tested mono (mixed
   replicas) and split, with rollback.
