# DX baseline log: a typed `QuotaAvailable` check, wired, tested, split

Performed once on 2026-11-08 (10:58–11:08 UTC) by an agent, following only the shipped
docs (`docs/`), `sandloom --help`, `sandloom docs …`, `sandloom explain …` and the
`sandloom new` output. Throwaway app outside the repository (scratch directory,
`dxquota`); framework = this checkout at `arch/kernel-boundary` (src tree identical to
`44451e4`). Kernel changes needed: **none**.

Task: a provider module `quota` owns the quota; a consumer module `reports` gates a route
on a typed `QuotaAvailable(units=1)`; both tested; then provider and consumer run as two
processes.

Legend: **CLI** = a framework command; **DOC** = a doc lookup; **EDIT** = a manual edit
of a file; **ERR** = an error the framework or its docs caused; **SELF** = my own mistake
(not counted against the framework).

| # | kind | what | result |
|---|---|---|---|
| 1 | DOC | `docs/getting-started/index.md` (Quickstart) | the steps below |
| 2 | DOC | `tools/smoke/quickstart_check.py` for the hidden flags `--framework-source path --framework-path` | needed only to run the checkout instead of the PyPI alpha; not a shipped doc |
| 3 | CLI | `sandloom new dxquota --no-batteries --module quota --agents claude --yes --framework-source path --framework-path <repo>` | 43 files, git repo, 2.4 s |
| 4 | CLI | `uv sync` | OK |
| 5 | CLI | `sandloom generate` | OK |
| 6 | CLI | `sandloom dev up` | Postgres + Redis in Docker, `.env` written, 7.7 s |
| 7 | DOC | `docs/modules/http/authorization.md` | atoms table; "a module's own domain-specific atoms live in its package"; no how-to for writing one |
| 8 | DOC | `sandloom docs Requirement`, `AccessFact`, `Entitled`, `register_access_fact` | the last: "no public sandloom.sdk name" |
| 9 | DOC | `sandloom docs recipes` | no recipe for a module's own access check |
| 10 | DOC | `docs/reference/sdk/authz.md` (`AccessFact`) | a fact is named by a string and has no parameter; `Entitled("quota.available")` would be untyped and could not carry `units` (remote `facts.v1` evaluates `(name, subject, request)`) |
| — | decision | typed atom = a `Requirement` subclass in the **consumer**, calling the provider's generated contract stub | the only typed, parameterised form the docs allow without importing the provider |
| 11 | DOC | `sandloom docs recipe call-contract` | `Contract(REF)`, `fake_contract` |
| 12 | DOC | `docs/modules/between/add-contract.md`, `sandloom add contract --help` | |
| 13 | CLI | `sandloom add contract quota limits` | `contracts.py` (stand-in `status`, `record`), DTO `LimitsStatus`, stub, DTO copy, `contracts.snapshot.json` |
| 14 | CLI | `sandloom add module reports` | consumer module, 1 s |
| 15 | EDIT | `quota/contracts.py`: replace the stand-ins by `available(owner_id, units) -> bool` | |
| 16 | EDIT | `quota/schemas.py`: remove the stand-in DTO `LimitsStatus` | |
| 17 | EDIT | `quota/services.py`: `ITEM_LIMIT`, `count_items()` | |
| 18 | EDIT (new file) | `reports/access.py`: `class QuotaAvailable(Requirement)` — `tier = TIER_LIVE`, `category`, `evaluate`, `contracts()`, `describe()` (31 lines) | author must know tiers, categories and the `contracts()` hook from the reference |
| 19 | EDIT | `reports/api.py`: `POST /exports` with `Depends(require(QuotaAvailable(units=1)))` | |
| 20 | CLI + ERR 1 | `sandloom generate` | SANDLOOM-T004 / T007 "method removed": replacing the generator's own stand-ins of a never-released contract counts as a breaking change; nothing written |
| 21 | DOC | `sandloom explain SANDLOOM-T004` | advises a `.v2` (wrong for an unreleased contract); the call-contract recipe says "delete its stub and run `sandloom generate`" |
| 22 | CLI + ERR 2 | delete the stub, `sandloom generate` | `ModuleLoadError`: `reports` imports the deleted stub (generate imports the app) |
| 23 | EDIT ×2 | move `reports/access.py` aside, strip the import and route from `reports/api.py` | workaround for ERR 2 |
| 24 | CLI + ERR 3 | `sandloom generate` | T004 again, now from `contracts.snapshot.json` |
| 25 | CLI | delete `contracts.snapshot.json`, `sandloom generate`; restore the two files; `sandloom generate` | OK; the stale DTO copy `LimitsStatus` stays in `shared/contracts/quota_dto.py` and generate reports "current" |
| 26 | DOC | `sandloom docs as_user`, `sandloom docs bind_tenant` | |
| 27 | EDIT | `reports/tests/test_reports.py`: fake `quota.limits.v1`, allow 201 / deny 402 | |
| 28 | EDIT | `quota/tests/test_quota.py`: call `Contract(QUOTA_LIMITS_V1)` under `bind_tenant` | |
| 29 | CLI + ERR 4 + ERR 5 | `sandloom test` | ERR 4: SANDLOOM-T009, `TIER_LIVE, AuthzContext, Category, Requirement` and `bind_tenant` are advanced tier and need `# sandloom: advanced <reason>`. ERR 5: the provider test's contract answered as if the owner had 0 items (`available(u, 2)` true after 2 items); cause not determined. SELF: a trailing blank line (ruff format) |
| 30 | CLI + ERR 6 | `pytest … -k limits -s` to debug | refused: "database belongs to another test run" (direct pytest and `sandloom test` have different owner ids); used `sandloom test quota --no-gates`. SELF: a debug `db()` outside a unit (R002) |
| 31 | DOC | `docs/reference/testing.md` (contract tests) | "a provider loaded in the app runs on the test session" |
| 32 | EDIT ×2 | provider test rewritten through HTTP (quota items, then the reports export: 201 at 2 items, 402 at 3); advanced-tier marker in `reports/access.py` | |
| 33 | CLI | `sandloom test` | **OK**: 12 tests, 10 gates (ruff, generated files, schema & deploy, suite, format, mypy, `sandloom check`, contract and route snapshots, migration drift). Monolith done at 11:03 |
| 34 | DOC | `sandloom docs recipe split` | |
| 35 | CLI | `sandloom add deploy quota`; `sandloom add deploy reports` | microservices profile, gateway, db init; `reports` gets `SERVICE_BINDINGS {"quota.limits.v1": …}` and `RPC_ALLOWED_CALLS {"reports": ["quota.limits.v1"]}` derived from the code, no manual wiring |
| 36 | CLI | `sandloom add deploy --check` | boot quota OK, boot reports OK, split check OK (6 s) |
| 37 | CLI | `sandloom keys dev-env --out deploy/.env.microservices` | OK |
| 38 | CLI + ERR 7 (environment) | `docker compose … build reports` | the app's lock points at the framework checkout path, outside the build context; with the PyPI framework this step would build. Not counted as a design cost |
| 39 | CLI | `sandloom migrate upgrade-all --apply` (dev db) | OK |
| 40 | EDIT (new file, off-docs) | `split_local.py.txt`: run the two services as local processes from the generated compose env + dev keys (no documented non-Docker path). Overrides: JWT HS256 (the profile expects EdDSA keys; this app has no issuer), dev DB/Redis, binding URL to localhost, accepted risk `shared_hs_secret_split`. SELF: one bug in my `${VAR:-default}` handling | |
| 41 | run | two processes, token for one user | 2 items: export **201**; 3 items: **402** `QuotaAvailable` over RPC (`POST /_sandloom/rpc/quota.limits.v1/available`; one describe `GET` on first use); provider stopped: **503** `remote_service_unavailable` |

