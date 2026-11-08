# Kernel boundary: decision gate

tm `kb-adrs` (parent `kb-assess`), 2026-11-08. The checklist the owner reviews before accepting
ADR-0012..0015 and the ADR-0010 amendment. Each item has a status and a link to where it is
settled. Statuses:

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
| [edges.yml](edges.yml) | 46 rows: 33 fixed, 3 retained, 10 deferred | Proposed |
| [migration-plan.md](migration-plan.md) | 35 PRs in 8 waves, plus 2 in the later CLI phase | Proposed |

## 2. Gate items from the process

| # | item | status | where |
|---|---|---|---|
| G1 | baseline closed with verdicts on all 46 judged rows | done | [baseline.md](baseline.md) (`b8d8506`), tm `kb-baseline-close` |
| G2 | requirements approved with the owner's 31 answers | done | [requirements.md](requirements.md), [owner-answers.md](owner-answers.md) |
| G3 | at least three alternatives per decision, incl. the minimal sufficient one, each mechanism counted | done | `alternatives-{a,b,c,d}-*.md` |
| G4 | adversarial review; every blocker and major answered | done | [adversarial.md](adversarial.md); §3 below maps each finding |
| G5 | PoCs for B and C run, SHAs and measurements recorded | done | [validation.md](validation.md) §2; ADR-0013, ADR-0014 "Evidence" |
| G6 | PoC for decision D (settings P1-P16) | **open**: not run; its rows are the acceptance criteria of KB-50..KB-56 | ADR-0015 "Migration" |
| G7 | owner's 14 pre-ADR decisions applied | decided | [owner-decisions-adr.md](owner-decisions-adr.md); §4 below |
| G8 | PRs #71-#74 reflected as done | done | ADR-0012 "Context" table |
| G9 | one mechanism ledger for the four ADRs (adversarial A-8) | decided | ADR-0012 decision 8 and each ADR's "Mechanisms" |
| G10 | every ADR has context, decision, rejected alternatives with reasons, consequences, evidence, fitness tests | done | the four ADRs |
| G11 | register of 46 edges with status, owner, ADR, PR, invariants, tests, anchors | done | [edges.yml](edges.yml) |
| G12 | migration in small, independently green PRs with no-extra-I/O and before/after measurements | done | [migration-plan.md](migration-plan.md) |
| G13 | latency threshold (I-14 part 4) | **open**: needs a quieter bench; the +1 % instruction budget applies where resolved (answer #5) | requirements §6.2 |
| G14 | requirements.md wording follow-ups: module-owned env names 22 (24 after #13), D-B2 "every process that loads billing", A-B2 wording without a kernel-hosted command | **open**: requirements.md is outside this task's area | adversarial D-4, D-5, A-7 |

## 3. Adversarial findings

| finding | answer | status |
|---|---|---|
| X1 blocker: default authority map of the shipped app | `sandloom-standard` holds it (owner #1) | decided, PR KB-08, KB-10 |
| C-1 blocker: live adapters removed without H-2 exception | approved exception after G-AUTHZ-05 equivalence, third-party capability kept (owner #2) | decided, PR KB-13, KB-34 |
| A-1, A-2 contributed CLI commands | out of decision A; B-I1 removed without a move; CLI phase later (owner #14) | decided, PR KB-06, KB-60 |
| A-3 admin importing modules | the kernel hands resource data to admin's renderer | decided, PR KB-40 |
| A-4 kernel applier vs lean `/ready`, group placement | per-deployment group; registered only with an event bus; readiness rule | decided, PR KB-09 |
| A-5, X4 console login with an alternative issuer | no consumer-defined contract; issuer-declared login contract or `iss` discovery | **confirm** (owner X4), PR KB-41 |
| A-6 stateless issuer | may declare "no refresh sessions" | **confirm** (owner A8), PR KB-09 |
| A-9 family templates | retained class (a) rows | decided, PR KB-03 |
| B-1, X4 kernel families under `strict` | compared against the kernel schema | decided, PR KB-21 |
| B-2 consumed set from imports | `consumes` is the single source (owner #9); imports only checked | decided, PR KB-20 |
| B-3, C-4, X3 contract-name ownership | access-bearing contracts owned by their first segment or the `access_providers` assignment; others first-registrant | **confirm** (owner X3), PR KB-13 |
| B-4 closed DTOs | `extra` recorded; added field on a closed DTO is breaking | decided, PR KB-20 |
| B-5 generate without composing the provider | shown by contracts PoC proof 7 | done |
| B-6, X9 two fixes for first-request describe | K6 boot seeding of every remote binding; B's local expectation covers compatibility, not the proxy cache | decided, PR KB-36 |
| B-7 two copies in mono | allowed; registry per contract and shape | decided, PR KB-23 |
| C-a1 whole-map `cpv` | per-key `cpv`, `previous=` window, issuer × verifier matrix (owner #10) | decided, PR KB-11 |
| C-a2 sealed mint inputs | issuer resolved from the calling module; `precomputed` limited; issuer fields protected at the read | decided, PR KB-12 |
| C-a3, X2 who is the issuer | the map names it; `issues_tokens` checked against it | decided, PR KB-10 |
| C-a4 status precedence | 401 `token_policy_outdated`, then 503, then 501 (the review proposed 503 first) | **confirm** (owner C7), PR KB-11 |
| C-a5 test authority scope | refused wherever the testing plugin is inactive | decided, PR KB-12 |
| C-a6 impersonation through `collect_claims` | recorded as a limit outside the threat model | decided |
| C-a7 claims rung inside noise | micro rung measured +0.57 %; per-key re-measured | PR KB-11 |
| C-2 K1 metadata source | assigned tenant authority's witness from the consumer's generated copy (owner #7) | decided, PR KB-31 |
| C-3 `Credits` not equal by construction | measured: 1 call / 4 DB both sides | done (Z-2) |
| C-5 K7 refresher start state | escalate to live or 503 before the first fetch; K7 optional | decided, PR KB-38 |
| C-6 `Account` ownership | kernel concept with neutral semantics | **confirm** (owner C4) |
| C-7 risk metadata is the provider's word | lowering is T004; route classification stays the backstop; describe extension counted (C-M4) | decided, PR KB-30, KB-32 |
| C-8 PA-3 split cost unmeasured | measured: equal calls, +20 % / +29 % instructions | done (Z-2, Z-6) |
| D-1 collect-mode switch | a context variable set by `config dump`, never from the environment; counted (D-M6) | decided, PR KB-55 |
| D-2 activation | defined in ADR-0015 decision 8; socket spy | decided, PR KB-55 |
| D-3 topology check scope | names and literal values only | decided, PR KB-56 |
| D-4, D-5 wording and counts | see G14 | open (requirements) |
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
| A6 | `Permissions`, `Roles` | move to identity | **confirm** |
| A7 | A-B2 wording | see G14 | open |
| A9 | what observes "executed" | consumer lag and dead-letter in `/ready` | **confirm** |
| B1 | no expectation for a required remote contract | refuse / `contract_unverified`; kernel families against the kernel schema | decided |
| B2 | provider version in provenance | omitted | decided |
| B3 | applications: every composed provider or `consumes` | `consumes` (a reviewed generated diff) | **confirm** |
| B4 | name of the generated package | `<module>/_generated/{contracts,events}` | decided |
| B5 | C-GEN-03 text | kernel-home clause removed | **confirm** (ledger change, I-16) |
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

1. The owner accepts or changes the nine **confirm** items (X3, X4, C4, C7, A6, A8, A9, B3, B5)
   and the ADRs move to Accepted.
2. Waves W0..W6 of [migration-plan.md](migration-plan.md) become tm tasks under a new parent, one
   task per PR, with the PR's scope as the claim area.
3. The edge register (KB-03) lands first in W0; every later PR flips its rows.
