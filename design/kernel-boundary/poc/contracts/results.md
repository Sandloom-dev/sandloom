# PoC: consumer-owned contract copies (alternative B1)

tm `kb-poc-contracts` (parent `kb-assess`). Proves or refutes alternative B1 of
[alternatives-b-contracts.md](../../alternatives-b-contracts.md) on running code, item by item of
its §11 ("what kb-poc-contracts must prove", 1-13). Written for the ADR of decision B; it does
not decide.

## 1. Setup

| item | value |
|---|---|
| base | `ceb3d66` (branch `arch/kernel-boundary`); `src/`, `packages/` and `tests/` equal at `53701d9` |
| prototype | [b1-candidate.patch](b1-candidate.patch) (`git apply` on `ceb3d66`), built in a throwaway worktree; nothing in this checkout's `src/` or `packages/` was edited |
| services | own containers `kbpocc-pg` (`postgres:16-alpine`, 127.0.0.1:39633, `max_connections=400`) and `kbpocc-redis` (`redis:7-alpine`, 127.0.0.1:39680, `--databases 256`, dbs 3-6, 40-61); `SANDLOOM_TEST_OWNER=kb-poc-contracts[-kbase/-kcontracts]`; removed after the run |
| wheels | `uv build --wheel packages/<p>`: `dist-N` (candidate: `sandloom`, `sandloom-billing`, `sandloom-tenancy`, `sandloom-identity` 0.7.0a11), `dist-N1` (candidate + the additive change of §3.2, billing and tenancy 0.7.0a12), `dist-base` (`ceb3d66`) |
| venvs | Python 3.12, `uv venv` + `uv pip install` of wheels only: `venv-cons-N` / `venv-cons-N1` / `venv-cons-base` = `sandloom[redis,celery]` + `sandloom-billing` (no tenancy); `venv-prov-N` / `venv-prov-N1` = `sandloom` + `sandloom-tenancy`; `venv-prov-acme` = `sandloom` alone |
| RPC harness | [scripts/provider.py](scripts/provider.py): a DB-free class registered as `tenancy.membership.v1` on a bare `CoreFacade`, served by the kernel's own RPC routes over uvicorn (pattern of `tools/packaging/wheel_rpc_smoke.py`); pair-MAC keys, one service id on both sides. Variants `N` (tenancy N's six methods, the installed wheel's `MemberState`; fingerprint `fbf42e76…`, equal to the real `MembershipReadContract`), `N1`, `missing` (no `get_role`), `acme` / `acme-missing` (replacement `acme_orgs` with its own `MemberState`). [scripts/consumer.py](scripts/consumer.py) composes the real app (`import sandloom.main`, `SANDLOOM_MODULES=["sandloom_billing"]`) and runs the two halves of the guard the app runs: the startup task body `check_remote_contract_compat` (`app_factory/lifecycle.py:182`) and the `/ready` services check `check_remote_service_bindings` (`app_factory/ops.py:148`). [scripts/run_pair.sh](scripts/run_pair.sh) runs one pair. |

Transcripts are in [out/](out/) (scratch paths shortened to `$PC`, `$S`).

### 1.1 What the prototype changes

| change | file(s) | B1 item |
|---|---|---|
| consumer copies of a module distribution in `<module>/_generated/contracts/`: stub, `<provider>_dto.py`, `<stem>.describe.json`; the set read by AST from the module's own imports of `<module>._generated.contracts.<stem>` (a `<provider>_dto` import pulls the contracts of that provider the module declares in `consumes`); **planned before composition**; provider found among importable packages by importing their `contracts` submodule (never composing them) | new `core/cli/consumer_copies.py` (575 lines); `core/cli/generated.py` (`plan_consumer_copies`, `main`) | B1-M1, step 5 |
| expectation file beside each stub, in a B2-extendable shape (`describe` key, plus `contract`, `fingerprint`, `provider`, `generator_format`) | `consumer_copies.expectation_document` | B1-M2 |
| `expected_contracts` looks first beside the loaded consumers' stubs (`<module>._generated.contracts`, `<top>.shared.contracts`, `CONTRACT_DTO_PACKAGES`), located without import | `core/contract_compat.py` (`consumer_expectations`) | B1-M2 |
| no expectation for a remote contract: `strict` refuses startup naming it; `ready` fails `/ready` with `contract_unverified`; `<m>.claims.v1` / `<m>.facts.v1` exempt (kernel-shaped) | `core/rpc/readiness.py` | B1-M3 |
| provider absent at generate: committed copy kept when its digest verifies; edited → refused; none → refused naming module, contract and provider | `consumer_copies._keep_committed` | B1-M3, step 5 |
| provenance header `contract`, `fingerprint`, `provider` (distribution and class), `generator: format N  digest`; `--check` explains a diff as generator change or contract change | `consumer_copies.with_header`, `explain_diff`; `generated.apply` | step 6 |
| stem collision refused in both paths (module-local and app shared package) | `consumer_copies.module_files`, `generated.plan` | step 7 |
| kernel-home branch removed: every generated file imports `sandloom.sdk.contracts` | `core/cli/dto_copies.py` (`contract_refs_home`, `KERNEL_SHARED` gone) | step 2 |
| `architecture_rules.yml` drops `shared_package: sandloom.shared`; no consumer copy or event schema is generated into a kernel package | `architecture_rules.yml`, `generated.plan` | step 2 |
| deleted: `sandloom/shared/contracts/` (5 stubs, 2 DTO modules, `__init__`), `sandloom/shared/events/*.json` (34), `sandloom/shared/membership.py` (C5, option (a): inlined in its only importer, the test) | | steps 2, 3, 8 |
| billing imports `sandloom_billing._generated.contracts.tenancy_dto`; admin imports `sandloom_admin._generated.contracts.identity_security_v1` | 3 files | step 1 |

