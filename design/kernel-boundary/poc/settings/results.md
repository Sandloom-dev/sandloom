# KB-49: settings PoC gate (ADR-0015, D2a), all sixteen scenarios P1-P16

tm task `kb-49-settings-poc-gate` (wave W0 of [migration-plan.md](../../migration-plan.md)). Builds
ADR-0015's mechanism (D2a declarations on registration, one source read, validation before
`setup`, one post-load check, risk namespaces, the static gate, `config dump` collect mode) in a
patched kernel copy and runs every row of [alternatives-d-settings.md](../../alternatives-d-settings.md)
§11 against real processes. This branch is never merged. Only these records are meant for
`arch/kernel-boundary`.

## Verdict

**D2a passes the ADR-0015 criteria. ADR-0015 does not need reopening, and none of its fallbacks
is triggered.** The reading per row:

- **Pass, as specified: P1-P11, P14-P16.**
- **P12 picks D2a**, the authoring form ADR-0015 chose.
- **P13 passes, read strictly.** On the final run (15 alternating rounds) the candidate's median is
  4 ms below base, against a 46 ms gap between the two base series. The mechanism's own work is
  5-12 ms per boot, measured in-process. Almost all of it is one scan of the installed entry
  points that the loader already does. An earlier run on a loaded machine gave +24 ms against
  5 ms of base-vs-base noise (§3.13), so KB-50 re-measures M-boot as planned.

Findings F-1..F-6 (§4) change ADR-0015's text, not its decision: the author-facing spelling
(`ClassVar` on the metadata; `api.settings` already exists, so the hand-written form is
`api.declare_settings`), one more advanced SDK name (`secret_problem`), three testing names, and
where the public names live (`core.module_api`, for ADR-0007 layering).
`config dump` with the composition costs **+1.9 s** over today's dump, against the research
estimate of ~0.9 s. ADR-0015 names this as the trigger for D3 "if too slow". Whether 3.3 s is
too slow is the owner's call; the PoC treats it as acceptable and records it.

| row | criterion | result |
|---|---|---|
| P1 | I-13: adding a module edits no kernel file | **pass** |
| P2 | refusal parity, 5 cases × 9 entry points + doctor | **pass** (50/50, 5 controls) |
| P3 | accepted module risk starts every entry point, logged, counted, listed | **pass** |
| P4 | risk namespaces (a)-(d), split, one shared anchor, K1 only | **pass** |
| P5 | optional module with an invalid value refuses | **pass** |
| P6 | unloaded module's variables inert at run time, reported statically | **pass** |
| P7 | sources and precedence; one `.env` parse, one lookup per alias | **pass** (after a fix, §3.7) |
| P8 | tiers and ratchets; removed module setting | **pass** |
| P9 | T014 shared module value; static collisions and prefix | **pass** |
| P10 | billing and identity ported; ledger tests; console list | **pass** |
| P11 | `config dump`: values, provenance, redaction, no socket, no lifespan | **pass**; +1.9 s wall time |
| P12 | DX: D2a against D2b | **D2a** (no clear D2b advantage) |
| P13 | boot cost within noise | **pass** (−4 ms against 46 ms base-vs-base) |
| P14 | deploy tooling reads declarations | **pass** |
| P15 | import audit under `python -I` | **pass** |
| P16 | H1: password hashing G-AUTH-15..18 unchanged | **pass** |

## 1. Setup

