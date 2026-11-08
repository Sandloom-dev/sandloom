---
search:
  exclude: true
---

# ADR-0013: Contract artifacts belong to the distribution that consumes them

- **Status:** Proposed (2026-11-08). The owner accepts.
- **Date:** 2026-11-08
- **Deciders:** the owner, Sandloom maintainers
- **Decision id in the assessment:** decision B (alternative B1, delivered as B4)
- **Amends:** [ADR-0010](0010-public-api-1.0.md) (the note of 2026-10-28: the framework tree
  stops generating into `sandloom.shared`; the generated consumer API category is the
  amendment of 2026-11-08 at the end of ADR-0010)
- **Related:** [ADR-0012](0012-kernel-boundary.md),
  [ADR-0014](0014-claims-trust-and-authorization-portability.md) (generated access atoms live
  in the same generated package)
- **Research and evidence:** `design/kernel-boundary/alternatives-b-contracts.md`,
  `adversarial.md` §3, `poc/contracts/results.md` (commit `67980bf`), `baseline.md` §5, §13,
  `guarantee-map.md` §7 and D4

## Context

The kernel wheel ships the bundled modules' interfaces: 5 generated contract stubs (C1), 2 DTO
copy modules (C2), 34 event JSON schemas (C3) and a hand-written consumer rule for tenancy's
member events, `sandloom/shared/membership.py` (C5). The generator makes `sandloom.shared` the
artifact home of the framework tree (C4: `shared_package: sandloom.shared` in
`architecture_rules.yml`, a kernel-home branch in the generator). Two consequences:

- **A module's contract change is a kernel release.** At base, adding an optional field to
  tenancy's `MemberState` and an event field to billing wrote
  `src/sandloom/shared/contracts/tenancy_dto.py` and
  `src/sandloom/shared/events/billing.credits.added.v1.json`
  (`poc/contracts/out/p2-base-contrast.txt`). Billing reads tenancy's `MemberState` through the
  kernel wheel (`sandloom_billing/services/tenant_resolver.py`).
- **A consumer-only install is not checked (D4).** When the provider's package is not in the
  consumer's environment, the startup compatibility check finds no snapshot and logs a WARNING,
  also under `CONTRACT_COMPAT_CHECK=strict` (G-COMPAT-04, G-COMPAT-06; reproduced at base:
  `startup compat (strict): ok {"probed": []}`, `poc/contracts/out/p3-base.txt`).

Applications already generate their own copies into `<app>.shared`
(`examples/helpdesk/helpdesk/shared`), and ADR-0010's note of 2026-10-28 already states the rule
for applications and ecosystem modules. Module versions are independent of the kernel
(`docs/contributing/versioning.md`).

## Decision

### 1. Home rule

Generated consumer artifacts live in the distribution that consumes them:

- a module distribution: `<module>/_generated/contracts/` (stubs, `<provider>_dto.py` copies,
  `<stem>.describe.json` expectations, generated access atoms of ADR-0014) and
  `<module>/_generated/events/` (event schema copies of channels it views or consumes);
- an application: `<app>.shared/contracts/` and `<app>.shared/events/`, as today.

The kernel ships none. `sandloom.shared` keeps platform types (`subject`, the authorization
engine, guards) and is never a generation target. The framework tree's
`architecture_rules.yml` has no `shared_package`. The generator's kernel-home branch is
deleted, so every generated file imports `sandloom.sdk.contracts` and
`kernel-never-imports-sdk` holds with no exception. The producer's `module.snapshot.json`,
already shipped in its wheel, is the published event schema. `_generated/` is reserved:
`sandloom check` refuses a hand-written file in it and an import of another module's
`_generated` package.

### 2. `consumes` is the single source of what a consumer requires

Owner decision #9. A module's `consumes`, as recorded in its `module.snapshot.json` (derived
from module-level `Contract(REF)` references, the contracts of its route policies, and explicit
`define_module(consumes=...)`), is the only source of:

- which contracts get a generated copy in the module;
- which methods the consumer requires: each entry lists the methods it calls, derived
  statically from calls on the typed client, or stated in `consumes=` where a call cannot be
  resolved. An entry the generator cannot resolve to a method list is refused with a fix that
  names `consumes=`; an empty list no longer means "the whole contract";
- for each required method: the argument names, kinds and order, the argument and result DTO
  shapes (fields, types, constraints, defaults, aliases, `Literal` and `Enum` values, nested
  models, `extra`), and the wire codec.

