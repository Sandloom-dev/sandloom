# Alternatives A: what dependency of the kernel on modules is legitimate

Task: tm `kb-alt-boundary` (parent `kb-assess`), decision A of [requirements.md](../requirements.md) §2.
This file proposes and compares; it decides nothing. ADR-A decides.

**Blind.** Written without reading `target-architecture.md`, `codex-review.md`, `reconciled.md`,
`roadmap-review.md`, the ChatGPT roadmap inputs, or the other decisions' alternatives files. Inputs:
[goals.md](../goals.md), [requirements.md](../requirements.md), [owner-answers.md](../owner-answers.md),
[problems.md](../problems.md), [baseline.md](../baseline.md), [guarantee-map.md](../guarantee-map.md),
[validation.md](../validation.md), [perf-dx-baseline.md](../perf-dx-baseline.md),
[research-trust.md](research-trust.md), [research-authz-portability.md](research-authz-portability.md),
[research-settings.md](research-settings.md), the code, and the owner decisions recorded in tm on
`kb-alt-boundary` (admin battery, 2026-11-08) and `kb-assess` (minimal sufficient alternative;
guarantee / provider / trust; capability rule; blinding).

**Commit.** Edge ids (A1, B-R1, C1, ...) are rows of baseline.md at `b8d8506`. The code was read at
`arch/kernel-boundary` HEAD `20a22b4`. Since `b8d8506`, `src/` changed only in
`core/facade/_claims.py` and its test (PR #70, at `44451e4`); `git diff --stat 44451e4 20a22b4 -- src/`
is empty, so baseline `file:line` hold except in `_claims.py`. One guard changed outside `src/` since the
baseline: `fd7b058` (tm `kb-guards-honest`) widened `tests/architecture/test_shared_purity.py`, added the
import-linter contract `kernel-imports-no-module` (`.importlinter:18-21`), made
`tools/packaging/distribution_imports.py` resolve first-party imports through base requirements only, and
added the shrink-only `tests/architecture/kernel_module_imports_allowlist.json` (one entry: B-I1). Paths
are under `src/sandloom/` unless they start with `sandloom_<m>/` (`src/`) or name a repository file.

Statuses: a claim with no code location, test or run is marked **Unverified**.

## 0. In plain words

Every alternative keeps the general security and isolation protocol in the kernel (I-21, H-14): the
subject, token minting and verification, revocation and blocking, the tenant and its isolation, the
platform admission, the authorization algebra. They differ in **how a module-owned thing is recognised
in kernel code** and in **what happens to the operator functions the kernel performs today through
identity** (admin bootstrap, incident revocation, the admin manifest).

| id | definition of the boundary, in one line |
|---|---|
| A0 (reference) | The kernel imports no business module. Nothing else is checked. |
| **A1 (minimal)** | Shipped kernel code and the kernel wheel name nothing a specific module owns, except rows of one edge register with a status; module functions hosted by the kernel's CLI and generator move to the module, declared on the existing module system. |
| A2 | The kernel depends only on capabilities it names and defines itself: where it performs a platform function with a module's data, it defines a port the module implements, and keeps the function. |
| A3 | A name belongs to whoever declares it: the kernel declares a protocol vocabulary, each module declares its vocabulary in its snapshot; kernel code may contain no module token, and no module may declare a kernel token. |
| A4 | The kernel is what its wheel ships and how it behaves alone: no module artifact in the wheel, no import outside its closure, its tests pass with no battery, and a conformance suite with every battery replaced passes. No lexical rule. |

The evidence favours **A1**, with A4's behavioural checks adopted as A1's tests rather than as the
definition (§8). Two points stay for a PoC: whether the kernel can own the durable revocation applier
itself (A1's M4), and whether an issuer-agnostic incident revocation is a platform guarantee (if yes,
A2's one port that passes H-18 joins A1).

## 1. What decision A settles, and what is fixed

**Decision A** (requirements §2): which kernel-to-module dependency is legitimate, and how the others are
kept out. Of the 46 judged baseline rows (40 class b, 5 class c, A19), decisions B, C and D own the
mechanisms for most groups: B the artifacts (C1-C5), C the authorization atoms and claim authority
(B-R1-B-R3, B-S1-B-S3 trust, B-N12-B-N18, A19), D the module settings (B-C1-B-C9). For those, A states
the status each row must reach and which decision carries it, and designs nothing. A designs what no
other decision owns: the classification rule, its enforcement, the admin battery split, the
operator CLI, and the kernel's revocation duties (scenario A-F5).

**Fixed for every alternative** (inputs, not choices):

- The rule is about source dependencies and shipped code; the protocol concepts stay (goals.md,
  "What the rule means"). A module's name in a template, recipe, doc or kernel test is classified
  apart and is not by itself a runtime edge; kernel tests run without batteries (answer #2).
- The admin battery (owner, tm reflection on `kb-alt-boundary`, 2026-11-08): `sandloom_admin` is an
  optional UI battery and a privileged API client with no special database access; platform
  authorization, audit, idempotency, tenant isolation, revocation and the safety rules of event and
  infrastructure operations stay in the kernel; removing it changes no kernel guarantee. Four gaps to
  close: the console requires the module named `identity` (`sandloom_admin/api.py:183`); the kernel
  gates manifest generation on the name `sandloom_admin` and holds the compiler
  (`core/cli/generated.py:547-561`, `core/admin/compiler.py`); identity's commands live in the kernel
  CLI; `ADMIN_CONSOLE_ENABLED` / `_DIST` are kernel settings (`core/config/modules.py:30`, `:38`). One
  console assembles sections from module resource declarations.
- No second driver system, no universal capability registry without a proven limit (H-17); a new port
  only when all six conditions hold (H-18); each new mechanism removes a confirmed violation or provides
  a required invariant at lower cost than an existing mechanism (H-12).
- Revocation duties (answer #16): the kernel guarantees access-token revocation and a durable applier;
  the issuer ends refresh sessions and the kernel verifies that obligation; a missing implementation is
  never hidden behind a warning.

## 2. The items to classify, where they are today

The last column applies goals.md "How to apply it" (protocol, module model, or neither) as read by
this file; the alternatives in §3 may classify differently, and §3.6 shows where.

| item | today (code) | goals.md reading |
|---|---|---|
| `core/auth`: tokens, bearer, verification, marks, revocation, blocks, recovery | `core/auth/*.py`; public in `sdk/security.py:17-37`. Names no module in code; docstrings mention identity (`core/auth/blocks.py:43`, `:89`, `:107`; `marks.py:159-160`) | protocol (I-21) |
| password hashing | `core/auth/hashing.py` (argon2id, bounded executor), `core/config/password_params.py` (`PASSWORD_HASH_*`), `sdk.security.password_hasher`, `run_password_hashing`, `rehash_password_if_needed` (`sdk/security.py:30-34`); consumers `sandloom_identity/services/auth_service.py`, `reauth.py` | neither: a generic security primitive with one consumer; names no module |
| `TenantScope`, tenant claim | `core/http/tenant_scope.py:28`, `:110-119`, `:159-173`; `shared/authz/_resolve.py:61-75`; `tenant_id` / `org_id` reserved (`e212893`); `tenant_ids` written by `sandloom_tenancy/claims.py:20-34` (B-S3); `TENANT_CLAIMS_MAX_ORGANIZATIONS` (B-C2) | protocol; the key `tenant_ids` is today tenancy's choice of name read by the kernel |
| `UserContext` | `shared/subject.py:60-77`: `roles`, `permissions` first-class, comment "From 'identity' module" (B-N16) | protocol (rights) |
| `MoneyLedger` | `core/sql_alchemy/money_ledger.py:1-17`, export `sdk/data.py:66`; docstring example `billing_credit_ledger`; one consumer billing (baseline §5.4) | neither: generic money-effect dedupe (ADR-0009 item 7) |
| `operation_intent`, `release_check`, `external_call`, `ResolvedAccount` | `core/unit_of_work/intent.py:1-40`; `ResolvedAccount.kind: "user" \| "org"`, "the billing account" (`:91-95`); an `org` release needs an authority re-read (`:350-354`) | neither: generic sequencing around an external call; `kind` borrows billing's account vocabulary |
| platform-admin gate | `require_platform_admin` (`shared/guards/platform.py:10-19`), `platform_operator` (`core/admin/api/platform.py:54-76`): trust `admin` when `"identity"` is in `claims_sources` (B-S1, B-S2) | protocol; the literal `"identity"` is identity's binding |
| platform operations API | one router per loaded module at `/api/v1/platform/<name>` (`core/admin/api/platform.py:262-280`, mounted `core/app_factory/__init__.py:251`; A8); settings view lists `billing_provider` (`:608`, B-C9) | protocol (A8); B-C9 module |
| admin console | `sandloom_admin/api.py`; `/admin/config.json` answers 503 unless a module named `identity` is in the manifest (`:181-183`) | admin battery |
| manifest compiler and generation | `core/admin/compiler.py` (529 lines; imports `<module>.admin` of each loaded module, `:462-470`), `core/admin/models.py` (resource schema), `core/cli/generated.py:547-561` (writes only when `"sandloom_admin"` is composed, B-N1; file name `:70`, B-N2), `sandloom admin export` (`core/cli/admin.py:525-531`); modules declare `RESOURCES` as plain dicts (`sandloom_billing/admin.py:5`) | admin tooling (owner decision) |
| `ADMIN_CONSOLE_ENABLED`, `ADMIN_CONSOLE_DIST` | `core/config/modules.py:30`, `:38`; read by `sandloom_admin/api.py` and `core/cli/admin.py:130-132` (B-C10) | admin |
| identity CLI commands | `core/cli/admin.py` (`create`, `invite`, `list` over `identity.security.v1`, `:61-62`); `core/cli/password_check.py:21`, `:40-44` (B-I1); `core/cli/security.py` (`revoke-sessions`, `force-password-reset`, `grant-admin` over `identity.security.v1`; `--tenant` over `tenancy.membership.v1`; `audit-export` kernel only); dispatch is a fixed table (`src/sandloom/__init__.py:225`, `:304-305`) | identity (accounts, passwords, refresh sessions); access-token revocation itself is kernel |
| durable revocation applier, refresh-session end on block | identity's consumers `sessions@tokens_revoked`, `sessions@user_blocked` (`sandloom_identity/__init__.py:63-84`); D1, T8 | applier: kernel duty (answer #16); refresh end: issuer's obligation |
| `sandloom.shared` | subject, authz engine and atoms, guards, contract stubs and DTO copies (C1, C2), 34 event schemas (C3), `membership.py` (C5); the generator's artifact home (C4) | mixed: platform primitives next to module artifacts |
| CLI, templates, recipes | B-N5-B-N11 (`core/cli/project.py`, `wizard.py`, `scaffold_deploy.py`, `templates/`, `sdk/_prim/recipes.py`) | composition text (goals.md, answer #2) |

## 3. The alternatives

### 3.1 A0 (reference): the import rule only

**Definition.** Shipped kernel code imports no business module, statically or dynamically. This is
G-KERNEL-10 as `fd7b058` widened it (`guarantees.yml:4051-4063`).

**Enforcement.** Exists: `test_shared_purity.py` (discovered `sandloom_*`, literal and module-level
constant dynamic imports), import-linter `kernel-imports-no-module`, `distribution_imports.py` (base
requirements only), the shrink-only allowlist. Missing for I-02: a computed import name that is not a
module-level constant is neither seen nor refused (`test_shared_purity.py:19-21`), while I-02 says a
check that cannot resolve an argument's origin refuses it.

**Result.** Catches B-I1, 1 of 46 judged rows. Contract names, claim sources, settings, artifacts and
CLI coupling (45 rows) stay invisible. Fails I-03, I-13, I-20, I-22, so H-1. Kept as the reference row
of the comparison and as a layer every other alternative keeps.

### 3.2 A1 (minimal sufficient): names rule with an edge register

**Definition.** Shipped kernel runtime code (`core`, `sdk`, `shared`, `main.py`, tests excluded) and the
built `sandloom` wheel contain no name a specific module owns: module or distribution name, contract or
event channel, fact name, setting, claim key that is not a kernel protocol key, CLI command, file name,
URL, role vocabulary, or generated artifact; and import nothing whose name is fixed in kernel source.
Concepts of the general protocol are named in the kernel's own vocabulary (`roles`, `permissions`,
`tenant_ids`, the reserved keys, the platform role `admin`, the families `<m>.claims.v1` and
`<m>.facts.v1`, the `core.*` events). Every remaining place is one row of the edge register, with a
status. A module function that the kernel's CLI or generator hosts today moves to that module and is
declared on the existing module system.

**What is legitimate.** Class (a) rows A1-A18 (descriptor-sourced, name no module), recorded in the
register as `retained` with reason "protocol"; the kernel protocol vocabulary once decision C assigns an
authority to each protected key; generic primitives with one consumer (password hashing,
`MoneyLedger`, `operation_intent`), which name no module.

**What moves, beyond B, C and D.**

- *Identity CLI.* `sandloom admin create | invite | list` and `sandloom security revoke-sessions |
  force-password-reset | grant-admin` become identity's contributed commands (M3), under the same
  names. B-I1 becomes identity importing its own policy. `--tenant` becomes identity consuming
  `tenancy.membership.v1` (optional), a module-to-module contract (H-3). The kernel keeps `sandloom
  security audit-export` and `sandloom backup`.
- *Admin tooling.* `core/admin/compiler.py` and `models.py` move to `sandloom_admin`; `sandloom
  generate` writes the manifest because admin declares it (M2); `sandloom admin export` becomes admin's
  contributed command (M3); `ADMIN_CONSOLE_*` become admin's settings (decision D). The console's
  `"identity" not in modules` test (`sandloom_admin/api.py:183`) becomes "a module of the composition
  declares `issues_tokens=True`" (`ModuleDescriptor.issues_tokens`, `core/descriptors/__init__.py:147`),
  plus an operator-login interface that admin documents and an issuer serves; with no issuer, admin
  refuses with a configuration error naming it. **Unverified**: that identity's login endpoints can be
  stated as such an interface without naming identity (PoC, scenario AD-3).
- *Set-password link* (B-C10, B-N3). It moves with `admin create` into identity. The link target
  becomes identity's own setting (decision D), written by `sandloom new` when admin is composed
  (application composition), so identity never reads admin's setting.
- *Start page* (B-N4). The `/admin` route probe stays (generic, `core/app_factory/pages.py:134-139`);
  the `sandloom admin create` hint becomes the list of commands the loaded modules contribute.
- *Revocation duties* (answer #16). The kernel registers the durable applier of
  `core.auth.tokens_revoked.v1` itself (M4; the function it runs, `apply_pending_revocation`, is already
  kernel, `sdk/security.py:21`); a module declaring `issues_tokens=True` that registers no consumer of
  `core.auth.user_blocked.v1` is refused at startup (M5).

**Enforcement.**

| layer | what | new or existing |
|---|---|---|
| static | the kernel-edge check (§4): AST over shipped kernel code; string literals (docstrings excluded) matched against module names discovered from `src/sandloom_*` and `sandloom.modules` entry points, `sandloom_\w+`, contract and event channels whose first segment is not `core`, and the module settings and claim keys that D and C declare; every finding must map to a register row | new (M1), extends the allowlist of `fd7b058` |
| static | origin rule for dynamic imports (I-02): allowed only at listed loader call sites with descriptor-sourced arguments; an unresolvable argument elsewhere is refused | extends `test_shared_purity.py` |
| static | import-linter `kernel-imports-no-module`, `test_shared_purity.py` | exist |
| wheel | `distribution_imports.py` (base requirements only); class (c) scan of the built kernel wheel: a generated file whose owner is not the kernel is a finding | first exists; second extends `check_package_distributions.py` |
| runtime, boot | lean boot (`check_package_distributions.py:399-413`, exists; leak list widened from `sandloom-identity` to every module wheel, `:324-328`); kernel unit and architecture tests in a venv with no module wheel (answer #2); the P2 matrix of 16 subsets (I-19); M5 at startup | extensions; M5 new |
| replaceability fixtures | the fixtures of `design/kernel-boundary/poc/replace` (`test_identity`, `grants`, `inventory`, `forged`) promoted to integration tests run without `sandloom_identity`, `_tenancy`, `_billing`, mono and split (A-H5, validation B6, C) | tests that I-13 and I-22 list as missing proof; no mechanism |

**New mechanisms** (§4.2 definition; H-12).

| # | mechanism | removes or replaces | serves | compared against |
|---|---|---|---|---|
| M1 | edge register with a status per baseline id, and the kernel-edge check (one new T code) | replaces the scope of `test_module_names.py:165` (exact literals in 4 directories) and generalises `kernel_module_imports_allowlist.json` from imports to every edge type | I-02, I-03 (missing proof), A-H4, A-F1 | widening `test_module_names.py` (no status, no reason, no ratchet on fixed rows); inline pragmas (no `fixed` status, scattered); import-linter (imports only) |
| M2 | generation extension: a module declares on its registration a file `sandloom generate` writes into the app's shared directory, rendered from the composed app; `generate --check` and `sandloom test` check it | B-N1 (`generated.py:551`), B-N2 (`:70`); lets `core/admin/compiler.py`, `models.py` and the A7 import leave the kernel | owner decision (2); I-13 (a third-party generated file); H-11 | admin's own command writing the file (outside `generate --check`: breaks H-11 and SANDLOOM-T016); building the manifest at console start (a split process does not compose every module, `generated.py:548-549`) |
| M3 | contributed CLI commands: a module declares commands (group and name) on its descriptor; `sandloom` reads the declarations from entry-point metadata and imports the module only when the command runs; a collision with a kernel command or another module's is refused with a code | B-I1, B-R4, B-C10, B-N3, B-R5 (session, password and grant commands); carries `admin export` | owner decision (3); I-02, I-13, I-20 (no silent fallback password policy) | a console script per module (`sandloom-identity ...`: also a new CLI command per module, and every documented command name changes); a kernel operator port (A2: fails H-18 for bootstrap, §3.3) |
| M4 | kernel-owned durable revocation applier: the kernel registers the consumer of `core.auth.tokens_revoked.v1` in every composition (a process-wide default) | D1; T8 first half; moves `sandloom_identity/__init__.py:73-84` | I-20, I-21, answer #16, A-F5 | the issuer registering it (D1 stays for tenancy alone); a startup refusal without identity (tenancy alone could not run) |
| M5 | issuer-obligation check at startup: `issues_tokens=True` requires a registered consumer of `core.auth.user_blocked.v1` | T8 second half: an unnamed duty becomes a refusal | I-21, I-22 (T8), answer #16 | a kernel port (A2 P1); documentation (fails I-20) |

M1-M3 are specific to A1; M4 and M5 answer answer #16 and appear in every alternative (A2 replaces M5
by a port). Not counted as new: the guard extensions in the enforcement table, and the removals that
need no mechanism (B-N15 constants with no importer, B-N19 ratchet entry, B-R6 billing special case).

**Costs and risks.**

- A lean install no longer lists `sandloom admin create` or `sandloom security revoke-sessions`; they
  arrive with identity. In a split deploy they run from an image where identity is installed (its own
  service), not from any service image as today (`core/cli/admin.py:29-31`). Runbooks change; the
  command names do not, so no alias (H-9). The ledger entries G-OPS-12 and C-OPS-08 keep their
  statements; their tests (`core/tests/test_security_cli.py`) move to identity's package (I-16).
- A replacement issuer ships its own bootstrap and incident commands. The kernel offers no
  issuer-agnostic way to end every session; access tokens can still be revoked per user through
  `revoke_user_tokens` (`sdk/security.py:32`). Whether more is a platform guarantee is open (§9.1).
- The register must be maintained; a `retained` row needs an ADR id, or it becomes an escape hatch.
- M3 must not import module code for `--help`, or one broken module breaks every `sandloom` command in
  P2 (A-B7).

### 3.3 A2: kernel ports for platform duties

**Definition.** The kernel may depend on a capability it names and defines, never on a module. Wherever
the kernel performs a platform function with a module's data or code today, it defines a port (a
contract with a kernel-owned name and a kernel-shipped stub), keeps the function, and checks at startup
that a required port has a provider or that its absence is defined. Every other edge is removed as in
A1.

**Candidate ports, against the six conditions of H-18.**

| port | serves | 1 platform guarantee | 2 no domain specifics | 3 testable | 4 replaceable | 5 defined absence | 6 local and RPC | verdict |
|---|---|---|---|---|---|---|---|---|
| P1 `platform.sessions.v1.end_sessions(user_ids)`: the issuer ends refresh sessions on block and revoke | G-BLOCK, G-REVOKE completeness | yes | yes | yes | yes | no issuer: no refresh sessions; issuer without P1: refuse | yes | **passes** |
| P2 `platform.subjects.v1.list(tenant=None)`: a subject directory for mass revocation | incident response | depends: a revocation by cut-off instant needs no list, and then the kernel does not lack the data | yes (tenant is a kernel concept) | yes | yes | yes | yes | **doubtful** |
| P3 `platform.operator.v1.create_admin(...)`: bootstrap of the first platform admin | none of the kernel's own: it checks admission, not accounts | no | no (email, password policy, set-password link) | — | — | — | — | **fails** → relocate as A1 |
| P4 console link target | DX | no | — | — | — | — | — | **fails** |
| P5 UI manifest | admin's product | — | — | — | — | — | — | **excluded** by the owner decision |

So A2 differs from A1 only on incident revocation and the refresh-session end: the kernel keeps
`sandloom security revoke-sessions [--all | --tenant | --user]`, calling P1 (and P2 if adopted);
`force-password-reset` and `grant-admin` go to identity; bootstrap, link and manifest as A1.

**Enforcement.** A1's static and wheel layers; the presence check of required ports at startup (a new
startup check family); a conformance kit for port implementers (new public testing names).

**New mechanisms.** M1, M2, M3, M4 as A1; P1 (a new contract kind: kernel-owned name and stub; replaces
M5 and the event-consumer duty); P2 (optional); the presence check. Six or seven.

**Costs and risks.**

- A port is versioned with the kernel: a P1 change needs dual versions across the kernel's and every
  issuer's version ranges.
- In a split deploy a block in tenancy's process reaches the issuer through a kernel consumer calling P1
  over RPC: a new call and a new `SERVICE_BINDINGS` / `RPC_ALLOWED_CALLS` entry where today identity
  consumes the event directly. Not the authorization path (I-14 unaffected), but an operator step.
- T9 becomes an assignment: who may serve `platform.sessions.v1`. That needs decision C's ownership
  check extended to a kernel-reserved `platform.*` family.
- A family of kernel ports grows toward the registry H-17 forbids; H-18 holding for one port of five is
  the evidence that the extra reach is small.

### 3.4 A3: declared vocabulary ownership

**Definition.** A name belongs to whoever declares it. The kernel declares its protocol vocabulary in one
kernel module: claim keys `roles`, `permissions`, `tenant_ids`, the reserved keys (today
`core/facade/_claims.py:62-87` at `44451e4`), the platform role `admin`, the reserved contract families,
the `core.*` events and the kernel settings. Each module's generated snapshot declares its vocabulary:
module and distribution name, contracts, events, facts, claim keys, settings, commands, generated files,
admin resources. The boundary: kernel code and the kernel wheel contain no token of any module's
vocabulary, and no module declares a kernel token as its own.

**What it settles mechanically.** The disputed rows B-S3 and B-N16 resolve by declaration: `tenant_ids`,
`roles` and `permissions` are kernel tokens; tenancy and identity write them only as the assigned
authority (decision C) and may not declare them. It also guards the other direction: a module
registering a contract, fact or claim key outside its declared vocabulary is refused at startup, which
covers T5-T7 and validation C4.

**Enforcement.** The kernel-edge check fed by a vocabulary index generated from every snapshot in the
repository (bundled modules, examples, fixtures) instead of patterns; a startup gate comparing
registrations with declarations; the wheel scan; the register for transitional rows.

**New mechanisms.** M1 (vocabulary-fed), the kernel protocol vocabulary module (part exists: the
reserved keys), vocabulary fields in module snapshots (new generated content; claim keys need an
explicit declaration, research-trust M6, which answer #15 defers to the PoC), the vocabulary index, the
startup declared-versus-registered gate; plus M2-M5. Seven or eight.

**Costs and risks.**

- The startup gate overlaps approved small fix #A, which checks `AccessFact.owner_module`, `owner=` and
  the families `<m>.claims.v1` / `<m>.facts.v1` against the registering module at lower cost; under H-12
  the gate is justified only for claim keys, and answer #15 says a claim-key manifest helps detect
  errors but does not replace the runtime ownership check.
- A module not in the repository contributes no vocabulary, so its name in kernel code is caught only if
  A1's generic patterns are kept as well.
- A universal index of declared names is close to the registry H-17 forbids, though it serves checks,
  not dispatch.
- Module authors declare claim keys; every vocabulary change is a snapshot diff (I-15 review).

### 3.5 A4: distribution and behaviour boundary

**Definition.** The kernel is what the `sandloom` wheel ships and how it behaves alone. A dependency is
legitimate unless it makes the kernel wheel ship a module-owned file, import outside its dependency
closure (extras excluded), fail its own tests with no battery installed, or fail a conformance suite in
which each standard battery is replaced by a fixture under another name. No lexical rule.

**Enforcement.** The wheel artifact scan; `distribution_imports.py` (exists); kernel tests in a lean venv;
the P2 matrix; the conformance suite (validation A, B, C as tests, plus AD-3 and AD-5 for admin).

**New mechanisms.** The conformance suite, optionally shipped as a kit for third-party batteries (new
public testing names), and the wheel artifact scan. One or two.

**Result.** A4 forces the rows that have a behavioural symptom: the live-adapter defaults (B-R1), the fact
owner map (B-R3), the platform gates (B-S1, B-S2), the artifacts (C1-C5), D1. It leaves the rows that have
none: unused constants `Permissions` / `Roles` (B-N15, no importer, baseline §5.4), error texts (B-N18),
the migration-lint entry (B-N19), the risk text citing billing (B-C5), inert tier rows (B-C6), backup
text (B-C8), the start-page hint (B-N4), `sandloom keys` text (B-N9). I-03 asks for a check over contract
names, claim keys, sources, settings and module names in kernel code with an approved list; A4 has none,
and A-F1 (a literal `"billing.entitlements.v1"` or `import_module(f"sandloom_{n}")`) passes it. So A4
alone fails I-02 and I-03 and H-1 disqualifies it. Its checks are the missing proof of I-13, I-19, I-20
and I-22 in every alternative.

### 3.6 Where each alternative puts each item

| item | A1 | A2 | A3 | A4 |
|---|---|---|---|---|
| `core/auth` | kernel protocol; identity mentions in docstrings rewritten in kernel terms (text) | same | same; kernel vocabulary | same; not checked lexically |
| password hashing | kernel, generic primitive; retained: it names no module, and moving it removes no edge while changing four advanced `sdk.security` names (I-17) | same | same; `PASSWORD_HASH_*` kernel vocabulary | same |
| `TenantScope`, tenant claim | kernel protocol; `tenant_ids` becomes a kernel protocol key with an assigned authority (C); B-S3 retained as protocol once C lands; `TENANT_CLAIMS_MAX_ORGANIZATIONS` to tenancy (D) | same | same, by declaration | same; fixture `grants` proves it (validation B) |
| `UserContext` | kernel; `roles`, `permissions` first-class as protocol rights (B-N16 retained); the trusted view is C's | same | same, by declaration | same |
| `MoneyLedger` | kernel, retained; the docstring example is text | same | same (docstrings outside the scan) | same |
| `operation_intent` and co. | kernel, retained; `ResolvedAccount.kind` follows C's decision on `Account` / `TenantKind` (B-N12, B-R2) | same | same | same |
| platform-admin gate | kernel; one platform admission shared by the two gates, trusting role `admin` from the assigned authority of `roles` (C); the literal `"identity"` goes | same | same; `admin` is a kernel token | same; fixture `test_identity` proves it |
| platform operations API | kernel (A8); protected by the kernel admission whether or not a console exists; B-C9 through D | same | same | same |
| admin console | `sandloom_admin`; issuer by declaration, admin-owned login interface | same | same, plus its vocabulary | same, if AD-3 is in the suite |
| manifest compiler and generation | `sandloom_admin` through M2; `admin export` through M3 | same (P5 excluded) | same | only if a fixture console fails without it |
| `ADMIN_CONSOLE_*` | admin's settings (D) | same | same | same |
| identity CLI | identity's contributed commands (M3) | bootstrap, password, grant to identity (M3); `revoke-sessions` stays kernel over P1 (P2) | as A1 | as A1 where a lean test fails, else unchanged |
| revocation applier, refresh end | kernel applier (M4), issuer obligation (M5) | kernel applier (M4), P1 | as A1 | detected by a fixture; mechanism borrowed |
| `sandloom.shared` | kernel package of platform primitives (subject, authz engine, guards); module artifacts leave (B), domain atoms leave (C); removing the package is not a goal | same | same | module artifacts forced out by the wheel scan |
| CLI, templates, recipes | composition text, scanned in its own scope, status `deferred` to the CLI phase; B-N8 generated from D's declarations | same | same | not judged |

### 3.7 The 46 judged baseline rows, by group

Target status in A1 and the decision that carries it; the other columns list only differences.
Statuses as in §4: `fixed`, `retained`, `deferred`.

| group | rows | A1 target | carried by | A2 | A3 | A4 |
|---|---|---|---|---|---|---|
| import | B-I1 | fixed | A (M3) | same | same | caught by the existing import guards |
| live adapters, fact owners | B-R1, B-R3, A19 | fixed | C | same | same | forced by fixtures |
| tenant kind | B-R2 | fixed, or retained as protocol, per C | C | same | same | same |
| operator CLI | B-R4 | fixed | A (M3) | same (P3 fails H-18) | same | stays unless a lean test fails |
| incident CLI | B-R5 | fixed: identity's commands, `--tenant` over tenancy's contract | A (M3) | kernel keeps `revoke-sessions` over P1 (P2) | same as A1 | — |
| test helper | B-R6 | fixed: `fake_access_fact` keeps generic fact-by-name stubbing; billing's case leaves | A | same | same | stays |
| platform gates | B-S1, B-S2 | fixed | C (authority) and A (one admission) | same | same | forced by fixture |
| tenant claim | B-S3 | retained (protocol key) | C | same | same, by declaration | same |
| module settings | B-C1-B-C9 | fixed (B-C7 shared fields, B-C8 backup notes, B-C9 console fields: research-settings §7.4) | D | same | same | B-C5, B-C6, B-C8 remnants stay |
| console link | B-C10, B-N3 | fixed | A (M3) and D (identity's link setting) | same (P4 fails) | same | stays |
| admin manifest | B-N1, B-N2 | fixed | A (M2) | same | same | B-N2 stays |
| start page | B-N4 | fixed: lists contributed commands | A (M3) | same | same | stays |
| starter, wizard, skill template, recipes | B-N5, B-N6, B-N7, B-N10, B-N11 | deferred (CLI and recipes phase) | later phase | same | same | not judged |
| deploy scaffold | B-N8 | fixed: env generated from declarations | D | same | same | stays |
| deploy templates, keys text | B-N9 | deferred | later phase | same | same | stays |
| domain atoms, beginner tier, check rules, error texts | B-N12, B-N13, B-N14, B-N17, B-N18 | fixed (B-N14 through an ADR-0010 amendment, answer #18; B-N17 through H-20 metadata) | C | same | same | B-N18 stays |
| unused constants | B-N15 | fixed: removed, no importer (baseline §5.4), under I-17 review | A | same | same | stays |
| rights in the subject | B-N16 | retained (protocol) | — | same | same, by declaration | same |
| migration lint | B-N19 | fixed: identity drops the index, or the ratchet entry moves to identity's tests | A | same | same | stays |
| docstring example | B-N20 | retained (allowed) | — | same | same | same |
| stubs, DTO copies, event schemas, consumer logic | C1, C2, C3, C5 | fixed | B | same | same | forced by the wheel scan |
| artifact home | C4 | per B | B | same | same | forced by the wheel scan |

A1 totals: 37 fixed (A 11, C 11, D 10, B 5), 3 retained (B-S3, B-N16, B-N20), 6 deferred to the CLI
phase. Class (a) rows A1-A18 stay as `retained` rows with reason "protocol".

## 4. The edge register (part of the enforcement, judged as a mechanism)

**Where.** `tests/architecture/kernel_edges.yml`: data next to the check, not shipped, framework-only.
Not in `architecture_rules.yml`, whose schema is shared with generated apps
(`architecture_rules.yml:3-4`). It absorbs `kernel_module_imports_allowlist.json`, whose rule (shrink
only, a stale entry fails) it keeps.

**Shape.**

```yaml
version: 1
baseline: b8d8506                # the commit the ids refer to
edges:
  - id: B-S1                     # baseline id; new rows get N-<n>
    class: b                     # a | b | c
    type: security_authority     # import | package | runtime_call | configuration | security_authority | naming
    scope: runtime               # runtime | composition_text (templates, recipes, starter text)
    owner: identity              # the module whose thing is named; null for class a
    verdict: disputed            # baseline verdict
    status: open                 # open | fixed | retained | deferred
    carried_by: ADR-C            # the decision or task that owns the change (required unless retained as protocol)
    decision: null               # ADR id that set retained / fixed (required for retained)
    reason: null                 # required for retained
    task: kb-...                 # tm alias doing the change (open, deferred)
    match:                       # anchors, not line numbers
      - path: src/sandloom/shared/guards/platform.py
        literal: '"identity"'
      - path: src/sandloom/core/admin/api/platform.py
        literal: '"identity"'
```

**Check rules** (one new T code, with `sandloom explain`):

1. Every finding of the scan maps to exactly one row's `match`; an unmatched finding fails with
   `file:line`, the pattern and the class it would have.
2. A `fixed` row with a finding fails (regression).
3. An `open` or `deferred` row with no finding fails ("mark it fixed"): the register only shrinks.
4. A `retained` row needs `decision` and `reason`; class (a) rows are `retained` with reason
   "protocol" and a pointer to the loader call site.
5. `--report` prints counts by class, type, scope and status, and the rows a change adds, fixes or
   retains, for the PR description (I-03 "a change reports the edges it adds, removes and approves").

**Scan.** AST of shipped kernel code; string literals outside docstrings; dynamic imports by origin
(I-02); the built wheel for class (c). Module names come from the source layout and installed
entry points at check time, as `test_shared_purity.business_modules` does today
(`tests/architecture/test_shared_purity.py:46-60`); setting names and claim keys come from D's and C's
declarations once they exist. A literal is matched whole (`"identity"`), not as a substring, so
`rpc_identity` or `sandloom.core.cli.dev.identity` are not findings. Templates, recipes and starter text
are scanned under `scope: composition_text` and reported apart (goals.md, answer #2).

**Judged as a mechanism.**

| question | answer |
|---|---|
| removes or provides | provides I-03's missing proof and I-02's (with the origin rule); replaces `test_module_names.py:165` and generalises the allowlist of `fd7b058` |
| compared against | a widened `test_module_names.py` (no per-row status, no reason, no ratchet); inline pragmas next to each edge (local, but no `fixed` status and no report); a register without a scan (a list nobody checks: the baseline's own condition, problems.md #8) |
| cost | about 64 rows (46 judged plus 18 class a); one check; one error code; a row per new edge, reviewed |
| known limits | a contract name built at run time (`f"{m}.entitlements.v1"`) is missed by the literal scan; behaviour (the replaceability fixtures) catches it if it matters. A third-party module's plain short name is caught only when installed at check time |
| abuse risk | `retained` used as an escape hatch: refused without an ADR id |

## 5. Scenario run for decision A

"with C" (or B, D): the scenario passes only when that decision's mechanism lands; A's part is that
the alternative keeps the thing in the kernel with no module name. AD-1 to AD-5 are the admin target
scenarios of the owner decision on `kb-alt-boundary`.

| scenario | A1 | A2 | A3 | A4 |
|---|---|---|---|---|
| A-H1 lean: `--help`, `new`, `create_app()`, `/health`, `/ready` | pass; `sandloom --help` lists only kernel commands in lean | pass | pass | pass |
| A-H2 full install, mono and split, expectations unchanged | pass; CLI tests move to identity's package with unchanged expectations | pass | cost: snapshots gain vocabulary fields (generated diff, I-15 review) | pass |
| A-H3 fixture module with contract, contributor, fact, setting | pass with D | pass with D | pass with D; the author declares claim keys | pass with D |
| A-H4 kernel edge check on the candidate tree | pass (§4) | pass | pass (vocabulary-fed) | **fail**: no name check |
| A-H5 identity replaced by `test_identity` assigned as authority of `roles`, mono and split | pass with C; no kernel file changes | pass with C; `test_identity` must also implement P1 or startup refuses | pass with C | pass with C; the fixture is the enforcement |
| A-B1 split without the owner module; platform routes 403 with no identity authority bound | pass with C (fail closed already: `core/tests/test_admin_operations.py:56`) | pass with C; a service that blocks users needs a binding to P1 | as A1 | as A1 |
| A-B2 `admin create`, `security revoke-sessions` in lean | pass: no installed module provides the command; non-zero exit naming it, no traceback; no fallback policy exists to apply (the scenario's "names the policy it applied" assumes a kernel-hosted command: wording to update, §9) | `revoke-sessions` refuses naming the missing P1 provider; `admin create` as A1 | as A1 | pass only if the lean suite runs the commands |
| A-B3 app module named `identity` writes `roles: ["admin"]` unassigned | pass with C; the register keeps the literal out | pass with C | pass with C; also refused at startup if it declares `roles` | pass with C, if a fixture covers it |
| A-B4 kernel and module wheels upgraded one at a time | pass with B: the kernel ships no module artifact | cost: P1 versioned with the kernel | cost: vocabulary format versioned with the generator | pass with B |
| A-B5 different `SANDLOOM_MODULES` order | pass; M3 collisions refused, so dispatch does not depend on order | pass | pass | pass |
| A-B6, A-B8, A-F2, A-F3, A-F4, A-F6, A-F7, A-F8 (claims trust) | pass with C: merge, mint and gates stay kernel (I-21) | same | same; earlier detection of an undeclared protected key (answer #15: helps, does not replace) | same |
| A-B7 16 subsets in P2 and P3 | pass with the celery packaging fix (`billing-boots-without-celery`) and the matrix; M3 reads metadata without importing modules | same | same | same (the matrix is its definition) |
| A-F1 kernel change adds a module import, computed import, constant or contract literal | pass: origin rule refuses the computed import; literal matched by the channel pattern; loader imports are `retained` class (a) rows | pass | pass for names in the vocabulary; others only with A1's patterns kept | **fail**: literal and computed import pass |
| A-F5 identity loaded nowhere, mark write fails; then a block | pass with M4 and M5 (PoC: a kernel-registered consumer, the loss path) | pass with M4 and P1 | as A1 | detects (fixture, tenancy alone), adds no mechanism: **fail** alone |
| AD-1 no admin: API works, no `/admin/` | pass (today too) | pass | pass | pass |
| AD-2 admin and identity out of the box | pass: `sandloom new` composes both; manifest by M2; `admin create` by M3 | pass | pass | pass |
| AD-3 admin with an alternative identity through the supported auth interface | pass if the issuer serves admin's login interface; the kernel part exists (`issues_tokens`); PoC | same | same | the fixture is the enforcement; same interface |
| AD-4 custom console on platform and module APIs, no `sandloom_admin` | pass: the platform API is guarded by the kernel admission (A8; B-S2 by C) | pass | pass | pass |
| AD-5 third-party `inventory` declares admin resources; `generate` adds its section with no kernel or admin change | pass: admin's compiler imports `<module>.admin` of every loaded module, as `compiler.py:462-470` does today | pass | pass, plus a vocabulary entry | pass |

## 6. Invariants and constraints

| item | A0 | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|
| I-02 no hard-coded module dependency | partial (computed names unresolved) | pass | pass | pass | fail |
| I-03 remaining edges approved and checked | fail | pass | pass | pass | fail |
| I-13 a new module needs no kernel change | fail (settings, risks, atoms) | pass with B, C, D; M2 and M3 are open to any module | pass; a new kind of platform duty needs a kernel port, by design | pass; the author declares vocabulary | pass with B, C, D |
| I-19 every profile and subset boots | — | pass with the celery fix and the matrix | same | same | same |
| I-20 no silent degradation | fail (D1-D4) | pass with B (D4), C (D2, D3), M4 (D1) | pass with B, C, P1 | as A1 | detects only |
| I-21 kernel keeps the protocol | holds; D1 open | holds; the applier moves into the kernel | holds; the kernel also keeps incident revocation | holds | not guarded |
| I-22 application assigns trust | T1-T10 open | T8 by M4, M5; T9 disappears (no kernel operator contract) | T8 by P1; T9 becomes the assignment of P1's provider | T5-T7 strengthened | not addressed |
| I-23 moving an atom changes no execution | n/a | n/a (A moves no atom; C) | same | same | same |
| I-24 one composition per process | holds | holds: M2, M3, M5 are per composition; M4 one consumer per composition | holds | holds | holds |
| H-8, I-17 public surface | — | adds two advanced declarations on `define_module` (commands, generated files); removes `Permissions`, `Roles` under review | adds port stubs and a testing kit | adds vocabulary declarations | none |
| H-9 no aliases | — | command names kept; lean loses them; upgrade steps | kept | kept | kept |
| H-11 generated files | — | holds: M2 inside `generate --check` | holds | holds; more generated content | holds |
| H-12 each mechanism earns its place | — | M1-M5 each remove listed ids | P2 doubtful; P1 versus M5 is a choice, §9.1 | the startup gate overlaps fix #A | holds |
| H-14 no protocol leaves | holds | holds | holds | holds | holds |
| H-15 no silent degradation | fail | pass with B, C | pass | pass | detects |
| H-17 no driver system, no registry | holds | holds: declarations on the existing module system, no lookup by capability | risk: a `platform.*` port family | risk: a universal name index | holds |
| H-18 ports only by the six conditions | — | no port | P1 passes, P2 doubtful, P3-P5 fail | no port | no port |
| H-19 DX | — | holds; operator commands move with identity | holds | an author step per protected key | holds |

## 7. Comparison

| criterion (§4.2) | A0 | A1 | A2 | A3 | A4 |
|---|---|---|---|---|---|
| hard constraints | fails H-1 (I-03, I-13, I-20, I-22) | pass, conditional on B, C, D | pass if P2 is dropped | pass if the startup gate is limited to claim keys | fails H-1 (I-02, I-03) |
| edges left of 46 | 45 | 3 retained, 6 deferred | same; kernel to P1 becomes class (a) | same | about 10 without a behavioural symptom, plus the 6 deferred |
| new mechanisms | 0 | 5 (M1-M3 specific; M4, M5 required by answer #16) | 6-7 | 7-8 | 1-2 |
| per-request work | none | none | none | none | none |
| startup work | none | M5 check; M4 consumer | presence check; P1 consumer | declared-versus-registered gate | none |
| DX, module author | — | declares commands or generated files only if it has them | implements kernel ports | declares claim keys and vocabulary | — |
| DX, operator | — | identity's commands run where identity is installed | kernel commands unchanged; P1 bindings in split | as A1 | — |
| packaging, versioning | — | kernel wheel ships no module artifact (with B) | ports version with the kernel | vocabulary format versions with the generator | as A1 |
| migration | — | CLI code and tests to identity; compiler to admin; one link setting; register seeded from baseline | as A1 except incident commands; P1 in identity | as A1 plus snapshot fields for every module | small; leaves rows |
| what it catches that A1 does not | — | — | issuer-agnostic incident revocation | module squatting another's names at startup | behavioural coupling hidden in computed names |

## 8. What the evidence favours

**A1**, with A4's behavioural checks adopted as A1's tests (lean-venv kernel tests, the P2 matrix, the
replaceability fixtures), and A0's import guards kept as they are.

1. A1 is the only alternative that satisfies I-02 and I-03 with the fewest new mechanisms, and each
   mechanism removes listed rows (M1-M3) or answers answer #16 (M4, M5). Its register extends a guard
   that already exists in the same shape (`kernel_module_imports_allowlist.json`, `fd7b058`).
2. The replaceability PoC shows the existing mechanisms carry the replacements with no kernel change:
   rights through claims contributors, tenant grants through the tenant claim, an unrelated fact over
   contracts, locally and over RPC (validation §1.2). Each failure it found is a trust gap (decision C)
   or a module name in kernel code (decision A), not a missing port. By H-17, that is no case for a new
   registry or port family; by H-18, four of A2's five candidate ports fail or are excluded.
3. A3's extra machinery duplicates approved fix #A and the claim declarations answer #15 defers to the
   PoC; its one gain, startup detection of name squatting, belongs to decision C's trust work.
4. A4 cannot express I-03 and leaves about ten rows that no behaviour exposes, but its checks are what
   proves I-13, I-19, I-20 and I-22 in any alternative.

What would change the conclusion: if the owner states that ending every session regardless of the
issuer is a platform guarantee (§9.1), P1 is the one port that passes H-18 and joins A1 in place of M5;
if the M4 PoC shows the kernel cannot own an event consumer without a module, M4 needs another form.

## 9. Open points

1. **Issuer-agnostic incident revocation.** Is "end every session of these subjects, whatever the
   issuer" a platform guarantee (A2's P1, kernel-owned `security revoke-sessions`), or the issuer's
   function (A1, identity's command)? The ledger records the commands as G-OPS-12 (dry run by default)
   and C-OPS-08 ("through the owners' contracts", `guarantees.yml:4659-4670`, `:6253-6263`), tested in
   `core/tests/test_security_cli.py`; under A1 both entries keep their statements and their tests move to
   identity's package (I-16). Neither states an issuer-agnostic guarantee: today a replacement issuer gets
   the commands only by serving `identity.security.v1` under identity's name (guarantee-map §3), so A1
   removes nothing a replacement has now. Owner question.
2. **M4 feasibility.** Can the kernel register its own consumer of `core.auth.tokens_revoked.v1`
   (owner `core`, one consumer group per deployment, placement in split, the loss path with a failing
   cache)? **Unverified**: consumers are registered by modules today; PoC.
3. **M5, "executed".** Answer #16 asks that the issuer's obligation be registered and executed. M5 checks
   registration; what observes execution (consumer lag, `dead_letter=False` redelivery,
   `sandloom_identity/__init__.py:63-71`) is open.
4. **Admin login interface.** Which existing endpoints form the operator-login interface an alternative
   issuer serves; whether `issues_tokens` at generate time is enough when the console runs in a split
   service. PoC with AD-3.
5. **Set-password link.** Identity's own setting written by `sandloom new` (A1's proposal) or a target
   admin provides over a contract that identity consumes optionally.
6. **Operators in split.** Under A1 identity's commands run from identity's image only; acceptable, or
   should M3 resolve the command from metadata of a module that is bound but not installed (it cannot
   without the code)?
7. **`Permissions`, `Roles` removal** (B-N15): public names with no importer; removal under I-17 with
   upgrade steps, or relocation to identity.
8. **Scenario A-B2 wording** assumes a kernel-hosted command ("names the password policy it applied");
   under A1 the command does not exist without identity. Requirements wording update: owner.
9. **Platform API exposure.** The owner decision calls it a control-plane configuration; whether a
   setting controls it today was not checked: **Unverified**.
10. **B-R2 `tenant_kind`**: kept as a protocol question ("is this tenant a shared account") or moved with
    `Account`; decision C.

## Sources

- Baseline rows and counts: [baseline.md](../baseline.md) §2-§5, §8-§9, §14.
- Guards since the baseline: commit `fd7b058`; `tests/architecture/test_shared_purity.py:1-60`;
  `tests/architecture/kernel_module_imports_allowlist.json`; `.importlinter:17-22`;
  `guarantees.yml:4051-4063` (G-KERNEL-10), `:4094-4104` (G-KERNEL-13).
- Code read at `20a22b4`: `sandloom_admin/api.py:170-186`; `core/cli/generated.py:70`, `:547-561`;
  `core/admin/compiler.py:1-20`, `:455-529`; `core/cli/admin.py:20-62`, `:109-170`, `:525-531`;
  `core/cli/security.py:1-60`; `src/sandloom/__init__.py:85-100`, `:225`, `:304-305`;
  `core/auth/hashing.py:1-30`; `sdk/security.py:1-70`; `core/sql_alchemy/money_ledger.py:1-30`;
  `core/unit_of_work/intent.py:1-40`, `:85-110`, `:330-360`; `shared/subject.py:55-80`;
  `shared/guards/platform.py`; `core/descriptors/__init__.py:147`; `core/app_factory/pages.py:120-145`,
  `:280-305`; `core/testing/static_checks.py:69-71`; `sandloom_identity/__init__.py:55-110`;
  `sandloom_billing/admin.py:1-20`; `architecture_rules.yml:1-20`.
- Trust, authorization, settings: [research-trust.md](research-trust.md) §6;
  [research-authz-portability.md](research-authz-portability.md) §7;
  [research-settings.md](research-settings.md) §7.4-§7.6.
- Behaviour without batteries and with replacements: [guarantee-map.md](../guarantee-map.md) §2, §8, §11,
  §12; [validation.md](../validation.md) §1.2-§1.5.