| item | value |
|---|---|
| base | `3806ad0` (`arch/kernel-boundary`; the merged baseline `c9b9b05` with PRs #71-#74, plus the accepted ADRs) |
| candidate | base + [`d2a-kernel.patch`](d2a-kernel.patch): 71 files, +1992/−424. New: `core/config/module_settings.py`, `core/module_api/_settings.py`, `core/cli/settings_gate.py`, `sandloom_billing/settings.py`, `sandloom_identity/config.py`. ~25 test files ported. Nothing under `src/` or `packages/` of this checkout was edited |
| fixture | [`fixture/`](fixture), outside `src/`. `inventory` has a secret (`INVENTORY_API_TOKEN`), a bounded ops setting (`INVENTORY_RESERVE_TTL_SECONDS` 1..3600), a semantic setting (`INVENTORY_ALLOW_OVERSELL`), the risk `inventory.oversell_in_prod`, a production check, a removed setting (`INVENTORY_LEGACY_SYNC`), a shared and console setting (`INVENTORY_WAREHOUSE`), a reader route and a `setup` reader. Variants: `inventory_dup` (declares an inventory name), `inventory_unprefixed` (`RESERVE_LIMIT`), `inventory_bad_import` (reads settings at import) |
| second case | billing ported in full (8 names, Stripe keys as `SecretStr`, the two validators as model validators, risk `billing.fake_psp_in_prod`, both removed settings, shared `BILLING_DENY_ON_PAST_DUE`, console `BILLING_PROVIDER`, dev values). Identity ported for `TWOFA_ENCRYPTION_KEY` (presence and strength). SSO, Telegram and the refresh-session pair stay kernel fields here; KB-53 moves them |
| processes | every scenario is a real interpreter in an empty cwd with only PATH/HOME, `PYTHONPATH=fixture/` and the scenario's variables: the G-CONFIG-07 harness shape. Mono is one process; split is two services (`billing-svc`: billing + inventory; `tenancy-svc`: tenancy) sharing one anchor, from one venv (K1 = one image) |
| services | own containers `kbset-pg` (postgres:16, 127.0.0.1:38633) and `kbset-redis` (redis:7, 127.0.0.1:38680, 256 dbs; test dbs 16-17 and up, never 0); `SANDLOOM_TEST_OWNER=kb-settings`; removed after the run |

```bash
S=<scratchpad>
git worktree add --detach $S/kernel 3806ad0 && (cd $S/kernel && git apply <this dir>/d2a-kernel.patch && uv sync)
git worktree add --detach $S/base 3806ad0 && (cd $S/base && uv sync)
docker run -d --name kbset-pg -e POSTGRES_PASSWORD=test_password -e POSTGRES_USER=test_user -e POSTGRES_DB=sandloom_test -p 127.0.0.1:38633:5432 postgres:16
docker run -d --name kbset-redis -p 127.0.0.1:38680:6379 redis:7-alpine redis-server --databases 256
export SANDLOOM_TEST_OWNER=kb-settings SANDLOOM_TEST_DATABASE_URL=postgresql+asyncpg://test_user:test_password@127.0.0.1:38633/sandloom_test \
       SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:38680/16 SANDLOOM_TEST_BROKER_URL=redis://127.0.0.1:38680/17
$S/kernel/.venv/bin/python run.py --py $S/kernel/.venv/bin/python --base-py $S/base/.venv/bin/python   # -> out/scenarios.txt
$S/kernel/.venv/bin/python bench_boot.py $S/base/.venv/bin/python $S/kernel/.venv/bin/python 15         # -> out/bench_boot.txt
(cd $S/kernel && uv run pytest $(cat p10_tests.txt) && uv run pytest $(cat p16_tests.txt))              # -> out/p10.txt, out/p16.txt
(cd $S/kernel && uv run sandloom test --full)                                                         # -> out/full-suite.txt
```

## 2. What was built (D2a in the kernel copy)

| ADR-0015 | mechanism | where (patched copy) |
|---|---|---|
| decision 1 | `define_module(..., settings=Model)`; `api.declare_settings(Model)` for a hand-written `register` (§4 F-2); the model is plain pydantic, metadata in `ClassVar` class attributes (`semantic`, `risks`, `removed`, `shared`, `console`, `backup`, plus `dev_env` / `dev_accepted_risks` for P14); `settings_of(Model)`; `override_settings` / `patch_settings` testing overrides; `register.__sandloom_settings__` exposes the declaration to static tools | `sdk/_decl/module.py`, `core/module_api/{_define,_registration,_settings}.py`, `core/config/module_settings.py` |
| decision 2 | `SourceSnapshot`: env > `.env` (parsed once) > `SECRETS_DIR` (one lookup per declared alias, never listed), with each key's origin. The process load of kernel `Settings` reads through it (`_SnapshotSource`, pydantic-settings' own decoding), and modules validate from the same instance | `module_settings.SourceSnapshot`, `config/settings.py` `_load_into` |
| decision 3 | validation inside the registration draft, before `setup`, with context `environment`, `accepts_risk` (local or full id; a `KeyError` for an undeclared one) and `kernel`. A failure raises `ModuleSettingsError(ConfigurationError)` with the G-CONFIG-07 prefix. The post-load check runs at the end of `_load_with_summary`, so every entry point that builds the composition runs it: collisions (`SANDLOOM-B036`), risk namespaces, removed settings, the prod typo check over the union, then module overrides and risks logged once, with `sandloom_accepted_risk_active` / `sandloom_config_override_active` set | `module_settings.{validate_module_settings,declare,post_load_check}`, `loader/plugin_loader.py` |
| decision 4 | namespaced ids. Kernel `Settings` refuses a flat non-kernel id and names the replacement by importing the installed declarations (option ii); `<loaded>.<x>` is checked against the declaration; `<known, not loaded>.<x>` is inert and listed "not applicable here"; `<unknown>.<x>` is refused. Known = K1 entry points ∪ loaded ∪ `SANDLOOM_MODULES` entries | `module_settings.{flat_risk_problem,post_load_check}`, `config/accepted_risks.py` |
| decision 5 | kernel `SEMANTIC` loses `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` (ratchet 12/14); module tiers come from the declaration (undeclared = ops), are recorded in `module.snapshot.json` `"settings"` by `sandloom generate`, and are compared by the static gate; `/diagnostics/core` gains `module_tiers` | `config/tiers.py`, `cli/generated.py`, `cli/settings_gate.py` |
| decisions 6, 7 | `sandloom check settings [--modules] [--profile] [--service NAME:ENV]`: collisions across every module of the app; the prefix rule with snapshot grandfathering; tiers against the snapshot; per service, unknown `SANDLOOM_MODULES` names, variables for a module the service does not load, and literal values validated (placeholders skipped). T014 unions each loaded module's `shared` names over the services that load it | `cli/settings_gate.py`, `cli/check.py`, `testing/deploy_profile.py` |
| decision 8 | `config dump` imports the composition root in collect mode (a context variable, never the environment); module values with provenance from the snapshot; redaction by the declared `SecretStr` plus the PR #74 name rule; problems on stderr, exit 0; `--json` stays a list; `--kernel-only` is today's path | `cli/config.py`, `module_settings.collect_mode` |
| decision 11 | billing and identity ported (§1); console list = kernel allowlist + loaded declarations' `console`; dev env and T014 shared keys from declarations; `deploy_profile.py` names no module setting; `fake_psp_in_prod` → `billing.fake_psp_in_prod` in compose files, examples, `.env.example`, the scaffold and the smoke check | `admin/api/platform.py`, `testing/deploy_profile.py`, `deploy/`, `examples/` |
| doctor | the loaded modules' overrides and the not-applicable ids, in the config section | `cli/doctor.py` |