Not changed by the prototype (found to need changing, §4): `sandloom add contract`/`add module`
(`core/cli/add.py:708-730`, `:850`), `scaffold_deploy.py:182`, archcheck C012 and C025,
`sdk/_prim/docs_wire.py`, `.importlinter`, `tools/packaging/public-source.json`, recipes and docs
texts, the ledger's C-GEN-03.

## 2. Verdicts

| # | proof (§11) | scenario / invariant | verdict |
|---|---|---|---|
| 1 | consumer-only install from wheels | S5, I-05 | **pass** |
| 2 | kernel wheel lists no module file; unchanged by an additive module change | S1, S13, I-11 | **pass** |
| 3 | D4 closed under `strict`, incl. replacement `acme_orgs` | S11, S6a, I-12, I-20 | **pass** |
| 4 | mixed versions N / N+1, both orders | S2, S8, S9, I-12 | **pass with a defect**: consumer-first refuses although the consumer never calls the new method (§3.4) |
| 5 | byte-identical generate; generator-format bump reported by file | S7, I-15 | **pass** |
| 6 | provider absent at generate | S10, B-B6 | **pass** |
| 7 | generate without composing the provider | baseline §13.2 | **pass**, two side effects recorded (§3.7) |
| 8 | two DTO copies and v1+v2 stubs in one process | B-B2, B-B5 | **pass**; hand-written `ContractRef` takes the first loaded copy |
| 9 | C5 moved, rolling-upgrade tests unchanged | S14, B-B7 | **pass** |
| 10 | stub-name collision refused | §1.6 | **pass** |
| 11 | rendering under two pydantic minors (B4 trigger b) | I-15 | **pass for pydantic; trigger shown for the formatter** (§3.11) |
| 12 | kernel tests with no module installed | answer #2 | **pass for B1's part**: only the listed migrations fail anew; the kernel suite already fails without batteries at base (195 ids) |
| 13 | DX, contract part, base vs candidate | §4.2 | counted (§3.13) |

## 3. Proofs

### 3.1 Consumer-only install from wheels (I-05, S5)

`run_pair.sh venv-prov-N N venv-cons-N strict` ([out/p1-p3-candidate.txt](out/p1-p3-candidate.txt)):

```
installed: ['sandloom-billing==0.7.0a11', 'sandloom==0.7.0a11']
find_spec('sandloom_tenancy'): None
find_spec('sandloom.shared.contracts'): None
loaded_modules: ['sandloom_billing']
client: _TypedClient
get_member_state -> sandloom_billing._generated.contracts.tenancy_dto.MemberState: MemberState(organization_id=UUID('a6c0…'), user_id=UUID('1a86…'), role='admin', version=7)
is billing's own copy: True
core.get_service(name).get_member_state -> sandloom_billing._generated.contracts.tenancy_dto.MemberState
billing tenant_resolver._member_state -> sandloom_billing._generated.contracts.tenancy_dto.MemberState version=7
expectation of tenancy.membership.v1: sandloom_billing/_generated/contracts/tenancy_membership_v1.describe.json
startup compat (strict): ok {"probed": ["tenancy.membership.v1"]}
/ready services: ok {"probed": ["tenancy.membership.v1"]}
```

