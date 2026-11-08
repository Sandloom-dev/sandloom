# Alternatives B: where module contracts, DTOs and event schemas live

Decision B of [requirements.md](requirements.md) §2: where contract DTOs, stubs and event
schemas live, who releases them, and how a consumer gets them. Task: tm `kb-alt-contracts`
(parent `kb-assess`). Problem #4 of [problems.md](problems.md); baseline edges C1-C5, B-N11;
degradation D4 of [guarantee-map.md](guarantee-map.md) §11.

Commit read: `f40cb7f` (branch `arch/kernel-boundary`). `src/` is unchanged from `44451e4` to
`f40cb7f`, so every `file:line` below holds at both. Written blind to the solution documents
listed in the task. This file compares alternatives. It does not decide: the ADR does, after
`kb-poc-contracts`.

## 1. What the code does today

### 1.1 Who generates what, and where it goes

`sandloom generate` (`core/cli/generated.py`) builds the composition (`load_composition_app`,
`:745`). For every composed L1 module it writes two kinds of file:

| side | files | where | written for |
|---|---|---|---|
| provider | `module.snapshot.json` (events, consumers, consumes, contracts, sagas, projections), `routes.snapshot.json`, `contracts.snapshot.json` | next to the module's `__init__.py` | modules under `source_root` only (`_owned`, `:472-480`; `:629-631`); an installed package ships its own |
| consumer | stub `<shared>/contracts/<x_y_vN>.py`, DTO copies `<shared>/contracts/<module>_dto.py`, event schemas `<shared>/events/<channel>.json` | the project's one shared package (`architecture_rules.yml` `shared_package`; `ArchRules.shared_dto_package`, `core/archcheck/manifest.py:112-125`) | **every composed provider**, consumed or not (`plan`, `generated.py:571-637`) |

The stub and the copies are rendered from the provider's **live class**: the generator reads
the registered implementation (`facade.service_registry.try_get`, `generated.py:514`), its
`contract_schema` and the pydantic `FieldInfo` of each DTO (`core/cli/dto_copies.py`). So the
provider must be importable and composed in the environment where `generate` runs. When it is
not, nothing is written and nothing says so (baseline §13.2: no tenancy stub when tenancy was
absent; scenario B-B6).

In the framework repository `shared_package` is `sandloom.shared` (`architecture_rules.yml:17`),
a kernel package. So the stubs of `billing`, `identity`, `tenancy`, their DTO copies and 34
event schemas are written into the kernel and ship in the `sandloom` wheel
(`packages/sandloom/pyproject.toml:129-140`; baseline C1-C4).

In an application the same code writes into `<app>.shared`. Applications already hold their
own copies of the bundled modules' artifacts: `examples/helpdesk/helpdesk/shared/contracts/`
has all five bundled stubs and both DTO modules, and `shared/events/` all 34 schemas. No
helpdesk module imports them (`grep` for `shared.contracts` outside `shared/`: none). The app
copy and the kernel copy differ only in the line that imports `ContractRef` / `dto_copy`
(baseline §13.2). ADR-0010's note of 2026-10-28 already states the rule for applications: "an
application that composes an installed module generates the copies into its own shared
package" (`docs/adr/0010-public-api-1.0.md:306-308`).

### 1.2 Who imports the kernel's module artifacts at run time

| importer | imports | evidence |
|---|---|---|
| `sandloom_billing` | tenancy's `MemberState` copy | `sandloom_billing/services/tenant_resolver.py:56` |
| `sandloom_admin` | `IDENTITY_SECURITY_V1` stub | `sandloom_admin/startup.py:14` |
| `sandloom/shared/membership.py` (C5) | `MemberState` copy | `shared/membership.py:32`; only importer: `tests/integration/test_revocation_pipeline.py:41` |
| `sandloom docs <contract>` | reads `sandloom.shared.contracts` / `.events` files, then `<app>.shared` | `sdk/_prim/docs_wire.py:9-11`, `:31`, `:49-56` |
| `sandloom_admin` console | reads `admin-manifest.json` from `CONTRACT_DTO_PACKAGES`, then `sandloom.shared` | `sandloom_admin/api.py:56` |

Cross-module consumption between bundled modules is two optional contracts: billing →
`tenancy.membership.v1`, admin → `identity.security.v1` (their `module.snapshot.json`
`consumes`). The kernel itself calls three contracts by name string (B-R1, B-R4, B-R5) and
imports no stub. Text-only mentions: recipes (B-N11, `sdk/_prim/recipes.py` holds strings),
error-code texts (`core/error_codes.py:141`, `:158`, `:162`, `:312`), two CLI templates
(`core/cli/templates/api-cheatsheet.md:22`, `module-AGENTS.md:12`, `:28`), and
`docs/reference/testing.md:231-232`, which tells users to import
`sandloom.shared.contracts.identity_contact_v1`. 14 test files read the kernel's artifacts.

### 1.3 The kernel-home branch in the generator

Generated files import `ContractRef` and `dto_copy`. Because the framework's own generated
files sit inside the kernel, and the kernel never imports `sandloom.sdk` (import-linter
contract `kernel-never-imports-sdk`, `.importlinter:210-218`; G-SDK-02), the generator has a
second branch that makes them import the kernel home instead:

- `KERNEL_SHARED = "sandloom.shared"` and `contract_refs_home` (`core/cli/dto_copies.py:83-97`):
  kernel home `sandloom.core.descriptors.contract_refs` with no marker when writing into
  `sandloom.shared`, else public `sandloom.sdk.contracts` with the `# sandloom: advanced`
  marker;
- `_shared_packages` always lists `sandloom.shared` (`generated.py:483-488`); `plan` defaults
  `shared_package="sandloom.shared"` (`:576`); `ArchRules.shared_dto_package` falls back to it
  for the framework tree (`manifest.py:113-118`);
- `DEFAULT_CONTRACT_DTO_PACKAGES = ("sandloom.shared",)` (`core/module_api/_provider.py:114`);
- the ledger pins the branch: C-GEN-03, test
  `tests/sdk/test_sdk_generated.py::test_the_framework_tree_stubs_keep_the_kernel_home`
  (`:54`).