Not built (no P-row needs it): backup notes per module secret (B-C8 reads `backup`; the
declarations carry it), the docs generator over declarations, the move of the SSO, Telegram and
refresh-session names, a doctor collect mode that lists every module's problem (doctor shows the
first, as today).

## 3. Scenarios

Transcripts: [`out/scenarios.txt`](out/scenarios.txt) (every driver row), [`out/bench_boot.txt`](out/bench_boot.txt),
[`out/p10.txt`](out/p10.txt), [`out/p16.txt`](out/p16.txt), [`out/full-suite.txt`](out/full-suite.txt),
[`dx/`](dx) (P12 inputs and both authored modules). Output is trimmed below.

### P1 — I-13: a module adds settings with no kernel change

The kernel diff names no fixture module: the only `inventory` hits are an unrelated comment and a
docstring example. `inventory` and its three variants live in `fixture/`, outside `src/`, and load
by `SANDLOOM_MODULES` + `PYTHONPATH`. The P12 agents each added a fourth module (`catalog`) with
a secret, a bounded setting, a semantic setting, a risk, a production check and a removed
setting, without opening or editing a kernel file (both reports: "Framework (kernel) source
files opened: none"). **Pass.**

### P2 — refusal parity

Five cases, each run on app, worker, beat, `migrate databases`, the outbox/streams/projection/sagas/backup
CLIs, and doctor. All 50 exit 1 (doctor exits 2 with the FAIL row), with the kernel prefix and
the same reason as the last line:

```
fixture-invalid-value    [core.config] Invalid configuration: module inventory: INVENTORY_RESERVE_TTL_SECONDS: Input should be greater than or equal to 1
fixture-oversell-in-prod [core.config] Invalid configuration: module inventory: INVENTORY_ALLOW_OVERSELL=true is not allowed with ENVIRONMENT=prod; accept the risk: SANDLOOM_ACCEPTED_RISKS=["inventory.oversell_in_prod"]
billing-provider-unset   [core.config] Invalid configuration: module sandloom_billing: BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod: set BILLING_PROVIDER=stripe ...
identity-weak-totp-key   [core.config] Invalid configuration: module sandloom_identity: TWOFA_ENCRYPTION_KEY[0] is too short for production (>= 32 characters); ...
duplicate-env-name       [core.config] Invalid configuration: SANDLOOM-B036: INVENTORY_RESERVE_TTL_SECONDS is declared by module inventory and module inventory_dup
doctor (each case)       rc=2 FAIL modules/composition_root: ModuleSettingsError: <the same reason>
```

Each case's environment builds once the value is fixed (5 controls, `built`). PR #71's existing
G-CONFIG-07 matrix (`test_boot_configuration_checks.py`) also passes on the candidate; its
`config dump` test is ported to decision 8 (§3.11). **Pass.**

### P3 — an accepted module risk

Prod (strict profile), `INVENTORY_ALLOW_OVERSELL=true`, `SANDLOOM_ACCEPTED_RISKS=["inventory.oversell_in_prod"]`.
None of the nine entry points refuses, and each logs `ACCEPTED RISK: inventory.oversell_in_prod — ...`
(worker and beat keep running until the 25 s timeout; the CLIs then fail on the unreachable
database host, past configuration). `/diagnostics/core` `configuration.accepted_risks` lists it,
`sandloom_accepted_risk_active{risk="inventory.oversell_in_prod"} 1.0` is set, and doctor shows
`WARN config/override:SANDLOOM_ACCEPTED_RISKS[inventory.oversell_in_prod]`. **Pass.**

### P4 — risk namespaces in a split

One shared anchor (prod strict, `BILLING_PROVIDER=fake`); app, worker and `migrate databases` on
each service:

| variant | billing-svc (billing + inventory) | tenancy-svc (tenancy) |
|---|---|---|
| (a) `billing.fake_psp_in_prod` | starts, risk applied | starts; `/diagnostics/core` and doctor list it as "not applicable here" |
| (b) + `bilingg.fake_psp_in_prod` | refused: `no module 'bilingg' is known in this deployment` | refused, same text |
| (c) + `billing.free_money` | refused: `'billing.free_money', which module billing does not declare (declared: ['billing.fake_psp_in_prod'])` | starts: billing is known (K1) but not loaded, so rule (2) makes it inert, and the static gate is where it is caught |
| (d) flat `fake_psp_in_prod` | refused by kernel `Settings`: `unknown risk id 'fake_psp_in_prod'; module risk ids are namespaced: use 'billing.fake_psp_in_prod'` | refused, same text: the replacement is named in a process without billing too (option ii, K1) |

Every refusal is identical across the three entry points. K1 alone (one image) served the split;
K2 was not needed (O-7). **Pass.** Read (c) in the tenancy service as the ADR states it: an
undeclared id of a known, unloaded module passes there until a process that loads the module
refuses it.

### P5 — optional module, invalid value

`SANDLOOM_MODULES=["inventory"]`, `MODULE_REQUIRED=[]`, `INVENTORY_RESERVE_TTL_SECONDS=99999`:
`rc=1 [core.config] Invalid configuration: module inventory: INVENTORY_RESERVE_TTL_SECONDS: Input should be less than or equal to 3600`.
`ModuleSettingsError` is a `SystemExit` the per-module `except Exception` does not catch. The
same module loads when valid (control). **Pass.**

### P6 — a module's variables where it is not loaded

Billing only, with `INVENTORY_RESERVE_TTL_SECONDS=0` (invalid for inventory) and
`INVENTORY_API_TOKEN` set: the process builds, and `config dump --json` lists no `INVENTORY_*`.
`sandloom check settings` over a two-service topology:

```
FAIL pay: SANDLOOM_MODULES names unknown module 'invntory'
FAIL pay: sets ['INVENTORY_API_TOKEN'] for module inventory, which this service does not load
FAIL shop: module inventory: INVENTORY_RESERVE_TTL_SECONDS: Input should be greater than or equal to 1
```

(plus `pay: module billing: BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod`, a correct
literal-value finding). Exit 1. **Pass.**

### P7 — sources

A cwd with `.env` and a `SECRETS_DIR`, the same names in two or three sources:

```
INVENTORY_RESERVE_TTL_SECONDS = 9 [env]          (env 9 > .env 8 > secrets 7)
INVENTORY_WAREHOUSE = dotenv-wh [dotenv]         (.env > secrets)
INVENTORY_API_TOKEN from-secrets [secrets_dir]
INVENTORY_ALLOW_OVERSELL = true [dotenv], LOG_LEVEL = DEBUG (kernel, from .env)
STRIPE_WEBHOOK_SECRET from SECRETS_DIR (billing)
spy: dotenv parses=1, dotenv_values calls=1, secret lookups: 208 aliases, max 1 per alias, SECRETS_DIR listed 0 times
```

**Pass**, after one fix. The first run counted two `dotenv_values` calls: pydantic-settings
builds its default `.env` source before `settings_customise_sources` replaces it. The process
load now passes `_env_file=None`, and the snapshot is the one read. KB-50 must keep that
argument; the spy is its test.

### P8 — tiers, ratchets, removed settings

- `test_config_tiers.py` passes with `SEMANTIC_BASELINE_COUNT=12`, `SEMANTIC_COUNT_CEILING=14`.
- Standard-install semantic total unchanged: base kernel 15; candidate kernel 14 + billing 1 +
  identity 0 = 15. No unclassified kernel name.
- `INVENTORY_ALLOW_OVERSELL=true` in dev: `[INFO] sandloom.settings: CONFIG OVERRIDE: INVENTORY_ALLOW_OVERSELL=true weakens the dev profile value false`.
- `INVENTORY_LEGACY_SYNC=true`: `module inventory: INVENTORY_LEGACY_SYNC was removed in 0.2 and INVENTORY_LEGACY_SYNC=true is no longer supported: stock sync is always event-driven now; drop it from the environment` (today's shape); `=false` warns and boots.
- Billing's two removed settings moved out of kernel `REMOVED`.

**Pass.**

### P9 — split shared value, static collisions

- T014 `_shared_settings` on a profile where `shop` and `stock` both load inventory with
  `INVENTORY_WAREHOUSE` north/south: `INVENTORY_WAREHOUSE differs between processes (['north', 'south'])`.
  With equal values, no inventory problem.
- `sandloom check settings --modules inventory,inventory_dup,inventory_unprefixed` (never loaded
  together): `SANDLOOM-B036: INVENTORY_RESERVE_TTL_SECONDS is declared by module inventory and module inventory_dup`
  and `inventory_unprefixed: new setting RESERVE_LIMIT must start with INVENTORY_UNPREFIXED_`.
- Control: billing, identity, tenancy and inventory are clean. `STRIPE_*` and
  `TWOFA_ENCRYPTION_KEY` are grandfathered because `sandloom generate` recorded them in each
  module's `module.snapshot.json` `"settings"`.

**Pass.**

### P10 — bundled port

Ledger tests on the candidate (`out/p10.txt`): G-BILLING-18, G-RISK-07, G-AUTH-19 and G-CONFIG-07,
every test id `guarantees.yml` lists for them: `14 passed`. Each claim is kept:

- The kernel-side tests now assert the module refusal: `test_prod_rejects_explicit_fake_psp_unless_opted_in`
  validates `BillingSettings` (dev passes; prod refuses; `billing.fake_psp_in_prod` passes; the
  flat id is refused by kernel `Settings` naming the replacement).
- `test_prod_twofa_key_must_be_strong` validates `IdentitySettings` with a weak key, a missing
  key and a good list.
- The registration tests build a real `ModuleAPI` (a `MagicMock` core would swallow
  `declare_settings`) and assert `SystemExit` with the reason and nothing registered.
- `test_ledger_runtime_pins` drops `fake_psp_in_prod` from the kernel pin and pins billing's
  declared `billing.fake_psp_in_prod`.

Console list (D-B5): with identity, tenancy and billing the module rows are `["billing_provider"]`;
without billing they are `[]`; the kernel allowlist no longer names it. **Pass.**

### P11 — `config dump`

- Modules inventory, billing (stripe), identity and tenancy; socket spy on `socket.connect`,
  `socket.create_connection` and `asyncio.open_connection`; a wrapper on `_make_lifespan`:
  `connects=[] lifespans=[] rc=0`. Billing builds its `StripeHttpClient` in `setup`, so building
  a client does not connect.
- Provenance: `INVENTORY_API_TOKEN = <redacted> [env module=inventory]`, `BILLING_PROVIDER = stripe [env module=billing]`.
- Redacted: `INVENTORY_API_TOKEN`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.
- Invalid module value: exit 0, `PROBLEM: [core.config] Invalid configuration: module inventory: INVENTORY_RESERVE_TTL_SECONDS: ...`
  on stderr, and the entry shown `(invalid: see problems)`. `--json` stays a list (a first cut
  turned it into an object and broke `test_ops_cli`; fixed).
- `--kernel-only --json` is byte-identical to today's `config dump --json` minus the 9 moved
  entries (196 entries).
- The text form has the same lines and values. Only the column width differs, because the
  longest name (`BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`) moved to billing. "Byte for byte"
  can only hold minus the moved names, padding included.
- Wall time, process, median of 10 alternating runs: today **1.452 s**, candidate `--kernel-only`
  1.757 s (a spot check of 3 runs each gives equal or faster: noise), candidate with the
  composition **3.338 s (+1.886 s)**. The research estimate was ~0.9 s.

**Pass** (criterion: no connection, no lifespan, no crash). The wall time is reported for the
owner (ADR-0015 "Conditions to revisit": D3 for the dump only if too slow).

### P12 — DX, D2a against D2b

The same task ([`dx/TASK.md`](dx/TASK.md): a `catalog` module with a secret, a bounded ops
setting, a semantic setting, a risk, a production check, a removed setting, a reader, five tests,
`mypy --strict`) was given to two agents of the same model. Each had only its form's one-page
reference ([`dx/REFERENCE-d2a.md`](dx/REFERENCE-d2a.md), written from ADR-0015 decision 1 as
it stands; [`dx/REFERENCE-d2b.md`](dx/REFERENCE-d2b.md), from alternatives-d §5.2). D2b ran on a
shim over the same runtime ([`dx/d2b/sandloom_d2b.py`](dx/d2b/sandloom_d2b.py)). Outputs are in
[`dx/out/`](dx/out).

| | D2a | D2b |
|---|---|---|
| files created / edited | 4 | 4 |
| settings-form errors | **1**: pydantic refuses the reference's un-annotated `semantic = (...)` (needs `ClassVar[...]`) | 0 |
| other errors | 2 (prod-boot harness needed a durable event bus; a BSD `sed` slip) | 3 (the same two, plus a stale run) |
| documentation lookups after the first read | 0 | 0 |
| kernel files read | 0 | 0 |
| `settings.py` lines | 43 | 23 |
| tests | 5 passed | 5 passed |
| `mypy --strict catalog` | clean | clean (`--follow-imports=silent`; the PoC shim itself is not typed clean) |

**Decides D2a**, ADR-0015's choice. D2a's one extra error is a documentation defect (the ADR's
example omits `ClassVar`), not a cost of the form. With the corrected example the two forms tie
on errors, lookups and kernel reads. D2b is shorter (23 against 43 lines) because a base class
can declare the `ClassVar`s and per-field metadata. ADR-0015 already weighed that against one
more SDK name and more concepts, so it is no reason to switch. A self-report by two agents on
one task is weak evidence; it falls short of "D2b clearly cheaper", the ADR's condition to
revisit.

### P13 — boot cost

`create_app` (`import sandloom.main`, identity + tenancy + billing), fresh interpreter per
sample, 15 alternating rounds of base A / candidate / base B / candidate + fixture
(`out/bench_boot.txt`):

```
base A                 median  1014.5 ms  min   884.7  max  1137.7
candidate              median  1033.1 ms  min   906.5  max  1389.4
base B                 median  1060.4 ms  min   892.6  max  1248.6
candidate + fixture    median  1017.8 ms  min   900.0  max  1123.5
noise |base A - base B| = 45.9 ms; candidate - base = -4.4 ms; fixture adds -15.3 ms
within noise
```

This is the final run. The first bench gave +168 ms. A profile traced it to the prototype re-scanning the installed
entry points six times per boot (`installed_module_names()`). Read once per process, the
in-process cost of the mechanism is:

```
installed_module_names 4.8-11.7 ms (one entry-point scan); declare 0.1-0.2 ms; post_load_check 0.1 ms; _report_module_overrides 0.1 ms
```

**Pass.** The final run is within base-vs-base noise (−4.4 ms against 45.9 ms). A run before it,
taken while other sessions loaded the machine (single samples 1.1-3.6 s), gave +24 ms against
4.9 ms of base-vs-base noise. Both runs agree on what the mechanism costs: one entry-point scan,
which the loader already does in `_load_with_summary`. KB-50 shares it (passes the loader's
listing in) and re-measures M-boot.

### P14 — deploy tooling

- `dev_shell(deploy/compose/microservices.yml)` takes `BILLING_PROVIDER=fake`, a 64-character
  `TWOFA_ENCRYPTION_KEY` and `SANDLOOM_ACCEPTED_RISKS=local_storage_in_prod,billing.fake_psp_in_prod`
  from the declarations (`dev_env`, `dev_accepted_risks`).
- `deploy_profile.py` names none of `BILLING_DENY_ON_PAST_DUE`, `TWOFA_ENCRYPTION_KEY`,
  `BILLING_PROVIDER`.
- `boot_problems` boots every HTTP service of the shipped profile (billing, identity, tenancy)
  with that env. Registration validates from it and no production check fires. The billing
  accepted risk is now reported in doctor's config section, like kernel risks; a first cut put
  it in the modules section, where `boot_service` counts warnings as failures.
- `profile_problems` on the shipped profile gives `[]`.
- `test_the_framework_profile_is_the_generators_output` needed the profile regenerated after the
  risk rename (block hash).

**Pass.**

### P15 — import audit

`tools/checks/import_audit.py` under `python -I`, the fixture put on `sys.path` (`-I` ignores
`PYTHONPATH`):

- `inventory, sandloom_billing, sandloom_identity`: `0 violation(s), 0 import error(s)`.
- `inventory_bad_import` (module-level `settings_of`): `rc=1 ... 1 import error(s)` —
  `SettingsNotBoundError: settings_of(BadSettings) with no application bound`.

**Pass.**

### P16 — password hashing (H1)

G-AUTH-15..18 (14 test ids from `guarantees.yml`): `19 passed` (`out/p16.txt`). `core/auth/hashing.py`
and `core/config/password_params.py` are untouched. In those test files the only change is the
risk-id rename in one shared env constant of `test_password_hash_config.py`. **Pass.**

## 4. Findings for ADR-0015 and KB-50..56

| id | finding | consequence |
|---|---|---|
| F-1 | pydantic refuses un-annotated class attributes: `semantic = (...)` must be `semantic: ClassVar[tuple[SemanticSetting, ...]] = (...)` (each metadata attribute) | ADR-0015 decision 1 example and the KB-50 docs; P12's only D2a error |
| F-2 | `ModuleAPI.settings` already exists (the kernel `Settings` property, used by kernel code and tests), so the hand-written form cannot be `api.settings(Model)` | name it `api.declare_settings(Model)` (as here) in ADR-0015 decision 1 |
| F-3 | a module's `settings.py` submodule replaces the package attribute `settings` that `__init__` imports from `sandloom.sdk.infra` (identity's `_setup` read `settings.jwt_secret` and got the submodule) | `sandloom add module` scaffolds `settings.py` only where `__init__` imports no `settings`; identity's declaration is `config.py` here; KB-53 decides the file name |
| F-4 | identity's TOTP-key strength check needs the kernel's secret-strength rule (`secret_problem`), which no SDK module exports | a sixth advanced SDK name (`sandloom.sdk.module.secret_problem`) beside the five of decision 1, under I-17 review |
| F-5 | module tests may import only the public surface (SANDLOOM-C013), and the snapshot is read once per process | two more testing names: `reset_process_snapshot(monkeypatch)` and `validate_settings(Model, kernel)` (plus `ModuleSettingsError`) in `sandloom.sdk.testing` |
| F-6 | `settings_of` in `sandloom.core.config` cannot reach the runtime (ADR-0007 layering, the config layer imports nothing above it) | the active-core lookup is injected by `core.module_api` (as here); the public names come from `core.module_api`, since `sandloom.sdk` imports only the public kernel surface |
| F-7 | pydantic-settings builds its default `.env` source before `settings_customise_sources` (P7's second read) | the process load passes `_env_file=None`; P7's spy is KB-50's test |
| F-8 | one entry-point scan per boot is the mechanism's only measurable cost (P13) | share the loader's listing (KB-50) |
| F-9 | a test built on `register(MagicMock())` no longer sees a refusal: validation is the real `ModuleAPI.declare_settings` | the port builds a real `ModuleAPI`; KB-52..54 do the same |
| F-10 | `config dump --json` must stay a list (`test_ops_cli`); problems go to stderr | decision 8 wording |
| F-11 | P4 (c): in a process where the module is known but not loaded, an undeclared id is inert (rule 2) | as designed; the static gate (KB-56) should also check every namespaced id in a profile against the declarations |
| F-12 | `config dump` with the composition costs +1.9 s (3.3 s against 1.45 s), twice the estimate | the owner's call under "Conditions to revisit" (D3 for the dump only) |

## 5. Full suite and gates on the candidate

`uv run sandloom test --full` in the kernel copy (`out/full-suite.txt`): **full suite 6555 passed, 10 skipped, 1 xfailed, 0 failed**; generated files, schema & deploy, ruff format, mypy, `sandloom check`, lint-imports, contract and route snapshots, migration drift and typing tests pass. The first full run had 4 test failures and broken layering, all from the prototype; each was fixed (F-5, F-6, F-10, the profile block hash, the redaction test enumerating module declarations).

Static gates the prototype does not meet (ruff check, 6 architecture tests, the public API baseline), none of them a criterion and all left to KB-50:

- `ruff check` on the changed paths reports 34 style findings: function-local imports
  (PLC0415) and complexity (C901) in the prototype code.
- The lint-suppression ratchet.
- The SDK reference blocks and the public API baseline. Both are stale by construction: new SDK
  names await the I-17 review (F-4, F-5).
- The CI-workflow pin, and the source-export selection (new files not listed).

`lint-imports` (8/8 contracts) and `mypy` are clean.

The driver's only failing row (`out/scenarios.txt`: 134/135) is the text form of `--kernel-only`.
It differs from today's output only in column padding (§3.11, P11).

## 6. Not verified

- Beat and worker were run to "past configuration" (25 s, or until the broker DNS failure), not
  to serving tasks.
- Per-service images (K1 without a module's wheel): no such deployment exists; reasoned only (a
  refusal, the safe direction).
- D2b ran on a PoC shim, not on a kernel-quality implementation; its mypy cleanliness was
  checked with `--follow-imports=silent`.
- Doctor lists one problem per module, not every problem (doctor collect mode not built).
- The boot numbers come from a shared machine (P13: two runs, one loaded).