The billing wheel carries `sandloom_billing/_generated/{__init__,contracts/__init__,contracts/tenancy_dto,contracts/tenancy_membership_v1}.py` and the `.describe.json`. Still needs `sandloom[celery]` to boot billing (problem #9, separate; baseline §13.2). **Pass.**

### 3.2 Kernel wheel (I-11, S1, S13)

The N+1 change: tenancy `MemberState.display_name: str | None = None` and a method `count_members`; billing `CreditsAdded.note: str | None = None` (an optional field on `billing.credits.added.v1`; billing's contracts have no DTO, so the DTO case is tenancy's). `sandloom generate` wrote only `sandloom_billing/_generated/contracts/*`, `sandloom_billing/module.snapshot.json`, `sandloom_tenancy/{module,contracts}.snapshot.json`. [out/p2-kernel-wheel.txt](out/p2-kernel-wheel.txt):

```
sandloom wheel N  : 526 files; sha256 7d7922e781850821
sandloom wheel N+1: 526 files; sha256 7d7922e781850821
file list equal: True
per-file hashes equal: True
module-named files in the sandloom wheel: []
billing wheel N vs N+1 differing: ['METADATA', 'RECORD', 'sandloom_billing/_generated/contracts/tenancy_dto.py', '…/tenancy_membership_v1.describe.json', '…/tenancy_membership_v1.py', 'sandloom_billing/events.py', 'sandloom_billing/module.snapshot.json']
```

Base contrast, same edit at `ceb3d66` ([out/p2-base-contrast.txt](out/p2-base-contrast.txt)): `generate` wrote `src/sandloom/shared/contracts/tenancy_dto.py` and `src/sandloom/shared/events/billing.credits.added.v1.json`, i.e. a new `sandloom` release. **Pass.**

### 3.3 D4 closed (I-12, I-20, S11, S6a)

Same transcript, provider variants:

| provider | consumer | startup (`strict`) | `/ready` | call |
|---|---|---|---|---|
| tenancy N, `get_role` missing | billing N, consumer-only | `REFUSED ContractIncompatibleError: … 'tenancy.membership.v1.get_role: method removed by the provider', 'expected_from': '…/sandloom_billing/_generated/contracts/tenancy_membership_v1.describe.json'` | `fail` `contract_incompatible` | `get_role` → `501 ServiceUnavailableInDeployment` |
| same, `CONTRACT_COMPAT_CHECK=ready` | billing N | logs ERROR, starts | `fail` `contract_incompatible` | — |
| `acme_orgs` (own `MemberState`, same fields; no sandloom-tenancy anywhere) | billing N | `ok`, compared (`probed`) | `ok` | typed → billing's copy; `core.get_service(name)` → **`builtins.dict`**; billing's `_member_state` → billing's copy |
| `acme_orgs`, `get_role` missing | billing N | `REFUSED` (same error, expectation beside billing's stub) | `fail` `contract_incompatible` | — |

No "No contract snapshot" warning in any candidate run. Base contrast ([out/p3-base.txt](out/p3-base.txt), base wheels, provider `missing`): `WARNING … No contract snapshot for remote contract tenancy.membership.v1: its compatibility is not checked`, `startup compat (strict): ok {"probed": []}`, `/ready services: ok` — D4 reproduced.

No expectation at all (the `.describe.json` removed from the installed billing wheel; [out/p3-no-expectation.txt](out/p3-no-expectation.txt)): `strict` → `REFUSED ContractIncompatibleError: cannot check remote contract(s) with no expectation: tenancy.membership.v1`; `ready` → `/ready` `fail` `contract_unverified`. Never a WARNING alone. **Pass.**

### 3.4 Mixed versions (I-12, S2, S8, S9)

[out/p4-mixed.txt](out/p4-mixed.txt). Billing a12 holds a copy regenerated against tenancy a12.

| order | call | startup `strict` | `/ready` |
|---|---|---|---|
| a) consumer N → provider N+1 (provider first) | `get_member_state` 200, billing N's copy (extra `display_name` ignored) | ok | ok |
| b) consumer N+1 → provider N (consumer first) | `get_member_state` 200 (`display_name=None`); `count_members` → `501 … Method 'count_members' is not exposed` | **REFUSED** `count_members: method removed by the provider` | **fail** `contract_incompatible` (also under `ready`) |
| c) N+1 → N+1 | `count_members` → 3 | ok | ok |

B-B4 (new consumer calls a method the old provider lacks: 501 plus `contract_incompatible`) passes. **But B-B1's "in both orders `/ready` 200" fails in order b**: billing N+1 never calls `count_members`; it refuses only because it regenerated against tenancy N+1 and its `consumes` entry has `methods: []` (= the whole contract), so the expectation is the provider's full describe. The base has the same rule when the provider's package travels in the consumer image; B1 now applies it to every consumer-only install. Not run: `tests/split_conformance` (15 deployments of the real modules on Postgres); the pair harness replaces it for the contract surface only. **Pass with a defect** for the ADR (§5, item 4).