## Totals (the baseline a new design must not exceed)

| measure | count |
|---|---|
| framework CLI commands run | 19 (`new`, `uv sync`, `generate` ×6 of which 4 only to get past ERR 1–3, `dev up`, `add contract`, `add module`, `test` ×3, `add deploy` ×2, `add deploy --check`, `keys dev-env`, `migrate`); 13 without the error recovery |
| files edited or created by hand (final app) | 7: `quota/contracts.py`, `quota/schemas.py`, `quota/services.py`, `reports/access.py` (new), `reports/api.py`, `reports/tests/test_reports.py`, `quota/tests/test_quota.py` |
| generated files deleted by hand | 2: the stub `shared/contracts/quota_limits_v1.py`, `quota/contracts.snapshot.json` |
| temporary manual workarounds | 2 edits (move the consumer aside to regenerate) |
| manual wiring for the split (bindings, allowlist) | 0: derived by `sandloom add deploy` |
| off-docs step to run the split without Docker | 1 launcher script |
| framework / doc errors met | 6 (ERR 1–6), plus 1 environment error (ERR 7) |
| doc lookups | 16 (rows 1, 2, 7, 8 ×4, 9, 10, 11, 12, 21, 26 ×2, 31, 34) |
| kernel (`src/sandloom`) changes | 0 |
| lines of the typed atom | 31 (`reports/access.py`) |

## What the run shows about today's design

- A module's typed access check is a consumer-written `Requirement` subclass over the
  provider's contract stub. It works locally and over RPC with no kernel change, and
  `sandloom add deploy` binds its contract because the atom declares `contracts()` and the
  module holds `Contract(REF)` at module level.
- Writing it needs four advanced-tier kernel names and a marker comment per import (T009);
  no doc or recipe shows how; the provider cannot ship the typed atom to its consumers
  (its package cannot be imported by them), so every consumer writes its own.
- A provider failure inside the hand-written atom surfaces as the contract proxy's 503
  (fail closed), not as an indeterminate `AccessDecision`: no `access_denied_*` metric or
  deny log, unlike the kernel's live atoms, which map an absent service to an
  indeterminate 501 (`atoms_live.py:57-73`).
- Changing a freshly generated, never-released contract costs three errors and two
  hand deletions (ERR 1–3); `sandloom explain` and the recipe disagree.
- An access fact (`AccessFact` + `Entitled`) is the only provider-owned declaration that
  travels to consumers, but it is untyped (a string) and parameterless.

Evidence: the hand-written files in `files/` (`*.py.txt`: records, not importable).