This branch exists only because module artifacts are generated into a kernel package. Every
alternative below that moves them out lets it go. `ContractRef` and `dto_copy` themselves stay
in `core/descriptors/contract_refs.py`: the kernel's decoder needs the copy registry
(`contract_wire._resolve_ref`, `core/contract_wire.py:759-772`).

### 1.4 Compatibility check and D4

At startup and in `/ready`, a consumer compares each remote-bound contract's live `describe`
with an **expectation**. The expectation comes from `CONTRACT_SNAPSHOT_PATHS`, else from the
`contracts.snapshot.json` of a package found by the contract's first segment
(`billing` → `sandloom_billing`, or a sibling of a loaded module:
`core/contract_compat.py:140-155`, `:175-213`). Without that package: a WARNING "No contract
snapshot for remote contract …" (`core/rpc/readiness.py:325-332`), no comparison, and startup
also under `strict` (D4; compat probe). The same happens for a third-party provider whose
package is not named after the contract's first segment (guarantee-map §7, row "compatibility
check", column 4), and for app-local providers in a split (validation.md §1.5 item 4).

### 1.5 Provenance today

| item | exists | where |
|---|---|---|
| contract name and version | yes, in the name (`x.y.vN`), enforced by the registry | `core/module_api/_service_registry.py:56-76` |
| source class of a stub | yes, docstring "Generated from `<module>.<qualname>`" | `generated.py:320` |
| source class of a DTO copy | yes, `@dto_copy("<module>:<qualname>")`, registered by origin | `core/descriptors/contract_refs.py:51-65`; `dto_copies.py:617` |
| schema fingerprint | **in the provider only**: `ContractSchema.hash` (sha256 of the describe, 32 hex), per-model `schema_hash` (sha256 of `model_json_schema`, 16 hex); in `contracts.snapshot.json` and on the wire for codec negotiation | `_provider.py:98-104`; `contract_wire.py:704-710`; `core/rpc/proxy.py:488`, `core/rpc/dispatch.py:124` |
| schema fingerprint in the consumer's copy | **missing** | — |
| generator version | **missing** (only the label "Public API 1.0" in `HEADER`, `dto_copies.py:52`) | — |
| provider distribution name and version | **missing** | — |
| link from a consumer copy to the provider snapshot | **missing** (only the shared contract name) | — |

The describe document is not enough to render a stub: it carries field types and `required`,
but not defaults, constraints, aliases, `model_config` or docstrings (checked on
`sandloom_tenancy/contracts.snapshot.json`, `get_member_state.returns`). The per-model
`schema_hash` does cover them, because `model_json_schema` includes constraints, defaults and
descriptions.

### 1.6 Collisions today

- **Stub file names.** `stub_names` joins the contract's segments with `_`
  (`generated.py:98-103`), and the registry allows `_` inside a segment
  (`_service_registry.py:72-76`). So `a.b_c.v1` and `a_b.c.v1` both become
  `a_b_c_v1.py` with constant `A_B_C_V1`. Nothing checks for this. Latent in every
  alternative that generates into a shared directory.
- **DTO modules.** One `<short>_dto.py` per provider module (`dto_copies.py:67-69`); two
  composed modules with one short name are refused at startup (SANDLOOM-B020).
- **DTO class names** inside one file: equal names get an origin-hash suffix
  (`Resolver.copy_name`, `dto_copies.py:145-158`).