### 3.5 Byte-identical generation (I-15, S7)

[scripts/p5_byte_identical.sh](scripts/p5_byte_identical.sh) deletes every `_generated/` and regenerates; digest over all consumer copies and module snapshots ([out/p5-byte-identical.txt](out/p5-byte-identical.txt)):

```
PYTHONHASHSEED=0      default order: exit 0; modules [sandloom_billing,sandloom_identity,sandloom_tenancy]; digest b2f4f35ae641bd88 over 21 files
PYTHONHASHSEED=12345  default order: … digest b2f4f35ae641bd88
PYTHONHASHSEED=random default order: … digest b2f4f35ae641bd88
PYTHONHASHSEED=0      reversed order: exit 0; modules [sandloom_tenancy,sandloom_identity,sandloom_billing]; digest b2f4f35ae641bd88
PYTHONHASHSEED=999    reversed order: … digest b2f4f35ae641bd88

$ sandloom generate --check   (GENERATOR_FORMAT patched 2 -> 3, declarations unchanged)
generated files are stale: run `sandloom generate` (6 file(s)):
  src/sandloom_admin/_generated/contracts/identity_security_v1.py [SANDLOOM-T007] (differs): generator change (format 2 -> 3), contract fingerprint unchanged (ed0385dfb659939804f7a8cfba941399)
  src/sandloom_admin/_generated/contracts/identity_security_v1.describe.json [SANDLOOM-T004] (differs): generator change (format 2 -> 3), fingerprint ed0385df… unchanged
  src/sandloom_admin/_generated/contracts/identity_dto.py [SANDLOOM-C012] (differs): generator change (format 2 -> 3), contract fingerprint unchanged (identity.security.v1 ed0385df…)
  src/sandloom_billing/_generated/contracts/tenancy_membership_v1.py [SANDLOOM-T007] (differs): generator change (format 2 -> 3), contract fingerprint unchanged (fbf42e76…)
  … (6 files) [exit 1]
```

The consumer copies do not depend on composition order by construction (planned before
composition). **Pass.** The formatter is an input, §3.11.

### 3.6 Provider absent at generate (B-B6, S10)

[scripts/p6_gen_without_provider.py](scripts/p6_gen_without_provider.py) makes `sandloom_tenancy` unimportable (meta-path finder, entry point hidden, not composed) and runs the real `generate` entry ([out/p6-provider-absent.txt](out/p6-provider-absent.txt)):

```
## a) committed copy present and unedited
$ sandloom generate / --check (tenancy absent)   -> exit 0; billing's _generated unchanged
## c) committed copy edited by hand (a field default changed)
src/sandloom_billing/_generated/contracts/tenancy_dto.py: error[SANDLOOM-T007]: incompatible change: committed copy of tenancy.membership.v1: hand-edited: digest 179ba8714ef94dd5 in the header, 5f320947b504774e of the body
sandloom generate: nothing was written   [exit 1]
## b) no committed copy
…/tenancy_membership_v1.py: error[SANDLOOM-T007]: incompatible change: refused: sandloom_billing consumes tenancy.membership.v1; its provider (module 'tenancy') is not importable here and no copy is committed. …
sandloom generate: nothing was written   [exit 1]
```

**Pass.** The refusal reuses T007 and prints T007's fix ("a breaking contract change is a new version"), which is wrong for it: it needs its own code.

### 3.7 No composition needed (baseline §13.2)

[scripts/p7_uncomposed.py](scripts/p7_uncomposed.py) in a venv with the candidate `sandloom` and `sandloom-tenancy` wheels, `env -i`, no app ([out/p7-uncomposed.txt](out/p7-uncomposed.txt)):

```
composed app exists: False
provider found: sandloom-tenancy sandloom_tenancy.contracts.MembershipReadContract
env reads during the provider import: ['MSGPACK_PUREPYTHON', 'PYDANTIC_DISABLE_PLUGINS']
files opened outside the interpreter/venv: none
network connects: none
  ORM tables (Base.metadata): 8 -> 12
  dto copies registered: 0 -> 0
  sandloom_tenancy modules imported: 0 -> 18
  settings loaded: 0 -> 0
expectation fingerprint: fbf42e7650aa13c0878c442f6080d419
equal to the committed stub body: True
```