The generator plans the copies statically (AST of the module's declarations, no import of the
module, no composition) before it composes the app, so a module that imports a stub it has not
generated yet still generates (contracts PoC, surprise 1). `generate --check` fails when the
planned set and the composed `consumes` differ. An import of `<module>._generated.contracts.<stem>`
with no `consumes` entry is refused (a stub imported for nothing); a `consumes` entry whose
contract is never used is reported. Every writer of generated files follows the rule:
`sandloom generate`, `sandloom add contract`, `sandloom add module`, the deploy scaffold.

Applications switch to the same rule: `<app>.shared` holds copies for the union of the app's
modules' `consumes`, not for every composed provider (fewer files; adversarial B-2, open point
B3). This is a generated-file change reviewed under I-15.

### 3. Expectation files and narrowing

Beside each stub, `generate` writes `<stem>.describe.json`: the provider's describe entry
restricted to the methods `consumes` lists, plus `contract`, `fingerprint`, `provider`
(distribution and class) and `generator_format`, under a `describe` key with room for B2's
`dtos` and `docs` keys (B4's forward-compatible shape).

Lookup at startup: beside the consumer's stub first, then `CONTRACT_SNAPSHOT_PATHS`, then the
provider package. Narrowing rule:

- the provider is held only to the methods, arguments, DTO shapes and codec the consumer
  declares; a provider method the consumer does not declare is not compared;
- a declared method missing from the provider, a declared argument the provider removed or
  made incompatible, a new required provider argument the consumer does not pass, or a declared
  DTO shape the provider no longer accepts is incompatible: `strict` refuses startup,
  `ready` fails `/ready` with `contract_incompatible`;
- an updated consumer that starts calling a method an older compatible provider lacks therefore
  refuses loudly at the consumer's startup; it never requires the method silently at the first
  call (owner #9, contracts PoC §3.4 defect fixed);
- a field added to a **closed** result DTO (`extra="forbid"` in the provider's
  `model_config`) is breaking, because an old copy rejects it; `extra` is recorded in the
  describe (adversarial B-4).

### 4. No expectation is never a warning

With no expectation for a required remote contract, `strict` refuses startup naming it and
`ready` fails `/ready` with `contract_unverified` (I-20, B-F5). This closes D4.

- **Kernel contract families exempt.** `<m>.claims.v1` and `<m>.facts.v1` have kernel-defined
  shapes, the same for every owner; they are compared against the kernel's own schema, not
  against a consumer copy (adversarial B-1, X4).
- **A hand-written `ContractRef(name)` without a stub** for a required remote contract is
  refused under `strict` and fails `/ready` under `ready`, naming the contract and the fix
  (generate a stub, or declare it `optional=True`, which needs no expectation because an absent
  provider answers `None`).

### 5. Provider absent where `generate` runs

The provider is a development dependency of the consumer, never a run-time one, and is
imported without composing it. If it is not importable: a committed copy whose header digest
verifies is kept; a hand-edited copy is refused; a missing copy is refused naming the module,
the contract and the provider. The refusal has its own error code and fix text (SANDLOOM-T018
proposed), not T007's "a breaking change is a new version" (contracts PoC §3.6).

### 6. Determinism without the project formatter

Generated files are a function of the provider's declarations and the generator format only
(I-15; answer #3). DTO copy modules are rendered in their final layout by the generator, as
stubs already are, never piped through the consumer project's ruff; `_generated/` is excluded
from the project's formatters by the generated `pyproject` and by `sandloom new`. The pydantic
minor is not an input. On a generator update every diff is reported as a generator change or a
contract change, with a compatibility check and snapshot review (answer #3).

### 7. Provenance header

Every generated consumer file starts with:

```
# Generated by `sandloom generate`: never edit by hand.
# contract: tenancy.membership.v1  fingerprint: fbf42e7650aa13c0878c442f6080d419
# provider: sandloom-tenancy (sandloom_tenancy.contracts.MembershipReadContract)
# generator: format 2  digest 5f320947b504774e
```

The digest covers the body, so a hand edit is detected without the provider. The generator
format is bumped only when rendered output changes. The provider's version is not recorded
(`uv.lock` records it); recording it would rewrite every consumer copy on every provider
release (adversarial B2 option). A source-tree or app-local provider prints its import package
in place of a distribution.

### 8. Collisions and copies

- Two contracts mapping to one stem (`a.b_c.v1`, `a_b.c.v1`) are refused in every path, naming
  both; the Protocol name and the constant collide too, so renaming the file alone is not
  enough.
- Two DTO copies of one provider class in one process (an app's and a module's) are allowed and
  decode independently on the typed path; a hand-written `ContractRef(name)` decodes into the
  first loaded copy, as today. The copy registry is keyed per contract and shape, so a
  replacement provider's results decode into the consumer's copy on the untyped path too
  (contracts PoC surprise 4; KB-23).

### 9. C5 leaves the kernel

`sandloom/shared/membership.py` is deleted (option (a)): the ordering rule is stated in
tenancy's `member_*` event descriptions, which reach every consumer with the schema; its one
importer, a test, inlines the 15 lines. A generic "optional version within `.vN`" primitive in
`sandloom.sdk` (option (b)) is added only when a second producer needs it.

### 10. Migration list

`core/cli/generated.py` (planner, kernel-home branch removed), `core/cli/dto_copies.py`
(final-layout rendering, `extra`), `core/cli/add.py` (`add contract`, `add module`),
`core/cli/scaffold_deploy.py` (`CONTRACT_DTO_PACKAGES`), `core/contract_compat.py` and
`core/rpc/readiness.py` (lookup, narrowing, refusals, kernel families), archcheck C012 (a DTO
must be copyable, not copied into a shared package) and C025 (reads the producer's
`module.snapshot.json`), `sdk/_prim/docs_wire.py` (`sandloom docs <contract>` reads producers'
snapshots and consumer copies), `.importlinter` (`sandloom.shared.membership`),
`tools/packaging/public-source.json`, `architecture_rules.yml`, the tests listed in contracts
PoC §3.12, `docs/reference/testing.md` and recipes that name `sandloom.shared.contracts`,
error-code texts, CLI templates, and the ledger entry C-GEN-03 (its kernel-home clause goes;
owner approval under I-16, it removes a case that no longer exists).

### 11. When B2 is adopted

B2 (consumer-side generation from a provider-published declaration) is added only when one of
these is shown: (a) a real consumer must generate against a provider it cannot install;
(b) rendered copies change with the build environment under one generator format after
decision 6. Neither holds today: no such consumer exists in the repository or the examples, and
the only build-environment input found was the formatter, which decision 6 removes.

### Mechanisms

| # | mechanism | kind | removes | compared against |
|---|---|---|---|---|
| B-M1 | generated subpackage inside a module distribution, planned from `consumes` | registration of a generated location | C1, C2, C4; I-05, I-11 | one framework-wide package (B0: the framework still owns module interfaces); per-owner packages (B3: a wheel and release step per provider) |
| B-M2 | expectation file beside each stub, narrowed to `consumes` | new kind of generated artifact | D4; I-12 consumer-only; I-20 | `CONTRACT_SNAPSHOT_PATHS` (an operator step per deployment); a fingerprint only (detects drift, not what broke) |
| B-M3 | refusals: provider absent at generate; no expectation at startup | startup check family | B-B6, B-F5 | a WARNING (fails I-20) |

Removed: the kernel-home branch, `sandloom.shared` as a forced artifact home, C1-C3 in the
kernel wheel.

## Alternatives considered

| alternative | why not |
|---|---|
| **B0** one framework contracts distribution | the framework still owns every module's interface (fails the goal); a module change still needs a framework-owned release |
| **B2** consumer-side generation from a provider-published declaration, now | its gains need a consumer that cannot install its provider, which does not exist; adds a declaration format and a second rendering path |
| **B3** per-owner contract packages | up to four more wheels, lockstep releases, an ADR-0010 decision-1 amendment, and a third-party replacement depending on the replaced module's contracts package |
| B1 with the consumed set read from imports (as prototyped) | two sources that can disagree with the declared `consumes`; owner #9 makes `consumes` the single source |
| expectation = the provider's whole describe (as prototyped) | refuses a consumer-first rolling order for a method the consumer never calls (contracts PoC §3.4) |
| rendering DTO copies through the project's ruff (today) | the formatter's presence and `line-length` change bytes (contracts PoC §3.11) |
| recording the provider version in provenance | rewrites every consumer copy on every provider release |
| a refusal-free "list unchecked contracts" setting for hand-written `ContractRef` | a new setting that turns I-20 into configuration |

## Consequences

**Benefits.** A module's contract change no longer touches the kernel wheel: measured, the
`sandloom` wheel had 526 files and an identical digest before and after an additive module
change (`poc/contracts/out/p2-kernel-wheel.txt`). A consumer-only install carries its copy and
its expectation, and D4 closes: an incompatible or unverifiable provider refuses under `strict`
and fails `/ready` under `ready`. Generation is byte-identical across hash seeds and composition
orders. In the DX flow the author learns one import rule (`<module>._generated.contracts.<stem>`)
and needs no `CONTRACT_SNAPSHOT_PATHS` step per deployment.

**Costs.** More files in module distributions (3 per consumed contract). Every generator writer
changes (migration list). Archcheck C012 and C025 read other sources. `sandloom docs <contract>`
for bundled contracts reads producers' snapshots. Applications lose copies of providers they do
not consume (a reviewed generated diff).

**New risks.** A consumer whose `consumes` cannot be derived must state methods by hand; a wrong
statement is caught only when the provider lacks the method. Importing a provider's `contracts`
module to render maps its ORM models into the shared metadata (harmless in `generate`, recorded
as a process-global effect, contracts PoC §3.7). Namespace scans must not touch lazy settings
proxies (`issubclass(type(value), type)`, contracts PoC §3.7).

**Migration.** Bundled modules import their own copies (billing: `tenancy_dto`, admin:
`identity_security_v1`). Applications that import `sandloom.shared.contracts.*` regenerate
into `<app>.shared` (upgrade step). Three split-readiness test fixtures need expectations.

**Conditions to revisit.** Either B2 trigger of decision 11 shows. A real consumer cannot state
its methods statically for a whole class of contracts. The copy registry per contract and shape
costs more than the untyped path is worth.

## Evidence

All from `poc/contracts/results.md` (base `ceb3d66`, prototype `b1-candidate.patch`, commit
`67980bf`), wheels built and installed in clean venvs, a real RPC pair:

| proof | result |
|---|---|
| 1 consumer-only install from wheels (I-05) | **pass**: `find_spec('sandloom_tenancy')` None, `find_spec('sandloom.shared.contracts')` None; billing decodes into its own copy; `startup compat (strict): ok {"probed": ["tenancy.membership.v1"]}` |
| 2 kernel wheel unchanged by an additive module change (I-11) | **pass**: 526 files, sha256 `7d7922e781850821` both sides; no module-named file |
| 3 D4 closed, incl. replacement `acme_orgs` (I-12, I-20) | **pass**: `get_role` removed → `REFUSED ContractIncompatibleError … expected_from: …/tenancy_membership_v1.describe.json`; no expectation → `REFUSED … no expectation` / `contract_unverified` |
| 4 mixed versions both orders | **pass with a defect**: consumer-first refused for a never-called method; fixed by decision 3 |
| 5 byte-identical generation | **pass**: digest `b2f4f35ae641bd88` over 21 files under three hash seeds and reversed order |
| 6 provider absent at generate | **pass**; reuses T007's text (decision 5 gives it its own code) |
| 7 generate without composing the provider | **pass**; no network, no file outside the venv; 4 ORM tables mapped |
| 8 two copies, v1 and v2 in one process | **pass**; hand-written `ContractRef` takes the first loaded copy |
| 9 C5 moved | **pass**: rolling-upgrade test 21/21, module suites 347/347, base and candidate |
| 10 stem collision | **pass**: refused, nothing written |
| 11 pydantic minors | **pass** (`a3ab116700ac9823` on 2.13.5, 2.14.1, 2.14.3); ruff absent or `line-length=60` changes bytes |
| 12 kernel tests without modules | **pass for B's part**: base 155 failed / 40 errors, candidate 163 / 42, every new failure attributed (migration list) |
| 13 DX (contract part) | 4 commands both; files generated into the kernel 4 → 0; split operator steps 1 per deployment → 0; documentation lookups 2 → 1 |
| adversarial verdict | `adversarial.md` §3, commit `53701d9`: B1/B4 stands with kernel families exempt, the consumed set from `consumes`, closed DTOs handled |

## Fitness tests

| test | enforces | exists / new |
|---|---|---|
| kernel wheel scan: no generated file owned by a module; file list unchanged by a module-only change | decision 1; I-11 | new (KB-22), extends `check_package_distributions.py` |
| `generate --check`: planned set equals composed `consumes`; import without `consumes` refused | decision 2; owner #9 | new (KB-20) |
| byte-identical generation under hash seeds, composition orders, ruff absent and a foreign `line-length` | decision 6; I-15 | new (KB-20), from `poc/contracts/scripts/p5_byte_identical.sh`, `p11_render.py` |
| mixed-version pair N/N+1 in both orders, consumer-only, `strict` and `ready` | decision 3; I-12 | new (KB-21), from `poc/contracts/scripts/run_pair.sh` |
| no expectation → `strict` refusal, `/ready` `contract_unverified`; kernel families compared against the kernel schema under `strict` with a remote `<m>.claims.v1` | decision 4; I-20 | new (KB-21) |
| closed DTO: an added result field classified breaking | decision 3 | new (KB-20) |
| provider absent at generate: keep / refuse edited / refuse missing with its own code | decision 5 | new (KB-20) |
| stem collision refused in every path | decision 8 | new (KB-20) |
| consumer-only install from wheels in a clean venv | I-05 | new (KB-22) |
| `tests/split_conformance` | I-06 | exists |
| `tests/integration/test_revocation_pipeline.py` unchanged expectations after C5 | B-B7 | exists |