- **One contract name, two providers** in one composition: refused
  (`ServiceAlreadyRegistered`, `_service_registry.py:106-107`). Across packages nothing owns a
  contract name (guarantee-map §7, boot probe run 2). Small fix A covers only the reserved
  families `<m>.claims.v1` / `<m>.facts.v1` (owner-answers #A).
- **Two copies of one DTO in one process** (an app's copy and the kernel's, as in helpdesk
  with billing loaded): the typed path decodes into the class the caller's stub names
  (`sdk/_decl/contracts.py:76-82`); the untyped fallback for a hand-written
  `ContractRef(name)` takes the **first loaded** copy (`contract_wire.py:765-767`).

## 2. Criteria

Every alternative goes through the decision-B scenarios of requirements.md §5.B (B-H1..B-H4,
B-B1..B-B7, B-F1..B-F5), plus the task's extra cases, in mono and split:

| id | case | source |
|---|---|---|
| S1 | new DTO version (additive) | B-H1 |
| S2 | independent upgrades, both orders | B-B1, A-B4 |
| S3 | two contract versions in one consumer | B-B2 |
| S4 | provider absent at run time | B-H2, B-F2 |
| S5 | consumer-only install (P2: `sandloom` + consumer wheel, no provider wheel) | I-05, problem #4 |
| S6 | third-party provider (a replacement serving an existing contract; a new contract from another repository) | goals.md "replaceability", guarantee-map §7 col. 4 |
| S7 | generator change, contract unchanged (byte-identical rule) | B-H3, I-15, answer #3 |
| S8 | old consumer → new provider | B-B3 |
| S9 | new consumer → old provider | B-B4 |
| S10 | provider not installed where `generate` runs | B-B6 |
| S11 | consumer-only, `strict`, provider lacks a declared method | B-F5 (D4) |
| S12 | two packages ship artifacts for one contract name | B-F4 |
| S13 | event schema gains an optional field | B-H4 |
| S14 | rolling upgrade with version-less `tenancy.member_*` events after C5 moves | B-B7 |
| S15 | stale or hand-edited generated file | B-F1 |
| S16 | breaking change inside `.v1` | B-F3 |

Invariants that decide: I-05, I-11, I-12, I-15, I-17 (H-8), I-20 (D4), H-9, H-11, H-12, H-17.
I-14 is untouched by every alternative here: none adds request-path work (§7.4 notes one
option it enables).

**Mono and split.** No alternative here changes the generated artifacts between mono and split:
a stub and its copies are the same files in both, and a local call already converts to the
copy's class like a remote one (`sdk/_decl/contracts.py:57-58`; ADR-0010 note). What differs
is only whether the provider's package is present in the consumer's process (S4, S5, S11) and
whether the compatibility check has anything to compare. The scenario tables mark "mono" only
where the answer differs.

## 3. The alternatives

Five candidates. B0 is the obvious "just move the files" and is listed to show why it is not
sufficient. B1 is the minimal sufficient one. B2 and B3 are the larger designs. B4 is the
hybrid (B1 now, B2's source as a gated addition).

### B0. Move the artifacts into one framework contracts distribution

The files of `sandloom/shared/contracts/*_v1.py`, `*_dto.py` and `shared/events/*.json` move
into a new distribution `sandloom-contracts` (import `sandloom_contracts`), released by the
framework whenever any first-party contract changes. `shared_package: sandloom_contracts` in
the framework tree. Everything else unchanged.

- Kernel-home branch: removed (the target is no longer a kernel package).
- I-11: the `sandloom` wheel no longer changes on a module's contract change, but
  `sandloom-contracts` does, for every module.
- Fails the goal: "the framework owns no module's interface or artifact" (goals.md, "What the
  rule means"; roadmap-review G3 as recorded in goals.md). The framework still owns and
  releases billing's, identity's and tenancy's artifacts, only in another wheel. A third-party
  provider gets no equivalent home, so first-party and third-party modules are not
  treated alike (I-13).
- D4 unchanged: the expectation is still looked up in the provider's package.
- New mechanisms: one new wheel. Replaces nothing that the other alternatives keep.

**Rejected** at the goal level. It is not analysed further below except in the summary table.

### B1. Consumer-owned copies with the existing generator (minimal sufficient)

**Rule.** Generated consumer artifacts live in the **distribution that imports them**. An
application keeps its one `<app>.shared` package (unchanged). A module that ships as its own
distribution (the bundled modules, any ecosystem module) holds the copies it consumes inside
its own package.

Changes:

1. **Framework modules generate their own consumer copies.** In the framework tree (top-level
   modules, `manifest.py` `top_level_modules`) consumer artifacts are written per consuming
   module into a reserved subpackage of that module, e.g.
   `sandloom_billing/_generated/contracts/tenancy_membership_v1.py` and `tenancy_dto.py`, and
   `sandloom_admin/_generated/contracts/identity_security_v1.py` and `identity_dto.py`. Only
   for contracts the module consumes. The set is read statically, by AST, from the module's
   imports of its own `_generated.contracts.<stem>` modules, so a developer writes the import,
   runs `sandloom generate`, and the file appears. No new CLI command. `architecture_rules.yml`
   drops `shared_package: sandloom.shared`.
2. **The kernel ships no module artifact.** `sandloom/shared/contracts/*_v1.py`, `*_dto.py`
   and `shared/events/*.json` are deleted. `sandloom.shared` stays a home for platform types
   (`subject`, `authz`, `guards`), and stays in `DEFAULT_CONTRACT_DTO_PACKAGES` for platform
   DTOs only. The kernel-home branch (§1.3) is deleted: no generated file is written into a
   kernel package any more, so every generated file imports `sandloom.sdk.contracts` and
   `kernel-never-imports-sdk` holds with no exception. C-GEN-03 loses its kernel-home clause:
   a ledger text change, recorded with the owner's approval (I-16; it removes a case that no
   longer exists, it weakens nothing).
3. **Event schemas.** The producer's `module.snapshot.json`, already shipped in each module
   wheel (`events` section), is the published schema. A consumer that declares a view or an
   `@on` consumer of another module's channel gets a copy in its own `_generated/events/`
   (module distributions) or `<app>.shared/events/` (applications, unchanged). In the
   framework tree no module consumes another module's events today, so the 34 schemas simply
   leave the wheel. The view check (`core/archcheck/runner.py:644-651`) reads the producers'
   snapshots when the producer is in the same tree.
4. **The expectation travels with the copy (D4).** Next to each generated stub, `generate`
   writes the provider's describe entry for that contract (a copy of the contract's entry in
   the provider's `contracts.snapshot.json`), e.g. `tenancy_membership_v1.describe.json`.
   `expected_contracts` looks first beside the stub the consumer declared (the Protocol's
   module, known from `Contract(REF)`), then `CONTRACT_SNAPSHOT_PATHS`, then the provider
   package as today. With no expectation for a required remote contract, `strict` refuses
   startup naming the contract, and `ready` fails `/ready` with `contract_unverified`; never a
   WARNING alone (I-20, B-F5). The exact outcome for a hand-written `ContractRef` without a
   stub is an owner point (§9).
5. **Provider absent where `generate` runs (B-B6).** If a module imports a stub whose provider
   is not importable, `generate` keeps the committed copy when there is one (it is the
   consumer's pinned version; `--check` verifies it is unedited against its own fingerprint)
   and otherwise refuses, naming the module, the contract and the missing provider. The
   provider is a development dependency of the consumer, not a run-time one. Today's second
   condition, that the provider must also be composed by the app's root (baseline §13.2,
   `ContractBindingError` at `generate`), is dropped: the generator imports the provider's
   L1 declarations without composing it. The PoC must prove this works without side effects.
6. **Provenance header** on every generated consumer file (§6): contract, fingerprint,
   provider distribution, generator format.
7. **Stem collision check**: `generate` refuses two contracts that map to one file name
   (§1.6), naming both.
8. **C5** leaves the kernel (§5).

What is not changed: the generator still renders from the provider's live class; apps keep
`<app>.shared` and "every composed provider" generation (switching apps to consumed-only is an
option, §9); no new wheel; no new import category.

**New mechanisms (H-12), each with what it replaces and what it was compared against:**

| # | mechanism | removes / provides | replaces | compared against |
|---|---|---|---|---|
| B1-M1 | a generated subpackage inside a module distribution, filled from the module's own imports | C1, C2, C4 (kernel ships module stubs/copies); I-05, I-11 | `sandloom.shared` as the framework's artifact home; the kernel-home branch | one shared package for all framework modules (B0: fails the goal); per-owner packages (B3: +1 wheel per provider) |
| B1-M2 | expectation file next to each stub (a new kind of generated artifact: a copy of an existing one) | D4; I-12 consumer-only; I-20 | the provider-package lookup as the only source; `CONTRACT_SNAPSHOT_PATHS` as an operator step | `CONTRACT_SNAPSHOT_PATHS` (needs an operator step per deployment: fails "no extra operator step", §4.2); a fingerprint only (enough to detect drift, not to say what broke) |
| B1-M3 | refusal at `generate` when a consumed provider is absent and no copy is committed; refusal or `/ready` failure at startup when no expectation exists | B-B6; B-F5 | a silent absence; a WARNING | none cheaper: both are the minimum the scenarios accept |

Removed mechanisms: the kernel-home branch (§1.3); `sandloom.shared` as a forced shared
package; C1-C3 shipping in the kernel wheel. The provenance header and the stem check are
additions to existing files and checks, not mechanisms by the §4.2 definition.

### B2. Consumer-side generation from a provider-published declaration

Everything in B1, plus: the consumer never imports the provider to generate.

1. **The provider publishes a declaration.** The provider's own `generate` (provider side,
   like `contracts.snapshot.json` today) writes `contracts.snapshot.json` in a format 2 that
   carries, per contract, the describe document **and** what the copier reads from the live
   class today: each DTO's fields with defaults, constraints, aliases, `Literal`/`Enum`
   values, nested models, allowed `model_config` options and docstrings; each method's
   docstring and parameter defaults; plus `module.snapshot.json`'s event schemas. Call it the
   declaration. It ships in the provider wheel as package data, as the snapshot does today.
2. **The copier is split into analyse and render.** `dto_copies.py` today walks pydantic
   `FieldInfo` and writes Python in one pass. B2 splits it: the provider side analyses the
   live class into the declaration; the consumer side renders Python from the declaration
   only. The provider's own repository renders from its declaration too, so there is one
   render path.
3. **Where the consumer gets the declaration at generate time**, in order: an installed
   provider distribution, located without importing it (`importlib.util.find_spec`, as
   `contract_compat.module_package_dir` does, `contract_compat.py:92-100`); the consumer's
   committed copy (the expectation file of B1-M2 *is* the declaration copy); an explicit
   source, `sandloom generate --from <file|dir>`, for a provider whose code the consumer
   cannot install (another repository, a closed product). With none, the refusal of B1-M3.
4. **Byte-identical by construction.** The rendered files are a pure function of
   (declaration, generator format). The provider's code, the pydantic version and the
   composition of the build environment are no longer inputs.

**New mechanisms**, beyond B1's M1-M3:

| # | mechanism | removes / provides | replaces | compared against |
|---|---|---|---|---|
| B2-M4 | declaration format 2 (a new wire format for DTO shapes, inside an existing artifact) | B-B6 positive outcome (generation without the provider); I-15 independence from the build environment | B1's live-class rendering on the consumer side; the B1-M2 expectation becomes this file | B1 (needs the provider installable at development time); JSON Schema from `model_json_schema` (does not round-trip `Literal`/`Enum` identity, aliases resolved by generators, `model_config`; the copier would still need a second reader) |
| B2-M5 | `generate --from` source option (an option of an existing command) | a third-party provider outside the build environment (S6) | installing the provider wheel as a development dependency | `CONTRACT_SNAPSHOT_PATHS` reused for generation (a run-time setting used at build time: two meanings for one name) |

Cost not counted as a mechanism: a renderer from the declaration, and a losslessness property
test from class → declaration → copy (ADR-0010 note, "Losslessness") instead of class → copy.

### B3. Per-owner contract packages

Each provider releases a second distribution with its generated artifacts:
`sandloom-tenancy-contracts` (import `sandloom_tenancy_contracts`) with the stubs, DTO copies,
event schemas and `contracts.snapshot.json`, generated by the provider's `generate` into
`packages/sandloom-tenancy-contracts/`. Consumers depend on it with a version range and import
from it. Applications stop generating copies of installed modules into `<app>.shared` and
import the contracts packages; app-local modules keep `<app>.shared`. The provider package
could optionally depend on its own contracts package. Hand-written domain atoms could live
there (research-authz-portability.md §4.5, option (v)).

- Kernel-home branch: removed.
- D4: `expected_contracts` looks the snapshot up in the contracts package (a lookup by
  distribution, not by the contract's first segment). A consumer-only install has it.
- New import category: a module imports another owner's contracts package. That is neither
  `sandloom.sdk`, nor a generated stub in its own tree, nor its own package: ADR-0010
  decision 1 (`docs/adr/0010-public-api-1.0.md:56-58`) and H-8 need an amendment; `sandloom
  check` C001/C002, `.importlinter` and `test_module_boundaries.py` change. Whether it counts as
  "importing module B" (H-3, I-04) must be ruled explicitly.
- A new dependency edge between module families: `sandloom-billing` → `sandloom-tenancy-contracts`.
  `docs/contributing/versioning.md:76-83` states that no module package depends on another; this
  edge is to a package the other module owns.

**New mechanisms:**

| # | mechanism | removes / provides | replaces | compared against |
|---|---|---|---|---|
| B3-M1 | one new wheel per provider, with its own version and release | C1-C4; I-05, I-11 | the consumer's generated copies of installed modules | B1 (no new wheel) |
| B3-M2 | a new allowed import category (contracts packages) | lets consumers import released artifacts | the "generated stubs and DTOs in the consumer's tree" category | B1/B2 (fit ADR-0010 decision 1 as written) |
| B3-M3 | a check that no two installed distributions ship artifacts for one contract name | B-F4 for released artifacts | — | B1/B2, where no provider ships importable artifacts, so the risk does not arise |
| B3-M4 | release automation and publishing steps per contracts package | keeping provider and contracts package in step | — | B1/B2 (nothing to release) |

### B4. Hybrid: B1 now, B2's declaration only behind a trigger

B1 as specified, with one forward-compatible choice: the expectation file of B1-M2 is written
in a shape that B2's declaration can extend (the describe entry under a `describe` key, room
for `dtos` and `docs` keys). B2-M4/M5 are added only when one of these is shown: (a) a real
consumer must generate against a provider it cannot install (S6, second case); (b) the PoC
shows that the rendered copies change with the pydantic version or the build environment under
one generator version (I-15). Until then the consumer renders from the live provider class, as
today.

This is not a third rendering path: when B2 lands, both sides switch to render from the
declaration (B2 step 2). Two live paths at once (render from the class when installed, from
the declaration when not) would need a byte-identity proof between them, and is not proposed.

## 4. Scenarios

Legend: **pass** (outcome of requirements.md §5.B observed or by construction), **pass+**
(passes with a better outcome than required), **cost** (passes at the stated price), **fail**.
"Today" is `f40cb7f`.

### 4.1 Happy path

| id | today | B1 | B2 | B3 |
|---|---|---|---|---|
| S1 / B-H1 new DTO version: billing adds an optional field to a `billing.credits.v1` DTO, releases `sandloom-billing` only | **fail** I-11: the kernel's copies are regenerated, so the `sandloom` wheel changes | **pass**: billing's wheel only; the kernel wheel has no billing file; consumers regenerate when they choose; an old copy ignores the extra field (pydantic default `extra="ignore"`; B-F1 runtime clause) | **pass**: as B1; billing's declaration changes in its wheel | **cost**: two releases in step (`sandloom-billing`, `sandloom-billing-contracts`) |
| S4 / B-H2 provider absent from the consumer process; app module calls `billing.credits.v1` over a binding | pass (app or kernel copy), compat skipped with a WARNING when the provider wheel is absent | **pass+**: app copy and its expectation; compat runs | **pass+**: as B1 | **pass**: contracts package installed; compat runs if B3's lookup change lands |
| S7 / B-H3 generator updated, contract unchanged | partial: diffs appear, nothing says they come from the generator | **pass** with the provenance header: same fingerprint, new generator format → `generate` reports each diff by file as a generator change (SANDLOOM-T007 path), compat check runs on the unchanged fingerprint, snapshot review; old files keep working (they import only public `sandloom.sdk.contracts`) | **pass+**: the declaration is unchanged by construction, so a consumer diff can only come from the renderer; the attribution needs no inference | **cost**: the contracts packages were rendered by the provider's generator; new rendering reaches consumers only through a re-release of each contracts package; a framework minor requires those releases (`versioning.md:79-82`) |
| S13 / B-H4 optional field on an event `.v1` | **fail** I-11: the kernel's schema copy changes | **pass**: the producer's `module.snapshot.json` changes in its wheel; existing consumers ignore the field; SANDLOOM-T005 passes | **pass** | **cost**: contracts package release |

### 4.2 Boundary

| id | today | B1 | B2 | B3 |
|---|---|---|---|---|
| S2 / B-B1 identity and billing upgraded at different times (split), both orders | holds in the repository; consumer-only compat skipped | **pass**: each consumer holds its pinned copy and expectation; compat compares in both orders | **pass** | **pass** within the declared ranges; a consumer that needs a newer contracts package for a new method forces that version on every module in the same environment |
| S3 / B-B2 two versions (`billing.credits.v1`, `.v2`) in one consumer | pass when the provider uses distinct classes per version | **pass**: two stubs; copies in one `billing_dto.py` from one provider release; equal names disambiguated by origin (`dto_copies.py:145-158`); a class shared by both versions is one copy, correct because the wire shape is one. Limit (all alternatives): one consumer cannot hold v1 copies from provider release N and v2 from N+1 at once | **pass** | **pass** within one contracts-package release; two consumer modules of one application that need different contracts-package releases cannot coexist (one version per environment) |
| S8 / B-B3 old consumer → new provider (additive) | pass in the repository; consumer-only: no comparison | **pass**: expectation N vs live N+1, additive, `/ready` 200 | **pass** | **pass** |
| S9 / B-B4 new consumer → old provider, missing method | 501 at the call (unchanged); `/ready` fails only where the provider package is installed | **pass**: 501 at the call; `/ready` `contract_incompatible` from the consumer's own expectation, provider package absent or not | **pass** | **pass** |
| B-B5 two applications compose one installed provider, each with its own copies | **pass** (helpdesk and the framework both hold copies) | **pass**; the provider package carries no app file | **pass** | **pass** trivially: apps import the contracts package, no copies |
| S10 / B-B6 provider not installed where `generate` runs | **fail**: silent absence (baseline §13.2) | **pass** by refusal, naming the module, contract and missing provider; a committed copy is kept and verified | **pass+**: generated from the declaration (installed wheel located without import, committed copy, or `--from`) | **pass**: nothing to generate; the consumer installs the contracts package |
| S5 consumer-only install (P2): `sandloom` + `sandloom-billing`, no tenancy | imports only because tenancy's copy is in the kernel wheel (I-11 violated); boot also fails on celery (problem #9, separate) | **pass**: billing's wheel carries its tenancy copy and expectation; optional contract → `None` or a bound proxy | **pass** | **pass** with a new install edge `sandloom-billing` → `sandloom-tenancy-contracts` |
| S6a third-party replacement: `acme_orgs` serves `tenancy.membership.v1`, `sandloom-tenancy` absent | works at run time; compat looks for a `tenancy` package: WARNING, nothing compared (guarantee-map §7) | **pass+**: consumers' copies decode the same wire shape; the expectation beside the stub is compared with acme's live describe, so a deviation is found | **pass+** | **cost**: consumers keep importing `sandloom_tenancy_contracts` (owned by the module acme replaces); acme extending the contract needs a competing package for one contract name: S12 |
| S6b new contract from another repository (`acme.quota.v1`) | the consumer must install acme's wheel to generate | **cost**: acme's wheel as a development dependency | **pass+**: acme's declaration file through `--from` or the installed wheel, no acme code imported | **pass**: acme publishes `acme-quota-contracts` |
| S14 / B-B7 version-less `tenancy.member_*` in flight after C5 moves | — | **pass** if the decision logic is unchanged and `tests/integration/test_revocation_pipeline.py` passes unchanged in its expectations (§5) | same | same |

### 4.3 Failure

| id | today | B1 | B2 | B3 |
|---|---|---|---|---|
| S15 / B-F1 stale or hand-edited stub or copy | **pass** (SANDLOOM-T007, C012) for the one shared package | **pass** after `generate --check` covers the module-local generated packages too; the header fingerprint lets `--check` detect an edited committed copy whose provider is absent | **pass** | provider repository: pass; consumer side: an installed package is not checked (out of scope) |
| B-F2 provider implementation absent, required / optional | **pass** | **pass** (run-time path unchanged) | **pass** | **pass** |
| S16 / B-F3 breaking change inside `.v1` | **pass** (T004 on the provider snapshot; T007 on the stub) | **pass**: provider side unchanged; consumer regeneration refuses (T007, `stub_problems`, `generated.py:346-358`) | **pass**: the declaration diff is classified on the provider side before release | **pass** on the provider side |
| S12 / B-F4 two packages ship artifacts for one contract name | no check; inside one composition the second registration is refused | **pass**: no provider ships importable artifacts; a duplicate inside one composition is refused at registration; two candidate sources at `generate` cannot arise (the generator reads the composed or imported provider) | **pass** with a small check: two installed declarations for one contract name → `generate` refuses naming both distributions | **cost**: needs B3-M3, a scan of installed distributions; otherwise two importable shapes |
| S11 / B-F5 consumer-only, `strict`, provider lacks a declared method | **fail** (D4: WARNING, start) | **pass**: comparison runs; `/ready` fails `contract_incompatible`; `strict` refuses when the provider is reachable at startup | **pass** | **pass** once compat looks in the contracts package |

### 4.4 Mono vs split

Same verdicts in mono and split for every row above, with three differences:

- S4, S5, S11 only arise in split or a consumer-only install; in mono the provider is local,
  the compatibility check does not run (no remote binding), and every alternative behaves as
  today.
- In mono, B1 and B2 load more than one copy of a DTO when an application and a module
  distribution both consume it (`<app>.shared` and `sandloom_billing/_generated`). The typed
  path is unaffected (each caller decodes into its own stub's class). A hand-written
  `ContractRef(name)` with no Protocol decodes into the first loaded copy
  (`contract_wire.py:765-767`), which may be another distribution's class. This already happens
  today (helpdesk + kernel copy); B1 adds more cases. B3 does not have it.
- H-6 (same module code, mono or split, by configuration) holds in all: no alternative puts
  deployment-specific content in a generated file.

## 5. `shared/membership.py` (C5)

Hand-written consumer logic for tenancy's member events: `member_event_version`,
`member_event_action` (apply / skip / refresh), `refreshed_state_wins`
(`shared/membership.py:37-64`). Its only module edge is the `MemberState` copy, and only
`refreshed_state_wins` uses it, for `.version`. Its only importer is a test. Docstrings and an
error text point to it (`sdk/_decl/projections.py:328-329`, `sandloom_tenancy/__init__.py:44`,
`sandloom_tenancy/contracts.py:95`, `docs/modules/between/projections.md:133`).

| option | effect | cost |
|---|---|---|
| (a) delete from the kernel; the rule is stated in tenancy's `member_*` event docstrings, which reach every consumer through the event schema's `description` (`shared/events/tenancy.member_added.v1.json:2`) and the generated copy; the test inlines the 15 lines | no kernel edge; a consumer in another process gets the rule with the schema | each consumer writes the rule itself |
| (b) a generic primitive "optional version within `.vN`: apply / skip / refresh" in `sandloom.sdk` (no tenancy names; takes the stored and incoming versions) | one implementation for every producer with optional versions | a new public name (I-17 review); justified only by a second consumer (H-12) |
| (c) move into `sandloom_tenancy` as code | — | a consumer in another process cannot import it (H-3); fails |

All three alternatives B1-B3 take (a), and keep (b) for when a second producer needs it. B-B7
passes if the decision table is unchanged and the rolling-upgrade tests pass with unchanged
expectations (requirements.md, B-B7).

## 6. Provenance: what each alternative adds

Proposed header for every generated consumer file (B1, B2; B3 puts it in the contracts
package):

```
# Generated by `sandloom generate`: never edit by hand.
# contract: tenancy.membership.v1  fingerprint: fbf42e7650aa13c0878c442f6080d419
# provider: sandloom-tenancy (sandloom_tenancy.contracts.MembershipReadContract)
# generator: format 2
```

| item | today | proposed | why |
|---|---|---|---|
| contract name and version | in the name | unchanged | — |
| schema fingerprint | provider only | the describe `hash` in the consumer's header and expectation | links the copy to the provider's `contracts.snapshot.json` entry by content (equal hash = same shape); lets `--check` verify a committed copy without its provider (S10, S15); separates a contract change from a generator change (S7) |
| generator version | none | a **generator format** number, bumped only when the rendered output changes | the package version would change every header on every `sandloom` release with no content change, against H-19 ("generation runs only when a public declaration changes") and answer #3 (a diff needs an explanation) |
| provider distribution | none | name and source class | names the owner for a reader and for `sandloom docs` |
| provider version | none | **not recorded** in generated files; `uv.lock` records it | recording it would rewrite every consumer copy on every provider release with an unchanged contract (the H-19 problem again). Open point §9 |
| link to the provider snapshot | none | the fingerprint (content link), plus the expectation file (a copy of the snapshot entry) | an exact path would break in a consumer-only install |

What stays missing in every alternative: the consumer cannot prove which provider *release*
produced a copy, only which shape. The fingerprint does not cover method docstrings (the
describe has none), so a docstring-only change rewrites a stub without changing the
fingerprint; the format-2 declaration of B2 would include docs in its own fingerprint if
needed.

## 7. Cross-cutting effects

### 7.1 ADR-0010, `kernel-never-imports-sdk` and the kernel-home branch

| | today | B0 | B1 | B2 | B3 |
|---|---|---|---|---|---|
| generated files inside the kernel | 7 modules + 34 schemas | none | none | none | none |
| kernel-home branch (§1.3) | needed | deleted | deleted | deleted | deleted |
| `kernel-never-imports-sdk` | holds, with the branch as the workaround | holds, no exception | holds, no exception | holds, no exception | holds, no exception |
| ADR-0010 decision 1 (allowed imports) | as written | as written | **as written**: stubs and copies in the consumer's own tree are "generated contract stubs and contract DTOs" | as written | **amendment**: a new category, another owner's released package |
| C-GEN-03 ledger text | pins the branch | changed | changed | changed | changed |

### 7.2 I-17 and generated consumer APIs (decision C interplay)

Decision C's candidate (iv) generates typed domain atoms next to the stub
(research-authz-portability.md §4.4). requirements.md I-17 and H-8: importing such atoms needs
an ADR-0010 amendment whatever decision B does (answer #18).

- **B1, B2:** atoms are generated into the same consumer-owned location as the stub
  (`<app>.shared/contracts/` or `<module>/_generated/contracts/`). The amendment can be narrow:
  "generated consumer interface = stubs, DTO copies and generated atoms, all in the consumer's
  generated package". The kernel never ships tenancy's or billing's atoms (research-authz §7
  trade-off "until B is decided" is closed).
- **B3:** atoms would be hand-written in the provider's contracts package
  (research-authz §4.5). The amendment is wider (import of another owner's released package
  that carries code), and decision C's metadata check (H-20) must parse installed packages.
- Beginner tier: unaffected by decision B itself. Moving `OrgRole` & co. out of `sandloom.sdk`
  is decision C's migration.

### 7.3 H-9 (no alias), migration

Deleting `sandloom.shared.contracts.*` and `sandloom.shared.events` is a breaking change of the
prerelease: no compatibility module is left behind (H-9). Upgrade step for an application:
`sandloom generate` (its `<app>.shared` already holds copies of every composed module, §1.1),
then replace `sandloom.shared.contracts.` with `<app>.shared.contracts.` in imports; `sandloom
check` already refuses imports outside the allowed list. Wire compatibility is unaffected:
DTO copy origins (`"sandloom_tenancy.schemas.membership:MemberState"`) and event channels do not
change, so rolling upgrades between old and new images keep decoding (H-9's wire clause).

Files that change, by alternative (B1/B2): `sandloom_billing/services/tenant_resolver.py`,
`sandloom_admin/startup.py` and its test; docstrings in `sandloom_tenancy`, `sandloom_identity`;
`sdk/_prim/docs_wire.py` (reads producers' snapshots and consumer copies instead of
`sandloom.shared`); `sandloom_admin/api.py:56` (the `sandloom.shared` fallback for the admin
manifest goes; the manifest's home is decision A's, B-N1/B-N2); `core/error_codes.py` texts;
two CLI templates; `docs/reference/testing.md`, `docs/modules/between/projections.md`,
`docs/modules/batteries/tenancy.md`; `architecture_rules.yml`; `packages/sandloom/pyproject.toml`
comment; 14 test files, the kernel's among them moved to fixtures so they run without
batteries (answer #2). B3 adds new `packages/*-contracts` trees, CI and publishing.

### 7.4 Performance

No alternative adds request-path or startup network work: the expectation is a package file
read at startup, where the snapshot lookup already runs. I-14 and H-10 are unaffected. One
option B1/B2 enable, not proposed here: with the consumer holding the provider's describe and
fingerprint, the first use of a bound contract would no longer need a remote `describe` on the
request (I-14 remote-tier violation, `core/rpc/proxy.py:656`). The proxy would use the local
describe and confirm the fingerprint on the first response. That belongs to decision C / I-14.

### 7.5 Ecosystem modules (third-party module repositories)

Today a repository of top-level modules must set `shared_package` (`manifest.py:119-123`), and
the ADR says such a module keeps its DTOs and an application generates copies. B1 gives these
repositories a default home for their consumer copies (inside each module), the same rule as the
bundled modules, so first-party and third-party modules package alike.

## 8. Comparison

| criterion | B0 | B1 minimal | B2 declaration | B3 per-owner packages |
|---|---|---|---|---|
| Goal "framework owns no module artifact" | **fail** | pass | pass | pass |
| I-05 consumer-only install | pass | pass | pass | pass (new install edge) |
| I-11 kernel wheel unchanged by a module contract change | pass (another framework wheel changes) | pass | pass | pass |
| I-12 consumer-only compat, D4, B-F5 | **fail** | pass | pass | pass (lookup change) |
| I-15 byte-identical, generator attribution | as today | pass with header; the build environment (pydantic, provider code) remains an input | **pass+**: pure function of the declaration | cost: re-release to re-render |
| B-B6 provider not installed at generate | n/a | pass by refusal | **pass+** by generation | pass (nothing to generate) |
| B-F4 one contract name, two artifact sources | as today | pass | pass + small check | cost: new check |
| S6 third-party replacement compared | fail | pass+ | pass+ | cost |
| S6 provider outside the build environment | fail | cost (dev dependency) | pass+ | pass |
| I-17 / H-8 ADR-0010 amendment for decision B | no | **no** | **no** | **yes** |
| Kernel-home branch | deleted | deleted | deleted | deleted |
| New mechanisms (H-12, §4.2 definition) | 1 (wheel) | **3** (M1-M3) | 5 (M1-M5) | 4 (M1-M4) |
| Wheels released for one contract change | 2 (module + `sandloom-contracts`) | **1** | **1** | 2 |
| New wheels | 1 | 0 | 0 | up to 4 |
| Dependency edges added between wheels | consumers → `sandloom-contracts` | 0 | 0 | consumer modules → owners' contracts packages |
| Import change for app authors | `sandloom_contracts` | none (apps already use `<app>.shared`) | none | installed modules: `<app>.shared` → contracts packages |
| Import change for framework module authors | `sandloom_contracts` | own `_generated` | own `_generated` | contracts packages |
| Operator steps added | 0 | 0 (removes the `CONTRACT_SNAPSHOT_PATHS` need) | 0 | 0 |
| Request-path work | 0 | 0 | 0 | 0 |

## 9. What the evidence favours

**B1, delivered as B4** (B1 now; B2's declaration only when its trigger is shown).

Why:

1. **B1 extends a mechanism that already works.** Applications already generate their own copies
   of the bundled modules' contracts and events (`examples/helpdesk/helpdesk/shared`), the
   copies equal the kernel's except for one import line (baseline §13.2), and ADR-0010's note
   already states the rule for applications. B1 applies the same rule to module distributions.
   No new wheel, no new import category, no ADR-0010 decision-1 amendment for B.
2. **It removes every violation decision B owns.** C1-C4 leave the kernel wheel (I-11), the
   consumer-only install keeps its copy in the consumer's wheel (I-05), D4 is closed by the
   expectation travelling with the copy (I-12, I-20, B-F5), B-B6 becomes a named refusal, and
   the kernel-home branch disappears, so `kernel-never-imports-sdk` holds without a workaround.
   C5 leaves with option (a).
3. **It has the fewest new mechanisms (3)**, each compared against a cheaper existing one
   (H-12): `CONTRACT_SNAPSHOT_PATHS` needs an operator step per deployment; one framework-wide
   package fails the goal (B0); per-owner packages add wheels and a release step per contract
   change (B3).
4. **B2's added value depends on facts not yet shown.** Its gains are generation without the
   provider installed (S6b, B-B6 positive) and independence from the build environment (I-15).
   No consumer in the repository or examples needs a provider it cannot install, and no
   observation shows rendered copies changing with the pydantic version. B4 keeps the
   expectation file in a shape B2 can extend, so adopting B2 later does not migrate twice.
5. **B3 fits a multi-repository product world the golden path does not generate.** It costs up to
   4 wheels, lockstep releases, an ADR-0010 amendment, a collision check, and makes a third-party
   replacement depend on the replaced module's contracts package (S6a). research-authz §4.5
   reaches the same judgement for the client-package variant of decision C.

What would change the verdict: a requirement that consumers generate against closed or remote
providers (→ B2); evidence that rendering varies with the build environment under one generator
format (→ B2); an owner decision that third-party modules must publish client libraries for
non-Sandloom callers (→ B3, possibly alongside B1).

## 10. Open points

1. **Missing expectation with a hand-written `ContractRef`** (no stub, no expectation): refuse
   at startup under `strict` and fail `/ready` under `ready`, or require the application to list
   it as unchecked? I-20 forbids a WARNING alone; the owner decides which refusal.
2. **Provider version in provenance**: omit (fingerprint plus `uv.lock`, as proposed) or record
   it and accept a rewrite of every consumer copy on every provider release.
3. **Applications: consumed-only generation.** Keep "every composed provider" (today, no
   migration) or switch apps to the import-driven set that B1 uses for modules (fewer files,
   e.g. helpdesk's unused copies; a DX change).
4. **Name and shape of the module-local generated subpackage** (`_generated/contracts`,
   `_generated/events`), and the `sandloom check` rules that allow and protect it.
5. **Contract-name ownership** by first segment (`tenancy.*` registered only by the module named
   `tenancy` or the application's assigned replacement): decision A/C, beyond small fix A's
   reserved families. Decision B only needs it for S6a diagnostics.
6. **C5 option (b)**, a generic versioned-event primitive in `sandloom.sdk`: only with a second
   producer.
7. **C-GEN-03** text change in the ledger: owner approval (I-16).
8. **Admin manifest** (`sandloom_admin/api.py:56` reads `sandloom.shared`): its home is decision A
   (B-N1, B-N2); B only removes the `sandloom.shared` fallback.

## 11. What `kb-poc-contracts` must prove

For B1 (and B4's trigger measurements). Each item names the scenario and invariant it decides.

1. **Consumer-only install from wheels** (I-05, S5): clean venv with the candidate `sandloom`
   and `sandloom-billing` wheels and the declared dependencies only, no tenancy; billing imports
   its own tenancy copy; a tenancy provider in another process is bound; the result's class is
   billing's copy; `pip list` shows no `sandloom-tenancy`.
2. **Kernel wheel contents** (I-11, S1, S13): the built `sandloom` wheel lists no module-owned
   file; an additive DTO change and an optional event field in billing, regenerated, leave the
   `sandloom` wheel's file list and hashes unchanged.
3. **D4 closed** (I-12, I-20, S11, S6a): consumer-only, `CONTRACT_COMPAT_CHECK=strict`, the
   provider lacks a declared method → `/ready` fails `contract_incompatible` and startup refuses
   when the provider is reachable; the same with a replacement `acme_orgs` serving
   `tenancy.membership.v1` (no "No contract snapshot" warning; the comparison runs).
4. **Mixed versions** (I-12, S2, S8, S9): module package versions N and N+1 in two processes,
   both orders, over the split conformance script; `/ready` 200 for additive, 501 plus
   `contract_incompatible` for a missing method.
5. **Byte-identical generation** (I-15, S7): `sandloom generate` twice under two
   `PYTHONHASHSEED` values and two module orders → equal bytes; a generator-format bump with
   unchanged declarations → every diff reported by file, fingerprint unchanged.
6. **Provider absent at generate** (B-B6, S10): a module imports a stub whose provider is not
   installed → refusal naming module, contract and provider; with a committed copy → kept and
   verified; a hand-edited committed copy → SANDLOOM-T007.
7. **No composition needed to generate** (baseline §13.2): the generator reads an installed,
   uncomposed provider's L1 declarations without side effects (no environment read, no I/O,
   no registration).
8. **Two copies, two versions** (B-B2, B-B5, §4.4): an application and a module distribution both
   holding a copy of one DTO in one process; v1 and v2 stubs in one consumer; each typed call
   returns its own stub's class; record what a hand-written `ContractRef(name)` returns.
9. **C5 moved** (B-B7): the rolling-upgrade and revocation-pipeline tests pass with unchanged
   expectations.
10. **Stem collision** (§1.6): `a.b_c.v1` and `a_b.c.v1` together → `generate` refuses naming
    both.
11. **B4 trigger (b)**: render the bundled copies under two pydantic minor versions with one
    generator format; any byte difference is the evidence for B2.
12. **Kernel tests without batteries** (answer #2): the kernel's unit and architecture tests
    pass with `sandloom/shared/contracts` and `shared/events` removed and no module installed.
13. **DX** (§4.2 comparative task, contract part): base vs candidate, count files, manual steps,
    errors and documentation lookups for "add a contract in module A, consume it in module B,
    split them", including the import-then-generate flow of B1-M1.