**Pass**, with two recorded effects: (1) importing a provider's `contracts` module maps its ORM
models into the shared `Base.metadata` (4 tables): a process-global registration, harmless in
`generate` (the composed app imports them anyway) but "no registration" is not literally true;
(2) the first run failed with `Invalid configuration: DATABASE_URL … not set`: the namespace scan
`isinstance(value, type)` touched the lazy `settings` proxy that `sandloom_tenancy.contracts`
imports, which loads settings on any attribute access. Fixed in the prototype with
`issubclass(type(value), type)`; the env reads above are two libraries' own flags. Also the
generator's own phase now runs before composition, so `generate` composes the consumer only after
its copies exist (§4, surprise 1).

### 3.8 Two copies, two versions (B-B2, B-B5)

Fixture [fixtures/p8](fixtures/p8): provider `acme` (`acme.quota.v1` → `Quota`, `.v2` → `QuotaV2`, both nest `Limit`), module distribution `shopmod` importing both stubs, application copy `app.shared.contracts` of v1. Provider and consumer in two processes over RPC; the consumer cannot import `acme` ([out/p8-two-copies.txt](out/p8-two-copies.txt)):

```
find_spec('acme'): None
copies of acme.schemas:Quota loaded: ['shopmod._generated.contracts.acme_dto.Quota', 'app.shared.contracts.acme_dto.Quota']
shopmod v1 typed -> shopmod._generated.contracts.acme_dto.Quota | limit: shopmod._generated.contracts.acme_dto.Limit
shopmod v2 typed -> shopmod._generated.contracts.acme_dto.QuotaV2 | limits[0]: shopmod._generated.contracts.acme_dto.Limit
app v1 typed     -> app.shared.contracts.acme_dto.Quota | limit: app.shared.contracts.acme_dto.Limit
v1 Limit and v2 Limit are one class: True
hand-written ContractRef('acme.quota.v1') -> shopmod._generated.contracts.acme_dto.Quota
```

**Pass.** A hand-written `ContractRef(name)` decodes into the first loaded copy (here the module
distribution's, by import order), as today (`contract_wire.py:765-767`).

### 3.9 C5 moved (B-B7)

`sandloom/shared/membership.py` deleted; its three functions inlined verbatim at the top of
`tests/integration/test_revocation_pipeline.py`; `MemberState` imported from
`sandloom_tenancy.schemas.membership` (the test already imports tenancy). No assertion line
changed. `pytest tests/integration/test_revocation_pipeline.py -p no:randomly`: base `21 passed`,
candidate `21 passed`. Module suites (tenancy member events and registration surface, billing,
admin): base `347 passed`, candidate `347 passed` ([out/kernel-suite-repo-env.txt](out/kernel-suite-repo-env.txt)). **Pass.**

### 3.10 Stub-name collision (§1.6)

Fixture [fixtures/p10](fixtures/p10): `a.b_c.v1` and `a_b.c.v1` ([out/p10-stem-collision.txt](out/p10-stem-collision.txt)):

```
stub_names('a.b_c.v1'): ('a_b_c_v1', 'ABCV1', 'A_B_C_V1')
stub_names('a_b.c.v1'): ('a_b_c_v1', 'ABCV1', 'A_B_C_V1')
consumer/_generated/contracts/a_b_c_v1.py: error[SANDLOOM-T007]: stub name collision: contracts a.b_c.v1 and a_b.c.v1 both map to a_b_c_v1.py; rename one contract
application shared package (plan() check): {'a_b_c_v1': ['a.b_c.v1', 'a_b.c.v1']}
written: nothing
```

**Pass.** The Protocol name and the constant collide too (`ABCV1`, `A_B_C_V1`); renaming the file
alone would not be enough.

### 3.11 Rendering under two pydantic minors (B4 trigger b)

[scripts/p11_render.py](scripts/p11_render.py) renders every bundled provider's stubs and DTO
modules with one generator format ([out/p11-pydantic.txt](out/p11-pydantic.txt)). The kernel
declares `pydantic>=2.14.1,<2.15`; 2.13.5 was forced with `--override` (outside the declared
range; 2.15 does not exist yet).

| environment | all 7 files |
|---|---|
| pydantic 2.14.1 / core 2.50.1, ruff 0.17.1 | `a3ab116700ac9823` |
| pydantic 2.14.3 / core 2.50.4, ruff 0.17.1 | `a3ab116700ac9823` |
| pydantic 2.13.5 / core 2.46.5, ruff 0.17.1 | `a3ab116700ac9823` |
| pydantic 2.14.3, ruff 0.12.0 / 0.6.0 | `a3ab116700ac9823` |
| pydantic 2.14.3, **ruff absent** | `f764072885207f81` (`identity_dto.py`, `tenancy_dto.py` differ by 1 byte) |
| pydantic 2.14.1, ruff 0.17.1, consumer project `line-length = 100` | `a3ab116700ac9823` |
| pydantic 2.14.1, ruff 0.17.1, consumer project **`line-length = 60`** | `3af1075f56ecda95` (both DTO modules differ) |

Pydantic minor: no byte difference. **The build environment is an input through the formatter**:
the DTO modules are piped through the consumer project's ruff with the consumer project's
configuration (`dto_copies.ruff_formatted`, `generated.py` `format_dto`), and silently left
unformatted without ruff. Stubs are rendered in the final layout by the generator itself and did
not change. B4's trigger (b) is met, but by the formatter, which B1 can remove without B2 (§5,
item 7).

### 3.12 Kernel tests with no module installed (answer #2)

Copies of both trees without `src/sandloom_*` and `packages/editable/sandloom-*`, a venv with the
tree's own `sandloom` wheel and the dev requirements (no module wheel), `pytest
src/sandloom/core/tests tests/sdk tests/architecture -n 6 -o pythonpath=`
([scripts/k-run.sh](scripts/k-run.sh); [out/p12-kernel-base.txt](out/p12-kernel-base.txt),
[out/p12-kernel-candidate.txt](out/p12-kernel-candidate.txt)):

| tree | batteries importable | `sandloom.shared.contracts` | result |
|---|---|---|---|
| base `ceb3d66` | none | importable | `155 failed, 5932 passed, 16 skipped, 40 errors` |
| candidate | none | absent | `163 failed, 5904 passed, 16 skipped, 42 errors` |

The kernel suite already fails without batteries at base (195 failing ids: scaffold-deploy, admin
operations, claims key ownership, deploy profiles, module extraction, …; answer #2's open work,
not decision B's). Failing ids only in the candidate (ids compared after removing the
parametrize suffix and a truncation artefact):

| ids | cause |
|---|---|
| `test_split_readiness.py` × 3 (`test_down_peer_does_not_fail_health_or_ready_by_default`, `test_required_peer_down_fails_ready_but_never_health`, `test_required_probes_run_concurrently_capped_and_authenticated`) | B1-M3: their bindings have no expectation (with batteries installed only one fails, the others find the provider package's snapshot) |
| `test_contract_dto_locality.py`, `tests/sdk/test_sdk_docs_wire.py` (collection) | import `sandloom.shared.contracts` |
| `test_sdk_generated.py` × 2 | kernel-home branch removed |
| `test_kernel_import_contracts.py`, `test_lint_suppression_ratchet.py::…function_local_imports…`, `test_public_api_sdk.py` | `.importlinter` names `sandloom.shared.membership`; prototype code |

No candidate-only failure comes from a kernel test that needs a battery's contract artifact: with
`sandloom/shared/contracts` and `shared/events` gone, nothing in `src/sandloom/core/tests` failed
for want of them except the two files that import them by name. **Pass for B1's part**; the 195
pre-existing failures are the separate work of answer #2.

In the repository environment (every module installed), the same suites plus `tests/laws`: base
`1 failed, 6554 passed` (`test_sqlite_nested_units.py::test_saga_steps_run_on_the_sqlite_test_harness`,
fails at base too); candidate `11 failed, 6524 passed, 2 errors`, each attributed:

| failure | cause | kind |
|---|---|---|
| `test_sdk_generated.py::test_the_framework_tree_stubs_keep_the_kernel_home`, `::test_an_application_stub_imports_the_public_home` | the kernel-home branch is gone (C-GEN-03) | intended; ledger text change |
| `test_split_readiness.py::test_required_probes_run_concurrently_capped_and_authenticated` | a binding without expectation now fails `/ready` (`contract_unverified`) | intended (B1-M3); the fixture needs an expectation |
| `test_archcheck_library.py::test_framework_manifest_passes_every_rule`, `test_static_cli_no_env.py::test_sandloom_check_runs_against_a_manifest_with_empty_environment` | archcheck C012 requires a copy in `<shared>/contracts/<m>_dto.py`; C025 reads `<shared>/events` (15 warnings) | rule migration (§4) |
| `test_contract_dto_locality.py`, `test_sdk_docs_wire.py` (collection errors) | import `sandloom.shared.contracts`; `sandloom docs <contract>` reads it (`docs_wire.py:9-11`) | migration; `sandloom docs` for bundled contracts must read producers' snapshots / consumer copies |
| `test_kernel_import_contracts.py` | `.importlinter` names `sandloom.shared.membership` | mechanical |
| `test_lint_suppression_ratchet.py` (2), `test_public_api_sdk.py`, `test_source_export.py` | prototype code (C901 markers, a function-local import), new paths not classified in `public-source.json` | prototype quality, mechanical |

### 3.13 DX, contract part (§4.2)

Flow "add a contract in module A, consume it in module B, split them" in a tree of top-level
modules (the framework tree), run in both worktrees ([out/p13-dx-base.txt](out/p13-dx-base.txt),
[out/p13-dx-candidate.txt](out/p13-dx-candidate.txt)):

| count | base | candidate |
|---|---|---|
| commands | 4 (`add module alpha`, `add module beta`, `add contract alpha quota`, `generate`) | 4 (same) |
| files edited by hand | 1 (`beta/services.py`: import + `Contract(REF)`) | 1 (same; the import names a file that does not exist yet) |
| files generated into the kernel package | 4 (`sandloom/shared/contracts/alpha_quota_v1.py`, `alpha_dto.py`, `shared/events/alpha.item.created.v1.json`, `beta.item.created.v1.json`) | 0 by `generate`; 3 by `add contract` (not migrated in the prototype, removed by hand) |
| consumer files generated | 0 in beta (the stub lives in the shared package, generated for every provider, consumed or not) | 3 in `beta/_generated/contracts/` (stub, `alpha_dto.py`, `.describe.json`), only because beta imports them |
| import path the author must know | `sandloom.shared.contracts.<stem>` (framework) or `<shared_package>.contracts.<stem>` after setting `shared_package` (ecosystem repository: `ManifestError` otherwise, `manifest.py:119-123`) | `<module>._generated.contracts.<stem>` (stem = contract name, dots → `_`) |
| errors met | 0 in mono | 0 from the flow; 1 C012 from the unmigrated archcheck rule |
| split: extra steps | beta's wheel carries no stub (it is in the kernel or the shared package); a consumer-only beta gets the D4 WARNING unless every deployment sets `CONTRACT_SNAPSHOT_PATHS` and ships alpha's snapshot: 1 operator step per deployment | 0: stub and expectation are in beta's wheel |
| documentation lookups | 2 (where the stub lands and its import path; `CONTRACT_SNAPSHOT_PATHS` for the split) | 1 (the `_generated.contracts.<stem>` rule) |

## 4. Surprises

1. **Import-then-generate needs generation before composition.** `sandloom generate` composes the
   app first (`load_composition_app`); a module that imports a stub it has not generated yet
   fails to load (`ModuleLoadError: … No module named 'sandloom_billing._generated'`). The
   prototype plans the module-local copies before and without composition (`plan_consumer_copies`)
   and writes them first; this is what makes B1-M1's flow work, and it also makes the copies
   independent of composition order.
2. **The expectation is the provider's whole describe** (§3.4): a consumer regenerated against
   N+1 refuses N for a method it never calls, because `consumes.methods` is empty.
3. **The formatter is a build-environment input** (§3.11): ruff's presence and the consumer
   project's `line-length` change DTO copy bytes; the header digest then also reads a project
   reformat as "hand-edited".
4. **A replacement provider's DTOs decode to `dict` on the untyped path** (§3.3): the copy
   registry is keyed by the provider's origin (`sandloom_tenancy.schemas.membership:MemberState`);
   `acme_orgs.schemas:MemberState` matches no copy, so `core.get_service(name)` returns a plain
   dict. The typed path and billing's own re-validation into its copy work.
5. **A second generator writes into the kernel tree**: `sandloom add contract` / `add module`
   (`core/cli/add.py:708-730`, `:850`) render stubs and event schemas into `rules.shared_dir`,
   which still falls back to `sandloom/shared` for the framework tree (`manifest.py:113-118`);
   `scaffold_deploy.py:182` writes `CONTRACT_DTO_PACKAGES` from it. Composing `sandloom_admin`
   still writes `sandloom/shared/admin-manifest.json` (decision A, B-N1/B-N2).
6. **Archcheck rules assume the shared package**: C012 requires a copy of every provider DTO in
   `<shared>/contracts/<m>_dto.py` (with consumed-only generation, an unconsumed provider has no
   copy anywhere); C025 reads `<shared>/events/*.json`. Both must read other sources.
7. **Kernel-shaped auto contracts** (`<m>.claims.v1`, `<m>.facts.v1`) have no consumer stub, so
   B1-M3 would fail every split binding of them; the prototype exempts them.
8. **Lazy settings on namespace scans** (§3.7): any `isinstance` on a provider module's globals
   loads settings.
9. **Provider distribution in provenance** comes from the `sandloom.modules` entry points; it is
   the same in the editable dev install and the wheel (`sandloom-tenancy`), but `?` for a
   source-tree or app-local provider (`acme`, `alpha`).
10. **Uncomposed provider import maps ORM tables** into the shared metadata (§3.7).

## 5. What the ADR for decision B must state

1. **Home rule.** Generated consumer artifacts live in the distribution that imports them:
   `<module>/_generated/contracts/` for a module distribution, `<app>.shared` for an
   application. The kernel ships none; `sandloom.shared` keeps platform types only and is never a
   generation target; `architecture_rules.yml` of the framework tree has no `shared_package`. The
   kernel-home branch and C-GEN-03's clause go (ledger change, owner approval, I-16).
2. **The consumed set** of a module distribution is read statically from its own imports of
   `<module>._generated.contracts.<stem>` (`<provider>_dto` pulls the contracts of that provider
   declared in `consumes`) and **planned before composition**. Every writer of generated files
   follows it: `generate`, `add contract`, `add module`, `scaffold_deploy`.
3. **The expectation file** beside each stub (`describe` + `contract`, `fingerprint`, `provider`,
   `generator_format`; extendable by B2), and the lookup order: beside the consumer's stub, then
   `CONTRACT_SNAPSHOT_PATHS`, then the provider package.
4. **Narrowing.** Which methods an expectation holds the provider to. With the whole describe,
   B-B1's consumer-first order fails (§3.4). The ADR must choose: the generator writes the methods
   the consumer calls (static `called_methods`), or `consumes` must name methods, or compat treats
   a method absent from the provider as a failure only when the consumer declares it.
5. **No expectation** for a remote contract: `strict` refuses startup naming it; `ready` fails
   `/ready` with `contract_unverified`. Where the expectation of kernel-shaped auto contracts
   (`claims.v1`, `facts.v1`) comes from (kernel-defined describe), or that they are exempt; the
   outcome for a hand-written `ContractRef` without a stub (open point 1).
6. **Provider absent at generate**: keep a committed copy whose digest verifies, refuse an edited
   one, refuse a missing one naming module, contract and provider, with **its own error code**
   (not T007) and fix text.
7. **Determinism**: generated files are a function of (provider declarations, generator format)
   only. DTO modules are rendered in their final layout by the generator (as stubs already are),
   not by the consumer project's ruff; `_generated/` is excluded from project formatters, or the
   digest is computed over a normalised form. The pydantic minor is not an input (§3.11).
8. **Provenance header** fields, and that the generator format is bumped only when rendered
   output changes; `--check` reports each diff as a generator change or a contract change; the
   provider *version* is not recorded (uv.lock does).
9. **Collisions**: two contracts with one stem are refused in every path, naming both (the
   Protocol name and the constant collide too).
10. **Replacement providers**: whether the DTO copy registry stays keyed by the original
    provider's origin (then a replacement's results decode to plain data on the untyped path) or
    copies are registered per contract and shape; and that a hand-written `ContractRef(name)`
    decodes into the first loaded copy.
11. **C5** leaves the kernel with option (a); B-B7 holds (§3.9).
12. **Migration list**: `core/cli/add.py`, `scaffold_deploy.py`, archcheck C012 (a DTO must be
    copyable, not copied into a shared package) and C025 (the producer's `module.snapshot.json`),
    `sdk/_prim/docs_wire.py` (`sandloom docs <contract>`), `.importlinter`, `public-source.json`,
    the tests listed in §3.12, recipes, error-code texts, CLI templates and
    `docs/reference/testing.md` that name `sandloom.shared.contracts`.
13. **B4 triggers**: (a) no consumer that cannot install its provider was found; (b) shown only
    for the formatter, which item 7 removes inside B1. The evidence does not require B2.

## 6. Not run, and why

- `tests/split_conformance` (15 deployments of the real modules, Postgres): replaced by the pair
  harness, which covers the contract surface (describe, compat, call decoding) but no module's
  own behaviour over RPC.
- A real tenancy provider with its database over RPC: the stand-in has tenancy N's exact
  describe (fingerprint `fbf42e76…` equal to `MembershipReadContract`'s) and returns canned data.
- The startup refusal through a full uvicorn lifespan: the startup task body and the `/ready`
  check were called directly on the composed core (`check_remote_contract_compat`,
  `check_remote_service_bindings`), which are what the lifespan and `/ready` run.
- Billing without celery (problem #9) and the other `standard` pieces: out of scope; the consumer
  venvs install `sandloom[redis,celery]`.
- B2 (declaration format 2) and B3: not prototyped (the task is B1).
- The collision check in the base (silent overwrite) was read from code (`generated.py:98-103`,
  `_contract_files`), not run.
