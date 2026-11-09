# Release Notes

> Entries before Sandloom's first public alpha describe internal development of the
> predecessor project. They are implementation history, not evidence of a
> published Sandloom release. Current installation and license information is
> in README.md and LICENSE.

All notable changes to Sandloom are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/). While the major version is `0`, a
**minor** release may contain breaking changes; they are listed first. The
website renders this same changelog.

## [Unreleased]

### Security

- **One trusted view per verified token; one platform admission; per-key
  `cpv`** (KB-11, ADR-0014 decisions 3 and 4, amended 2026-11-09; edges
  B-S1, B-S2 fixed). `decode_token` reduces every verified access token
  once, cached with it, to the claims the composition's map trusts: a
  protected key counts only when its authority (current, or the one
  `previous=` the map declares) is named by the token's per-key `cpv`
  digest of `(key, assigned import path)` and ran at the mint
  (`claims_sources`, not failed). `UserContext`, `Role`, `Perm`,
  `TenantScope` and both platform gates read only the view.
  `require_platform_admin` and the platform console's operator gate share
  one admission: `roles: admin` from the authority the map assigns, never
  from a source named `identity`. An untrusted key answers 401
  `token_policy_outdated` where one refresh fixes it, 503
  `claims_policy_mismatch` where issuer and verifier run different maps
  (`Or` picks 401 > 503 > 501). A remote `<m>.claims.v1` answer now names
  the module serving it; the issuer refuses one naming another module, or
  none, for an authority of its map, so the pinned `service_id` proves the
  service and the answer proves the module. New: `Authority(name,
  module=, previous=)` and `LegacyClaimsTokens(until=)` in
  `sandloom.sdk.app`, `create_app(legacy_tokens=...)`,
  `UserContext.untrusted_claims`. **Upgrade:** (1) upgrade every service
  providing a `<m>.claims.v1` before the token issuer; (2) tokens minted
  before this release carry no `cpv`: without a compatibility window every
  client refreshes once (401 `token_policy_outdated`); to roll out without
  that, pass `legacy_tokens=LegacyClaimsTokens(until="<UTC time the last
  old issuer stops>")` to `create_app` in every service of the profile,
  then remove it once `until + ACCESS_TOKEN_EXPIRE_MINUTES +
  JWT_LEEWAY_SECONDS` has passed (accepted risk G-RISK-14; it ends by
  itself, is never extended, and is counted in
  `claims_policy_legacy_tokens_total`); (3) a test minting a raw token with
  `create_access_token` stamps the kernel test authority (`cpv` and
  `claims_sources` of `sandloom.core.auth.claims_view.TEST_CPV` /
  `TEST_AUTHORITY`) or uses `as_user` / `make_user_headers`, which do;
  (4) to move a key to another authority, deploy
  `Authority("<path>", previous=Authority("<name>", module="<old path>"))`
  first (ADR-0014 decision 4).
- **A consumer-only install is checked from its own expectations; no
  expectation is never a warning** (KB-21, ADR-0013 decisions 3 and 4,
  closes D4). The startup compatibility check now reads what a caller
  expects of each remote contract from the `<stem>.describe.json` its
  module ships beside the generated stub (`_generated/contracts/`, narrowed
  to the methods its composed `consumes` lists), then
  `CONTRACT_SNAPSHOT_PATHS`, then the provider's package; `<m>.claims.v1`
  and `<m>.facts.v1` are held to the kernel's own schema. A required remote
  contract with no expectation (or with a hand-edited one) used to be
  logged as a WARNING and skipped, also under `CONTRACT_COMPAT_CHECK=strict`;
  now `strict` refuses startup before any probe
  (`ContractUnverifiedError`) and `ready` fails `/ready` with
  `contract_unverified`. The expectations are resolved once at startup:
  `/ready` reads no file, and a contract call sends the same requests as
  before. **Upgrade:** a service whose `SERVICE_BINDINGS` names a contract
  it has no expectation for (no consumer copy, no snapshot in its image,
  no `CONTRACT_SNAPSHOT_PATHS` entry) now fails `/ready`: run
  `sandloom generate` in the consuming module, ship the provider's
  snapshot, or declare the client `optional=True`.
- **A token issuer without claim authorities refuses the boot; the issuer's
  fields pass the mint check; plan atoms answer 503 while billing is down**
  (W1 review of KB-10). A composition that loads an `issues_tokens=True`
  module with no `claim_authorities` map (the lean `sandloom.main` with
  identity installed) now refuses at startup (`SANDLOOM-B035`) instead of
  running an app whose every login is refused (503); a process with no
  issuer still boots. `email` and `email_verified` are the map's issuer's:
  a map that assigns them to another module (or names them without an
  issuer) refuses the boot, a map with an issuer that names neither assigns
  them to it, and identity writes them through the mint check
  (`collect_claims(issuer_claims=...)`, new on `CoreFacade` and
  `CoreRuntime`), never after it. `Feature` and `WithinLimit` on the user's
  token answer indeterminate 503 when the claim's authority failed at the
  mint (`claims_sources_failed`), so `Not` cannot invert it; a billing that
  ran and granted nothing still denies. The resolved
  `ClaimAuthorities` mappings are read-only.
  **Upgrade:** run every process from a root with a map. In a framework
  checkout or worktree without a `.env`, export
  `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main` (`.env.example` sets
  it), or `sandloom generate`, `sandloom migrate`, the gates of
  `sandloom test` and the other commands that build the app refuse with
  `SANDLOOM-B035` (the pytest suites pick the standard root in the root
  `conftest.py`); run the API
  as `uvicorn sandloom_standard.main:app`. An application root composing an
  issuer passes `create_app(claim_authorities={...})` (`sandloom new` writes
  it). A map entry giving `email`/`email_verified` to a module other than the
  issuer is removed or given to the issuer.
- **Claim authorities: the composition root says who may write each
  protected token claim** (ADR-0014 decisions 1, 2, 7; KB-10). New
  `create_app(claim_authorities={key: module, ..., "issuer": module},
  default_claim_authorities=...)`: each protected claim (`roles`,
  `permissions`, `tenant_ids`, `email`, `email_verified`,
  `subscription_status`, `plan_code`, `features`, `limits` and every key the
  map names) is kept at the mint only from its authority, the module loaded
  from the assigned import path or its `<m>.claims.v1` binding; a module
  merely named like it is not it. A contributor writing a protected claim it
  is not assigned, or a reserved key (now also `cpv`), is a violator: an
  optional one loses its whole result (`claims_sources_failed`, an ERROR log,
  the counter `claims_contributor_violations_total`) and the login succeeds
  with the authorities' values; a critical one refuses the mint (503) naming
  itself. This replaces the "any foreign key refuses every mint with 503"
  rule, so one buggy optional module no longer denies every login. Startup
  refuses an assignment the composition cannot honour (`SANDLOOM-B035`, which
  now covers both of the issuer's startup checks): in the issuing process an
  authority neither loaded nor bound (a typo is named as one), loaded from
  another path, loaded without a contributor, or non-critical for a claim
  other than the four billing claims; an `issues_tokens=True` module that is
  not the map's `issuer`, or the reverse. Billing stays non-critical, so a
  billing outage still mints a login (its claims absent, the subscription
  check escalating to the live fact). The kernel holds no default map:
  `sandloom_standard.main` passes the standard one as
  `default_claim_authorities`, kept only for the batteries in the
  composition (loaded, or bound through `<m>.claims.v1`); `sandloom new`
  writes the app's map (`CLAIM_AUTHORITIES`) into `<app>/asgi.py`.
  `CoreFacade.claim_authorities` (new, read-only) is the resolved assignment;
  it is made once, by `create_app`.
  **Upgrade:** a root that composes identity (or any issuer whose critical
  contributor writes `roles`) must assign the claims, or every mint refuses
  with 503 and startup logs that no map is set: run `sandloom_standard.main`
  (`SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`, the shipped profiles
  already do), or add to your own `asgi.py`
  `create_app(..., claim_authorities={"issuer": "sandloom_identity", "roles":
  "sandloom_identity", "permissions": "sandloom_identity", "email":
  "sandloom_identity", "email_verified": "sandloom_identity", "tenant_ids":
  "sandloom_tenancy", "subscription_status": "sandloom_billing", "plan_code":
  "sandloom_billing", "features": "sandloom_billing", "limits":
  "sandloom_billing"})`, keeping only the modules you compose. A custom
  module that writes one of these claims is assigned it, or stops writing it;
  a binding of a rights authority must not set `"critical": false`.
- **The kernel applies durable token revocations in every service; the token
  issuer must end its sessions on a block** (ADR-0012 decision 5). The durable
  consumer of `core.auth.tokens_revoked.v1` moved from identity into the
  kernel: every composition registers it as `auth@tokens_revoked` in its own
  group, `sandloom:auth@tokens_revoked@<SANDLOOM_SERVICE_NAME>`, so a mark
  that failed after commit is re-applied in every service whether identity
  is loaded there or not (identity's `sessions@tokens_revoked` is gone).
  There is no fallback to the database identity: on a shared event bus
  (`EVENT_BUS_BACKEND=streams|redis`, or `auto` in prod) a process without
  `SANDLOOM_SERVICE_NAME` refuses to start in every entry point (app, worker,
  `sandloom migrate`, the configured CLI commands) with
  `INVALID_CONFIGURATION`, in every environment. The in-process memory bus
  needs no name (group `…@local`). `/ready` now also fails, naming the
  consumer, when the applier is needed and missing or not running, when the
  oldest pending `core.auth.tokens_revoked.v1` / `core.auth.user_blocked.v1`
  event is older than `ACCESS_TOKEN_EXPIRE_MINUTES`, or when one is
  dead-lettered; a younger backlog stays ready, exported as
  `sandloom_consumer_lag_seconds` and logged at WARNING above half the bound.
  A module declared `issues_tokens=True` must register a durable consumer of
  `core.auth.user_blocked.v1`, or declare
  `define_module(..., refresh_sessions=False)` (new, recorded in
  `module.snapshot.json`, shown by `sandloom doctor` and
  `/diagnostics/core`); startup refuses an issuer with neither, and a
  declaration contradicted by a refresh route, a session table or such a
  consumer (`SANDLOOM-B035`). **Upgrade:** set `SANDLOOM_SERVICE_NAME` in
  every process on a shared bus — the same value for all replicas of one
  service, a different one for every other service (the shipped compose
  profiles and `sandloom add deploy` already do; T014 refuses two services
  of one profile with one name). A custom token issuer without a block
  consumer adds one or declares `refresh_sessions=False`. Drop any operator
  pin of the old `identity:sessions@tokens_revoked` group; its pending
  entries are re-applied by the new group from the stream (idempotent).
  Once every service runs this version, delete that group, which nothing
  reads any more:
  `redis-cli XGROUP DESTROY <EVENT_STREAM_PREFIX>core.auth.tokens_revoked.v1 identity:sessions@tokens_revoked`,
  then `sandloom streams release-group core.auth.tokens_revoked.v1
  identity:sessions@tokens_revoked` (operator reference, *The retired
  identity group*).
- **`SANDLOOM_SERVICE_NAME` now names a consumer group.** It used to be a
  label only (consumer ids, the subscription inventory, diagnostics). It now
  also names the kernel's revocation applier group,
  `sandloom:auth@tokens_revoked@<SANDLOOM_SERVICE_NAME>`. **Upgrade:** two
  services that shared one label now share one applier group, and each
  misses part of the other's revocations. Give every service its own name,
  the same for all its replicas. Within one profile SANDLOOM-T014 refuses a
  shared name; across profiles nothing detects it at runtime (a recorded
  gap of G-REVOKE-26).
- **A shared event bus selected through `INFRA` now needs
  `SANDLOOM_SERVICE_NAME` too.** The applier decided "shared bus" from
  `EVENT_BUS_BACKEND` alone. With `INFRA={"eventbus": {"default": {"driver":
  "streams"}}}` and `EVENT_BUS_BACKEND=auto` outside production, unnamed
  services shared the group `…@local`, split revocations, and `/ready` stayed
  green. The decision now follows the resolved driver of the `default`
  eventbus instance (`INFRA` applied); every driver but `memory` is shared,
  and an `INFRA` eventbus that cannot be merged counts as shared.
  **Upgrade:** a process that selects Streams or Pub/Sub through `INFRA`
  sets `SANDLOOM_SERVICE_NAME`, or it refuses to start.
- **`/ready` counts revocation dead letters fail-closed.** A failed
  dead-letter read made `/ready` report zero, and only the oldest 100 DLQ
  entries of all groups were counted. Now the whole DLQ is counted for this
  service's groups, paged, up to 10,000 entries. A failed read, or a longer
  queue, is an unknown count, and an unknown count is not ready. The read
  runs at most once per `HEALTH_CACHE_TTL_SECONDS` however many probes
  arrive, and `sandloom_consumer_lag_seconds` returns to 0 once the backlog
  clears.
- **Identity's and billing's production refusals stop every process, not only
  the API.** identity without `TWOFA_ENCRYPTION_KEY` in prod, identity without
  `JWT_SECRET` (any environment; issuer-only with an asymmetric `JWT_ALGORITHM`),
  and billing on the implicit fake PSP in prod were refused only by critical startup tasks in the
  ASGI lifespan: the Celery worker, beat, `sandloom migrate` and the operator
  commands (`outbox`, `streams`, `projection`, `sagas`, `backup`, …) built and
  ran. The modules now refuse while they register, and the loader turns a
  module's `SystemExit(reason)` from `register` / `setup` into the kernel's
  `[core.config] Invalid configuration: module <name>: <reason>` (exit status
  1), in every entry point that builds the composition; an optional or
  aggregated module load cannot carry on past it. `sandloom config dump` still
  builds no composition. **Upgrade:** a deployment that ran its worker, beat or
  migrate one-shot without these settings must give them the same module
  environment as the app (the shipped compose profiles already do).
  `ConfigurationError` now lives in `sandloom.core.config.common` (still
  importable from `sandloom.core.app_factory`).
- **A module registers claims, facts and their contracts only under its own
  name** (`SANDLOOM-B034`). An access fact whose `owner_module` is another
  module, a claims contributor given `owner=` naming another module, and
  another module's `<m>.claims.v1` or `<m>.facts.v1` contract now refuse the
  module's load and the boot. So does any of them registered outside a
  module's registration (a direct call on the core from a startup task or the
  composition root). Before, the kernel took the name as given: a module could
  answer another module's facts, become the owner of `identity.claims.v1`, or
  be stamped `identity` in `claims_sources`. Over RPC, a consumer drops a fact
  that `<m>.facts.v1` describes under another `owner_module` and logs
  `SANDLOOM-B034`. **Upgrade:** give each `AccessFact` the registering
  module's short name as `owner_module`; drop `owner=` from
  `add_claims_contributor`; register facts, contributors and these contracts
  from the module's `register()` or `setup=`, never on the core directly.
  Tests that register on a bare `CoreFacade` wrap the calls in
  `with core.module_registration("<module>") as api:` and call `api`.

- **A token claim has one owner, and no claims contributor writes another's
  claim or a reserved key.** A second module writing a key an earlier module
  wrote (identity's `roles`, say), or any module writing a key the kernel
  stamps or reads as the subject's structure (`claims_sources`,
  `claims_sources_failed`, `sub`, `user_id`, `provenance_authoritative`,
  `claims`, `tenant_id`, `org_id`, `iss`, `aud`, `exp`, `nbf`, `iat`, `jti`,
  `iat_us`, `auth_time`, `token_type`), aborts minting with `503`, local or remote. Before, the last
  writer won: a module could put `roles: ["admin"]` in a token stamped with
  identity and pass the platform administrator gate, override `user_id` and
  revocation fields, or write `tenant_id` and bind a foreign tenant. **Upgrade:** a contributor that returned one of these keys
  must drop it; return values as top-level claims your module owns instead of a
  nested `claims` mapping. A nested `sub`, `roles` or `permissions` cannot move
  up unchanged (reserved, or identity's): rename it and update its readers.
- **The log redactor masks every setting `sandloom config dump` masks.**
  `TWOFA_ENCRYPTION_KEY`, `SENTRY_DSN` and `S3_ACCESS_KEY_ID` passed the log
  redactor (a log extra or a logged mapping key) in clear while `config dump`
  redacted them. Both now judge a name by one rule
  (`guardrails.is_secret_name`), and the log redactor keeps its broader
  substring markers on top. Lookup keys such as `operation_key`,
  `idempotency_key` and `dedupe_key` stay readable in logs and audit data.
- **The admin console serves in front of the composition's assigned issuer,
  not a module named `identity`** (KB-41, ADR-0012 decision 4, owner's X4).
  `/admin/config.json` answered 503 unless the manifest listed a module
  `identity`. It now reads the claim-authorities map's `issuer` and the login
  entry that issuer declared on its registration, both written into
  `admin-manifest.json` by `sandloom generate` (new `issuer` field:
  `{"module", "login_entry"}`, `null` without an assigned issuer), and
  serves `issuer` and `login_entry` in `config.json`. No issuer, or an issuer
  that declares no login entry, is a 503 naming the fix (the console never
  signs in through a guess); a login entry that is not a route path of the
  application (a URL, `//host`, `..`, a query) is refused at registration,
  at `sandloom generate` (the issuer's own package must serve it with `POST`)
  and again by the console. The issuer is never taken from the `iss` of a
  token, which is unverified input. Upgrade: declare
  `define_module(..., issues_tokens=True, login_entry="/auth/login")` on your
  token issuer (the bundled identity module does), then run
  `sandloom generate` (or `sandloom admin export`) and restart. New public
  API: `define_module(login_entry=)`, `ModuleDescriptor.login_entry` and
  `sandloom.sdk.module.GenerationIssuer` (`GenerationInput.issuer`).
  The console UI still requires the manifest to list `identity`
  (a follow-up).

### Added

- **Modules declare their own settings** (ADR-0015, KB-50).
  `define_module(..., settings=InventorySettings)` (or
  `api.declare_settings(InventorySettings)` in a hand-written `register`)
  takes a plain pydantic `BaseModel` whose fields are aliased to their
  environment names (`Field(alias="INVENTORY_RESERVE_TTL_SECONDS")`, secrets
  as `SecretStr`); metadata are `ClassVar` class attributes holding
  `SemanticSetting`, `Risk` and `RemovedSetting`, and production checks are
  `model_validator`s reading `info.context` (`environment`, `accepts_risk`,
  a read-only `kernel`, `module`). The model is validated from the process's
  one read of the environment before anything else of the module registers
  and before `setup`; an invalid value stops every entry point that builds
  the composition with `INVALID_CONFIGURATION` and exit 1, an optional
  module included (`ModuleSettingsError`, a `ConfigurationError`). After the
  load pass one check refuses an environment name declared twice by the
  kernel and a loaded module or by two loaded modules (`SANDLOOM-B036`), a
  loaded module's removed setting at a live value, and in prod a near-miss of
  any declared name; it logs a module's semantic setting weaker than its
  profile value (`CONFIG OVERRIDE`, `sandloom_config_override_active`). Read
  the values with `settings_of(InventorySettings)` inside a request, a task
  or `setup`: a read at import raises, and the import audit fails it. New
  advanced names: `settings_of`, `SemanticSetting`, `RemovedSetting`, `Risk`
  (`sandloom.sdk.module`); `override_settings`, `validate_settings`,
  `ModuleSettingsError` (`sandloom.sdk.testing`). The bundled modules keep
  their settings in the kernel until their own ports (KB-52..KB-54), and a
  module's risk id `<module>.<name>` is accepted by `SANDLOOM_ACCEPTED_RISKS`
  once namespaced risks land (KB-51). **Upgrade:** nothing to change for an
  existing module. A module that read the environment itself
  (`os.environ`, its own `BaseSettings`) moves the fields onto a declared
  model and reads them with `settings_of`; tests replace `setattr` on the
  kernel settings with `with override_settings(Model, field=value):`.
- **One source read per process.** The kernel `Settings` load now reads the
  environment, `.env` (parsed once) and `SECRETS_DIR` through the same
  snapshot the modules validate from, with the same precedence (environment >
  `.env` > `SECRETS_DIR`) and decoding. `SECRETS_DIR` is no longer listed: a
  file is looked up by the declared name in lower or upper case (`jwt_secret`
  or `JWT_SECRET`), once per name. **Upgrade:** on a case-sensitive file
  system, rename a secret file whose name mixes cases (`Jwt_Secret`) to the
  lower- or upper-case name; nothing else changes.
- **Accepted risks of a module are namespaced** (ADR-0015 decision 4, KB-51).
  Kernel ids stay flat (`relay_disabled`); a module's id is
  `<module>.<name>`, declared in its settings model (`Risk`) and accepted in
  `SANDLOOM_ACCEPTED_RISKS` by that name (`kb_inventory.oversell_in_prod`).
  After the modules load: the id of a loaded module is checked against what
  it declares, and one it does not declare refuses the boot; the id of a
  module installed, loaded or listed in `SANDLOOM_MODULES` but not loaded in
  this process is inert and listed as "not applicable here" (so one
  `SANDLOOM_ACCEPTED_RISKS` serves every service of a split with one image);
  any other module name refuses, so a typo in a module or risk name never
  passes silently. A flat id that is no kernel id is refused as before, and
  names the namespaced replacement when the module that declares it is
  installed. The accepted module risks are logged, counted in
  `sandloom_accepted_risk_active{risk}` and reported by `tiers.diagnostics`.
  There is no alias. **Upgrade:** none yet for the bundled modules: billing's
  `fake_psp_in_prod` becomes `billing.fake_psp_in_prod` in one step with the
  port of billing's settings (KB-52), whose notes carry the rename; a module
  of your own that declares risks is accepted by `<module>.<name>`.
- **`sandloom check` and `sandloom test` check the declared settings**
  (ADR-0015 decision 7, KB-56; `SANDLOOM-B036`, new `SANDLOOM-T019`).
  They import every module of the application and flag a name declared by
  two modules (or a module and the kernel) whether or not they load
  together, a new setting without the module's prefix (`INVENTORY_` for
  `inventory`), a tier that `module.snapshot.json` does not record, and, per
  service of the deploy profiles, a wrong module name in `SANDLOOM_MODULES`,
  a variable set for a module the service does not load and a literal value
  the declaration refuses. `sandloom generate` writes the new `settings`
  record of `module.snapshot.json` (each name and its tier); names an existing
  module already had when it first declared settings are recorded as
  `legacy` and keep passing, a new module's unprefixed names are not
  grandfathered. `sandloom.core.testing.settings_gate.semantic_ratchet_problems`
  is a module's own semantic ratchet for its tests. **Upgrade:** a module
  that declares settings runs `sandloom generate` once and commits the
  snapshot; a new setting is named `<MODULE>_...`.

### Changed

- **Identity, tenancy and the admin console declare their own settings**
  (KB-53, KB-54; ADR-0015). `TWOFA_ENCRYPTION_KEY`, the SSO client ids and
  secrets, `TELEGRAM_BOT_TOKEN`, `SSO_ALLOWED_REDIRECT_URIS`,
  `REFRESH_TOKEN_EXPIRE_DAYS` and `MAX_SESSION_DAYS` belong to
  `sandloom_identity` (`IdentitySettings`); `TENANT_CLAIMS_MAX_ORGANIZATIONS`
  to `sandloom_tenancy` (`TenancySettings`); `ADMIN_CONSOLE_ENABLED` and
  `ADMIN_CONSOLE_DIST` to `sandloom_admin` (`AdminSettings`). Environment names,
  defaults and bounds are unchanged. The kernel `Settings` no longer has these
  fields, so `settings.twofa_encryption_key` and the others are gone (no alias):
  a module reads its own with `settings_of(Model)`. A weak or missing
  `TWOFA_ENCRYPTION_KEY` in production is now identity's own settings check, run
  before its `setup` in every entry point: the message is unchanged apart from
  the `module sandloom_identity:` prefix. The secrets are `SecretStr`. A process
  that does not load identity (or tenancy, or the console) no longer parses or
  validates its variables. New advanced name `sandloom.sdk.security.secret_problem`
  (the strength rule `JWT_SECRET` uses) for a module's production check.
  **Upgrade:** a module of your own that read one of these kernel attributes
  reads it through the owning module's settings model or its own environment;
  in a `compose/microservices.yml` run `sandloom add deploy --refresh`:
  `REFRESH_TOKEN_EXPIRE_DAYS` moves from the shared env to the identity
  service's, and a service that loads no identity must not set it
  (`SANDLOOM-T019`). `sandloom admin create` / `invite` still choose the console
  or the frontend link from `ADMIN_CONSOLE_ENABLED`, read from the process's
  environment. The generated configuration reference lists the module settings;
  the ops tier count falls by the 16 names that moved.
  The production typo check of module settings also counts every loaded module,
  one that declares no settings included, as the owner of its variable prefix.

- **Deploy profiles and `sandloom new` / `sandloom add deploy` templates say how
  required mail is sent** (kb-ic-6). Identity and tenancy require an email
  notifier, so a prod stack on the default `log` driver already refuses to
  start. `deploy/compose/monolith.yml`, `deploy/compose/microservices.yml`, the
  microservices template and the compose file of an app with identity or
  tenancy now document `NOTIFIER_BACKEND=smtp` with `SMTP_HOST` / `SMTP_FROM`
  (an app's compose passes the `SMTP_*` settings through) and name the demo
  choice: a stack without a mail server adds `log_notifier_in_prod` to
  `SANDLOOM_ACCEPTED_RISKS`; no mail is delivered. Upgrade: a production stack
  sets `NOTIFIER_BACKEND=smtp` and `SMTP_HOST`; a demo stack adds
  `log_notifier_in_prod` (CI's microservices smoke does).
- **Readiness and the taskqueue: transient trouble only, keyed on the
  requirement** (owner decision on module infrastructure dependencies,
  answers 3 and 4; KB-IC-3). `/ready`'s taskqueue term now follows each
  loaded module's REQUIRED taskqueue requirement (declared or derived), not
  the presence of a `tasks.py`. A module that uses the queue while serving
  (no feature named, or `delay` / `idempotency_key`) makes `/ready` 503 when
  its broker cannot be reached, naming the instance. A module that needs only
  `periodic` keeps the web process ready during a broker outage: the worker
  and beat run that work. The internal block lists such modules under
  `background_modules`. An optional requirement is never probed. The internal
  `taskqueue` block now holds per-instance results under `instances`, and
  `instance` / `details` are gone from its top level. The `celery` driver's
  `health()` now connects to the broker once per probe, bounded by
  `HEALTH_CHECK_TIMEOUT_SECONDS` and with no retry; before, it always answered
  `ok`. `/ready` caches it with the other probes, so the broker gets one
  connection per TTL however many hits arrive. **Upgrade:** a deployment whose
  `/ready` was 503 only because Celery was missing is now refused at startup
  (kb-ic-1). A monitor reading `taskqueue.status` or `taskqueue.details` reads
  `taskqueue.instances.<name>` instead.
- **Billing watches that its intent reconciler runs** (KB-IC-3). A reachable
  broker is not enough. Every completed run of `reconcile_operation_intents`
  records its end in the new table `billing_reconciler_runs` (migration
  `0012_billing`). The `billing_reconciler` health check fails when no run
  completed within 15 minutes (three periods): `never_ran` means no worker or
  no beat, and `overdue` means one of them, or the broker, stopped, or every
  run fails. The check is part of `/health` and `/ready` in every process that
  loads billing, after a 15-minute grace from process start in which it reads
  nothing. **Upgrade:** run `sandloom migrate upgrade-all --apply`, and run a
  worker and one beat wherever billing runs; in development without them,
  `/ready` turns 503 on this check after 15 minutes.
- **`sandloom doctor` has a `periodic` section, and `/diagnostics/core` a
  `periodic` block** (KB-IC-3). For each loaded module whose taskqueue
  requirement asks for `periodic`, they show whether a beat holds the lease
  for this module set. If none does, a required need fails and an optional
  one warns: identity's token cleanup is optional, and without it
  `refresh_tokens` grows without bound (documented on the identity page).
- **Modules declare the infrastructure their code needs; startup checks it,
  also where it is only derived** (owner decision on module infrastructure
  dependencies, item 2; kb-ic-2). The `taskqueue` capability has a new
  feature `periodic`: the driver runs a module's periodic jobs at their
  interval while the deployment runs (only `celery` advertises it; a driver
  never gets it for supporting enqueue). `ModuleDescriptor.effective_capabilities()`
  now adds what a module's code shows it needs, read without importing it: a
  `tasks.py` needs `taskqueue`, its `setup_periodic_tasks` the feature
  `periodic`. A declaration keeps its `required` and `instance` and only
  gains the derived feature; a capability nothing declares is derived as
  required. New `sandloom check` rule `undeclared-capability`
  (SANDLOOM-C033) asks for the declaration, `sandloom add task` writes it,
  and `module.snapshot.json` records the effective requirements
  (`capabilities`). Billing declares `taskqueue` with `periodic` (its intent
  reconciler must run); identity declares it optional (its token cleanup is
  storage hygiene). Identity and tenancy now require `notification` with
  `email`, so **a production deployment on the default `log` notifier refuses
  them at startup** instead of logging verification, reset and invitation
  mail nobody receives. Upgrade: configure a delivering notifier
  (`NOTIFIER_BACKEND=smtp` and `SMTP_*`, or a driver package), or accept the
  risk for a demo stack with `SANDLOOM_ACCEPTED_RISKS=log_notifier_in_prod`;
  run `uv run sandloom generate`; declare `taskqueue` in each module with a
  `tasks.py` (`sandloom check` names them). A module selected in
  `SANDLOOM_MODULES` whose required capability is unmet now refuses startup
  even when it is optional; one only discovered or defaulted is still skipped.

- **The admin console's manifest is a file the console declares** (ADR-0012
  decision 4). A module can declare files `sandloom generate` writes into the
  app's shared package, `define_module(..., generates={"name": render})`:
  `render(source) -> str` receives the composed app as data
  (`sandloom.sdk.module.GenerationInput`: OpenAPI, routes, each loaded
  module's `admin.py` resources), never a module, and `sandloom test` fails on
  a stale file (SANDLOOM-T016, also for a name two modules declare). The
  console declares `admin-manifest.json` this way: the kernel no longer gates
  generation on the name `sandloom_admin` and no longer names the file, and
  the manifest compiler and the resource schema moved from
  `sandloom.core.admin` to `sandloom_admin.compiler` / `sandloom_admin.models`
  (`sandloom.core.admin` keeps the platform operations API). The manifest's
  bytes are unchanged. `sandloom admin export` renders the file the composed
  console declares: its build-time composition must include `sandloom_admin`
  (it fails, saying so, otherwise). New advanced names:
  `GenerationInput`, `GenerationModule`, `ModuleDescriptor.generated_files`.
  **Upgrade:** a build that runs `sandloom admin export` with a composition
  that leaves the console out (an explicit `SANDLOOM_MODULES` subset, or a
  composition root that does not compose `sandloom_admin`) adds
  `sandloom_admin` to the composition of that build step; the running app's
  composition does not change. SANDLOOM-T016's slug is now
  `declared-generated-file` (it was `admin-manifest`): `sandloom explain
  SANDLOOM-T016` and the code's docs anchor are unchanged.
- **A module distribution holds copies of the contracts it consumes**
  (ADR-0013, KB-20). In a tree of top-level module packages (the framework,
  an ecosystem module repository), `sandloom generate` writes
  `<module>/_generated/contracts/`: the stub, the DTO copies and
  `<stem>.describe.json`, the provider's describe restricted to the methods
  the module's `consumes` lists. The set is planned from the declarations
  (module-level `Contract(REF)`, `define_module(consumes=[...])`) before the
  app is composed, so a module may import a copy it has not generated yet;
  `generate --check` fails when it differs from the composed `consumes`, and
  an entry with no method list (a call through `core.get_service`,
  `getattr` or a helper) is refused with SANDLOOM-T018 until
  `ContractRequirement(name, methods=(...))` states it. `c = ref.client()`
  in a function, tested against `None` and called, now resolves its methods.
  Every generated stub and DTO copy starts with a provenance header
  (`contract`, `fingerprint`, `provider`, `generator: format 2  digest …`);
  DTO copies are laid out by the generator at 88 columns and never piped
  through the project's ruff, so ruff's presence or `line-length` no longer
  changes them. A provider not importable where `generate` runs keeps a
  committed copy whose digest verifies and refuses an edited or missing one
  (SANDLOOM-T018). Two contracts with one stub stem are refused in every
  path. An application's shared package keeps copies only for the union of
  its modules' `consumes` when every entry lists its methods (no example
  changes). A field added to a closed (`extra="forbid"`) result DTO is
  breaking against an expectation. Bundled stubs stay in `sandloom.shared`
  until KB-22. Upgrade: run `uv run sandloom generate` (headers change; no
  body changes).
- **A module's `_generated/` is the generator's, end to end** (ADR-0013
  decisions 1, 2, 5, 6; W1 review of KB-20). `sandloom check` refuses a file in
  `<module>/_generated/` that `sandloom generate` did not write or that was
  edited since, and an import of another module's `_generated` package
  (static, relative or a constant dynamic import; new rule
  `generated-reserved`, SANDLOOM-C032). Each `<stem>.describe.json` carries
  the `digest` of its content (expectation `format` 2), so with the provider
  not importable an edited expectation is refused like an edited stub
  (SANDLOOM-T018). `sandloom generate` removes the copies of a contract the
  module no longer consumes (`--check` reports them; an edited orphan is
  refused). A module-level `x = Contract(REF)` that no file of the module
  names again is refused naming the client (it consumed the contract for
  nothing), and methods stated in `define_module(consumes=[...])` now cover
  a call the derivation cannot resolve instead of being dropped. The
  framework's `pyproject.toml` and the one `sandloom new` writes exclude
  `_generated` from ruff. **Upgrade:** run `uv run sandloom generate` (each
  committed `.describe.json` gains its `digest`); in an existing app, add
  `"**/_generated"` to `[tool.ruff] extend-exclude`.
- **`sandloom admin create` leaves the password rules to the provider.** The
  kernel CLI no longer imports `sandloom_identity.password_policy` and has no
  fallback 8–100 length rule of its own: identity's `password_problem` answer
  over `identity.security.v1`, already asked at every attempt, is the only
  check. The prompt no longer states a minimum length; a refused password
  still gets identity's one-line reason. Command names are unchanged.
- **The redundant-index check (SANDLOOM-T012) names no module's index.** The
  kernel's list of known redundant indexes is gone (with
  `sandloom.core.testing.static_checks.KNOWN_REDUNDANT`): a module keeps such an
  index on purpose with `info={"sandloom_known_redundant": "<reason>"}` on the
  `Index`, and holds the remove-only ratchet over it in its own tests. Identity
  marks `ix_external_identities_provider` this way. The schema is unchanged.
- **The standard composition root moved into a new distribution,
  `sandloom-standard`** (ADR-0012 decision 3). `sandloom_standard.main` exposes
  `app` and `core`: the kernel factory with every installed module, the same
  composition `sandloom.main` built so far. `sandloom[standard]` installs it.
  The Sandloom image (`deploy/docker/Dockerfile`: `ENV
  SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`, `CMD ...
  sandloom_standard.main:app`), both shipped compose profiles and the
  framework's `sandloom add deploy` name it, so the API, the Celery worker and
  beat, `sandloom migrate` and the operator commands build one composition.
  `sandloom.main` stays in `sandloom` as the lean kernel root and the default
  when no root is configured; its behaviour is unchanged. Both roots take
  their default module set from `sandloom.local_modules.default_modules()`, so
  a framework checkout's `LOCAL_MODULES` (moved from `src/sandloom/main.py` to
  `src/sandloom/local_modules.py`, where `sandloom add module` now writes) is
  composed under either root. SANDLOOM-T014 now
  refuses a microservices profile whose processes name different
  `SANDLOOM_COMPOSITION_ROOT` values. The publishing tools release six
  distributions. **Upgrade:** `pip install "sandloom[standard]"` keeps working
  and pulls `sandloom-standard`. A deployment built on the Sandloom image or on
  the shipped profiles gets the root with the new image; a deployment with its
  own compose files, manifests or process commands sets
  `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main` on every process (API,
  worker, beat, migrate, operator commands) and serves
  `sandloom_standard.main:app`, or keeps `sandloom.main` everywhere. Do not mix
  roots in one deployment. Applications made with `sandloom new` keep their
  `<app>.asgi` root and need no change.
- **`unscoped()` takes an optional `reason=` and is never silent.** Every
  scope that disables tenant enforcement is counted in
  `sandloom_tenant_unscoped_total{caller}` by the module that entered it; the
  reason goes to the `sandloom.tenant.unscoped` log at DEBUG.
- **`set_active_core` binds the whole runtime.** The core, its infrastructure
  registry and its event bus become the process defaults in one call, so the
  three accessors never point at different applications.
- **`undigested` and `dto_copy` are public in `sandloom.sdk.contracts`**
  (advanced; `dto_copy` is written by `sandloom generate` into the DTO
  copies), and the
  bundled billing, identity and tenancy modules import nothing from
  `sandloom.core`: their contract methods and claims contributors read the
  provider's session through `db()`, which inside a contract call is the
  contract's own unit.
- **`sandloom check: OK` says what it covers:** static rules only; `sandloom
  test` also boots the app and runs the tests.
- **The kernel's module edges are one checked register (SANDLOOM-T017).**
  Every place where shipped kernel code or the `sandloom` wheel names a module
  (a module name, package, non-`core` channel, module setting or claim key, a
  computed dynamic import outside the listed loader call sites, a generated
  module file) is a row of `tests/architecture/kernel_edges.yml` with a current
  status and a target (ADR-0012). `sandloom test` fails on an unrecorded edge,
  on a `fixed` row whose edge is back, and on an `open` row whose edge is gone;
  `uv run python tests/architecture/kernel_edges.py --report` prints the
  counts. The scan also reads static imports (`from sandloom_billing import
  x`), and a `fixed` row's anchor literal fails as that row's regression in
  any kernel file, not only the one it was removed from. The register
  replaces `kernel_module_imports_allowlist.json` and the literal scan of
  `test_module_names.py`. Framework contributors only: nothing changes for
  applications.

### Fixed

- **A registered driver whose package is missing no longer passes the
  preflight; SANDLOOM-B037 names what is missing** (KB-IC-1). The built-in
  `celery` taskqueue and `redis` cache drivers are registered whether or not
  Celery or redis-py is installed, and `validate_requirement` read only their
  metadata, so a module requiring `taskqueue` or `cache` loaded on plain
  `sandloom` and failed later. A driver registration now lists the packages it
  needs (`register_driver(..., requires=("celery",), install="pip install
  'sandloom[celery]'")`; `redis` and `streams` event buses need `redis`), and
  the preflight checks them with `importlib.util.find_spec`: nothing imported,
  no connection. A module whose required capabilities cannot be met is refused
  with one new boot code, SANDLOOM-B037 (`capability-unavailable`), listing
  every unmet requirement with the module, capability, instance, driver,
  features, the missing feature or package, and the fix (the install hint, or
  another driver in `INFRA`). An instance `INFRA` selects on an uninstalled
  driver refuses startup with the same code, and so does a dependency that
  fails to import while a driver is built (strict, production startup).
  A synthesized `default` instance nothing requires is no longer built,
  opened or probed: an app without task modules starts in production without
  Celery, `/diagnostics/infra` lists it with `status: "unused"`, and `/health`
  skips an unused object store. The instances the kernel itself uses
  (database, the revocation/security cache, the event bus) stay required.
- **`sandloom-billing` and `sandloom-identity` boot on plain `sandloom`, without
  Celery.** `sandloom.sdk.tasks` imported Celery at import, so a module whose
  `tasks.py` uses the typed `shared_task` failed to load (`ModuleLoadError ...
  No module named 'celery'`) unless the optional `sandloom[celery]` extra was
  installed. Celery is now imported only where it is used: with Celery
  installed the decorator is Celery's `shared_task` as before (worker and beat
  unchanged); without it the task still runs inline and `delay` /
  `apply_async` / `s` / `si`, and `cron(...)`, raise `TaskQueueNotInstalled`
  naming the extra. `TaskQueueNotInstalled` is public (advanced): `from
  sandloom.sdk.tasks import TaskQueueNotInstalled` to catch it, recorded in
  `public_api_baseline.json` with the rest of `sandloom.sdk.tasks`. The package check boots each module wheel on plain
  `sandloom` and probes `/health` and `/ready` (KB-01).
- **`/ready` is no longer 200 when a module's tasks cannot be enqueued.** A
  loaded module with a `tasks.py` (tasks or periodic tasks) on a process whose
  default taskqueue cannot open (for the `celery` driver: Celery not
  installed) used to look ready. `/ready` now opens the taskqueue while such a
  module is loaded and answers 503 with `taskqueue_status: fail`; the internal
  token also shows a `taskqueue` block naming the capability, the instance and
  the modules with tasks. The probe shares the health cache
  (`HEALTH_CACHE_TTL_SECONDS`). With no such module the taskqueue is not
  probed (`taskqueue_status: unused`), and `/health` is unchanged.
  **Upgrade:** a deployment that loads billing or identity without the
  `celery` extra now reports not ready: install the extra, or leave the
  module out of `SANDLOOM_MODULES`.
- **`sandloom-identity` declares `argon2-cffi`, which its login service
  imports.** It installed only because the `sandloom` kernel requires it for
  its own password hashing. The packaging import check now refuses a module
  wheel importing a kernel requirement that is not part of the stack the
  kernel provides modules (FastAPI, Starlette, SQLAlchemy, Alembic, Pydantic,
  HTTPX): such a requirement is declared by the module itself.
- **A failure to list the installed module entry points stops startup.** The
  loader logged it and started without those modules; it is now a required
  module failure (`ModuleLoadError`).
- **One app's shutdown no longer closes the RPC client another app is
  using.** Two applications in one process share the pooled outbound RPC
  client; when the stopping app is no longer the process default, its
  shutdown retires the client (in-flight calls finish, the next call builds a
  fresh one) instead of closing it under the other app's calls.

## [0.7.0a11] — 2026-11-07

### Breaking changes

- **`UtcDateTime` refuses a naive `datetime`** (`NaiveDateTimeError`), in a
  value and in a comparison: its instant is not known (asyncpg reads it in the
  process's local time zone, SQLite as UTC). `CreatedAt`, `UpdatedAt` and every
  `UtcDateTime()` column are affected. `column - timedelta(...)` still binds an
  interval. The planner's raw executor takes no caller's key or filter of this
  type (it runs on the general executor, whose bind processing refuses a naive
  time); keyset columns, read back aware, stay on the raw executor.

  **Upgrade:** bind aware times to `CreatedAt`, `UpdatedAt` and `UtcDateTime`
  columns (`datetime.now(UTC)`, `value.replace(tzinfo=UTC)`); replace a module's
  `DateTime(timezone=True)` with `UtcDateTime()` (no migration on PostgreSQL).
- **Autogenerate and the migration drift step compare server defaults**
  (SANDLOOM-T006). `sandloom migrate new`, `sandloom migrate drift` (the drift
  step of `sandloom test`) and the migrations-match-models test share one set
  of options, `COMPARE_OPTIONS` in the migration environment: types and, now,
  column server defaults. A chain whose default differs from its model's fails
  the step, and `migrate new` writes the change. A JSON column's default is
  compared as the value it writes (PostgreSQL's `json` has no `=`). The
  standard modules and the examples had no such difference.

  **Upgrade:** run `uv run sandloom migrate drift` on PostgreSQL. For each
  `server default differs` it reports, add a revision with
  `uv run sandloom migrate new <module> "<message>"` and review it, or correct
  the model's `server_default`.
- **SQLite 3.38 or newer** (was 3.35): `json_path` uses its built-in JSON
  functions (SANDLOOM-B030 refuses an older one).

  **Upgrade:** check `python -c "import sqlite3; print(sqlite3.sqlite_version)"`
  where the application runs on SQLite.
- **`sandloom check` refuses SQL that not every database runs alike, in a
  module and in its tests** (SANDLOOM-C031 `portable-sql`, new, an error). A
  `func.<name>` (`sa.func`, an alias, `sqlalchemy.sql.functions`) outside the
  portable list (`abs`, `char_length`, `coalesce`, `count`, `length`, `max`,
  `min`, `nullif`, `sum`, each run on PostgreSQL and SQLite by a test, value
  and type) is a finding. That includes `func.now()` (the transaction's start
  on PostgreSQL, whole seconds on SQLite, outside the stored `UtcDateTime`
  format), `lower` / `upper` (ASCII only on SQLite) and `avg` (a `Decimal` on
  PostgreSQL, a `float` on SQLite). Raw SQL (`text()`, `literal_column()`,
  `DDL()`, `exec_driver_sql()`, `.op()`, `.bool_op()`, a string given to
  `.prefix_with()`, `.suffix_with()` or `.with_hint()`) needs `# sandloom:
  raw-sql <reason>` on the call or alone on the line above, except a plain
  literal (`server_default=text("0")`); a test decorated with
  `skip_unless_db_feature(...)` is not reported. Reading the database's name
  (`x.dialect.name`, `dialect_name()`, `get_backend_name()`) is a finding too,
  and so is a database URL literal naming a backend (`"postgresql://"`,
  `"postgresql+asyncpg://"`, `"sqlite+aiosqlite://"`, a
  `create_mock_engine` of one), each admitted only by the marker. Migrations are exempt; `CheckConstraint` and
  `Computed` strings are not read. The PostgreSQL cast (`::`) in `text()` moved here from
  SANDLOOM-C028, and the marker does not admit it.

  **Upgrade:** run `uv run sandloom check --only portable-sql`. Replace
  `func.now()` with `WallClock()` from `sandloom.sdk.data` (the database's
  clock at the statement, read back as an aware UTC time) or a bound
  `datetime.now(UTC)`; write raw SQL as SQLAlchemy expressions (a module test
  reads a kernel table through `sqlalchemy.table("audit_log", column(...))`),
  or mark a reviewed line. A test that asked `dialect.name` asks what it needs:
  `skip_unless_db_feature(...)` (a race test whose writers must overlap:
  `skip_unless_db_feature("concurrent_writers")`) or `has_optimized_reads(engine)`
  from `sandloom.sdk.testing`. A test that named a database URL takes the
  configured one (`settings.database_url`) or compiles against its session's
  dialect (`db.bind.dialect`).

- **Modules write no SQL dialect** (SANDLOOM-C028, an error). `sandloom check`
  fails on a `sqlalchemy.dialects` import in module code (also
  `sa.dialects...`) and on dialect options such as `postgresql_where=`;
  migrations are exempt. `sandloom.sdk.data` has the portable forms modules
  used dialects for: `CaseInsensitiveText`, `partial_index`, `search_index`,
  `conflict_insert(Model)` (`ConflictInsert` for annotations) and
  `await transaction_lock(key)`; ids use `sqlalchemy.Uuid`. Each database's
  SQL dialect renderer writes the forms, found in the `sandloom.sql_dialects`
  entry-point group (shipped: PostgreSQL and SQLite). A form the database
  cannot write refuses (SANDLOOM-R015), and a database without a renderer
  stops startup (SANDLOOM-B024). On PostgreSQL every form renders the SQL the
  modules wrote before: the schema and the migrations do not change.
  Identity, tenancy, billing and every example use them. The kernel follows
  the same rule: its outbox, inbox, command and HTTP idempotency stores, saga
  store, projection upserts and watermarks, locks, row security, read-only
  units and identity's claims all go through the renderer, and SQL functions
  only one database has are SANDLOOM-C031's. An architecture test counts
  every place outside the database driver that names a database (a dialect
  import, a `dialect.name` comparison, PostgreSQL-only SQL or functions, a
  dialect option or `dialect_options["postgresql"]`, a URL prefix test, an
  error-code check); the count may only shrink. The final allowlist has 21
  sites in 10 files, each with its reason, none in the driver's dialect-neutral
  code: the migration environment and provisioning, `sandloom dev`,
  `sandloom migrate extract-module` and the test harness's per-database
  provisioning.

  **Upgrade:** run `uv run sandloom check --only dialect-imports`. In your
  modules, replace `from sqlalchemy.dialects.postgresql import UUID` with
  `from sqlalchemy import Uuid`, `CITEXT` with `CaseInsensitiveText`,
  `postgresql_where=` indexes with `partial_index(...)`, `pg_insert(Model)`
  with `conflict_insert(Model)`, and `func.pg_advisory_xact_lock(key)` with
  `await transaction_lock(key)` (a 64-bit integer key, and the
  `transaction_locks` feature declared: SANDLOOM-C030). Then
  `uv run sandloom migrate drift` reports no difference.
- **A bare `DateTime(timezone=True)` in module code is an error**
  (SANDLOOM-C029, new). `sandloom check` flags it (also `DateTime(True)` and
  `TIMESTAMP(timezone=True)`) outside migrations: SQLite returns its values
  naive. `UtcDateTime()` from `sandloom.sdk.data` reads them back aware in UTC
  on every database and is the same `timestamptz` on PostgreSQL. Identity's
  token expiries, tenancy's invitation times, billing's `*_at` columns, the
  projection generator's `deleted_at` and the members recipe use it.

  **Upgrade:** run `uv run sandloom check --only utc-datetime-columns` and
  declare each finding `UtcDateTime()`; no migration follows on PostgreSQL.
- **`sandloom_identity.claims.identity_claims_select` and
  `identity_claims_lateral` are removed.** Identity's claims are two
  correlated scalar subqueries of `aggregate_list`,
  `identity_claims_columns(user_id)`, which run on SQLite as on PostgreSQL
  (no `LATERAL`, no `array_agg`).

  **Upgrade:** replace a join of `identity_claims_lateral(...)` or a select
  from `identity_claims_select(...)` with
  `roles, permissions = identity_claims_columns(<user id column>)` in your
  `select(...)`; the two columns read the same lists.
- **`sandloom dev up` refuses a checkout whose `.env` names a SQLite
  database.** A SQLite `DATABASE_URL` that is the last one in `.env` and
  stands outside the `sandloom dev` block is the checkout's own database:
  `dev up` refuses and leaves `.env` as it is (before, it commented the line
  out), and `dev run` serves the file and starts no dev services (before, it
  started PostgreSQL and Redis in Docker). A PostgreSQL `DATABASE_URL` is
  handled as before.

  **Upgrade:** a checkout whose `.env` ends with a SQLite `DATABASE_URL` and
  that uses the dev services removes or comments out that line.

### Deprecated

- **`transaction_lock` without a declared `transaction_locks` feature.**
  `sandloom check` warns (SANDLOOM-C030, new) and the warning says it becomes
  an error in the next minor release. Startup does not refuse such a module
  for it (SANDLOOM-B033 counts only declared features and tables built with a
  feature form). Identity now declares it.

  **Upgrade:** in the module's `define_module`, replace `"database"` in
  `capabilities=` with `CapabilityRequirement("database",
  features=frozenset({"transaction_locks"}))` (`sandloom.sdk.module`).

### Added

- **A caller that rolls back a unit joined to it on SQLite warns, and tests fail
  on it** (`JoinedUnitRolledBack`, G-UOW-09). On SQLite a contract command, a
  saga step's unit or a child `open_uow()` joins its caller's transaction; on
  PostgreSQL it commits on its own and stays committed. Code that relies on
  the caller's rollback undoing it passed on SQLite and failed in production.
  The `sandloom.sdk.testing` plugin turns the warning into an error. A test's
  own `db` session and saga transitions are not reported.

  **Upgrade:** a test that rolls back such a unit on purpose adds
  `@pytest.mark.filterwarnings("ignore::sandloom.core.unit_of_work.JoinedUnitRolledBack")`.
  Elsewhere, make the call a saga step or undo it explicitly.

- **CI runs the module suites on SQLite too: the `second-db-test-lane` job, and
  a skip ledger.** PostgreSQL (`backend-tests`) stays the mandatory lane. The
  new lane runs the standard modules' suites, every example's tests and an
  application from `sandloom new` with the standard modules (served on a
  SQLite file, signed up to an organization through the API, then its own
  `sandloom test`) on SQLite files. Each lane writes the tests it ran and
  skipped (`-p sandloom.core.testing.skip_ledger
  --sandloom-skip-ledger=<file>`, or `SANDLOOM_SKIP_LEDGER`), and the
  `skip-ledger` job (`tools/ci/skip_ledger.py`) fails on a test skipped on
  every lane, unless `tools/ci/skip_ledger_allowlist.json` names it with a
  reason. `tools/smoke/quickstart_check.py --database sqlite` runs the
  quickstart on SQLite. A new `database` feature, `concurrent_writers`
  (write transactions overlap, each holding its row locks), is derived from
  a renderer's `serial_writers` fact: PostgreSQL has it, SQLite does not, and
  a renderer that declares it itself is refused (SANDLOOM-B025). Race tests
  carry `skip_unless_db_feature("concurrent_writers")` and run on PostgreSQL
  only.
  `faulthandler_timeout` in the framework's (600 s) and the generated
  application's (300 s) pytest configuration: a hung test prints every
  thread's stack.

- **Migrations match models: `migration_drift(module)` and
  `assert_migrations_match_models(module)` in `sandloom.sdk.testing`**
  (advanced). On a scratch PostgreSQL database the helper applies the core
  chain, the chains the module depends on and its own chain to head, then
  compares the result with the module's models through Alembic's
  `compare_metadata`, with the options `sandloom migrate new` uses. It also
  compares each index as the database reflects it (access method, operator
  classes, sort order, included columns, `NULLS NOT DISTINCT`, a partial
  index's condition) with the same index created from the models in a second
  scratch database. Not compared: RLS policies, extensions, triggers, CHECK
  constraints and an index's collation. It returns one line per difference;
  the assertion fails naming each and the fix. A loaded module with no
  `migrations/` directory skips. On SQLite it skips: SQLite builds its schema from
  the models and never runs a chain (SANDLOOM-B028), and this test is what
  keeps the PostgreSQL schema the chains build equal to those models. Each
  standard module with a chain (identity, tenancy, billing) and the core chain
  has the test; `sandloom add module` writes `tests/test_migrations.py` with
  it. **Upgrade:** add the same one-line test to each of your modules that has
  a chain: `assert_migrations_match_models("<module>")`.
- **`has_optimized_reads(bind)` in `sandloom.sdk.testing`** (advanced):
  whether the planner's optimized executor can run on the test database (the
  DBAPI adapter's `raw_executor`), for declared-read tests that used to read
  `dialect.name`. A module's race test asks for overlapping writers with
  `skip_unless_db_feature("concurrent_writers")`; whether writers take turns
  (`has_serial_writers`) is a question of the kernel's own tests only, in
  `sandloom.core.testing.infra`.
- **`aggregate_list` in `sandloom.sdk.data`** (slice 10 of the database
  capability): the values of a group as one Python list, an aggregate like
  `count`. PostgreSQL writes `array_agg`; SQLite writes `json_group_array`,
  read back through the item type (a `Uuid` as a `UUID`, a `Boolean` as a
  `bool`, a `UtcDateTime` aware). A group without rows reads `[]` on both.
  JSON, array and binary values are refused. It is a feature form: it needs
  the new renderer feature `list_aggregates`, which both built-in renderers
  declare (members `aggregate_list`, `aggregate_list_type`). A module using
  it declares the feature (SANDLOOM-C030), and startup refuses it on a
  database whose renderer lacks it (SANDLOOM-B033). `SqlDialectConformance`
  checks the two members.
- **Identity's claims and refresh rotation run on SQLite.** The claims
  contributor and the refresh rotation built roles and permissions with
  `func.array_agg`, joined `LATERAL` into a data-modifying CTE; none of the
  three exists in SQLite. The claims are now two correlated scalar
  subqueries of `aggregate_list`
  (`sandloom_identity.claims.identity_claims_columns`; see Breaking changes). A refresh loads the
  token, its user and the claims in one `SELECT`, then consumes the token
  with a plain `UPDATE ... RETURNING` compare-and-set: still five statements
  on PostgreSQL, the same on SQLite, and no claims query. Identity now
  declares `list_aggregates` beside `transaction_locks`.
- **Test runs: an explicit `SANDLOOM_TEST_DATABASE_URL` beats an exported
  `DATABASE_URL`** (also the cache and broker URLs). The framework's root
  conftest seeded the overrides with `setdefault`, so a shell that still
  exported `DATABASE_URL` snapshotted settings on that database while the
  fixtures used the override: `skip_unless_db_feature` read the wrong
  database.

- **Nested write units commit with their caller on SQLite** (slice 8c). On a
  database whose renderer says `serial_writers` (SQLite), a local contract
  call, a saga step's unit or a child `open_uow()` inside a writing unit of
  the same task and database instance runs as a SAVEPOINT on the caller's
  connection instead of being refused (SANDLOOM-R016). Its rollback undoes
  only itself; the caller's rollback undoes its rows, outbox events and
  command record; its outbox dispatch, `on_commit` callbacks and amended
  command record wait for the caller's COMMIT. Units of other tasks nest one
  task at a time. The guarantee lattice computes the label "commits with the
  caller" (`nested_unit_label`; `effect_label("command", ...)` carries it when
  its `Mechanism` states `serial_writers`) and startup warns once per such
  database. R016 now refuses only a writer whose lock holder is not one of
  the task's open units. Read contracts, and a request's contract read scope,
  read through a read-only unit on SQLite, as on PostgreSQL never seeing the
  caller's uncommitted writes. PostgreSQL is unchanged: no savepoint, a fresh
  unit.
- **Module test suites run on SQLite** (slice 8c):
  `SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///<dir>/<name>_test.db`. Each
  xdist worker gets `<name>_test_gw<N>.db`, the file's owner is
  `<file>.owner`, the schema is built from the models and emptied between
  tests, the `db` fixture and a test's own sessions begin their transaction
  at their first write (each read sees the last commit until then), the
  application's units nest in the `db` session, and `scratch_database`
  skips. The per-database reset moved to `sandloom.core.drivers.schema_reset`.
  `skip_unless_db_feature(*features, database=)` (`sandloom.sdk.testing`,
  advanced) skips a test whose database instance lacks a feature.

- **Database features as `sandloom.sdk.data` forms** (slice 9 of the database
  capability). `Array(item_type)` (feature `arrays`), `fulltext_index(name,
  *columns, language=)` with `matches(columns, query)` (`fulltext_search`),
  `json_path(column, *path)` (`json_path`) and `json_contains(column, value)`
  (`json_contains`). Each database's renderer writes them; PostgreSQL declares
  all four (`item_type[]`, a GIN index over `to_tsvector` and
  `websearch_to_tsquery`, `#>>`, `jsonb @>`), SQLite `json_path`
  (`json_extract`, numbers through `->`). `json_path` reads the scalar's JSON
  text as stored, the same on both: a string unquoted, `true` / `false`, a
  number as written (`1.50`, `1e2`, `-0.0`, every digit of a large integer).
  It is compared with a `str`; a number on either side (`== 1.5`, `> 2`,
  `Model.id == json_path(...)`, an integer subquery, `coalesce(..., 0)`, a
  number bound to its parameter) raises `TypeError`, since PostgreSQL refuses
  text against a number and SQLite answers differently. Ordering uses each
  database's collation (`.collate("C")` for byte order on PostgreSQL). An index step reads arrays
  only (`NULL` on an object) and a negative one is refused. A module that uses a new form
  declares the feature (SANDLOOM-C030, an error; a `*` import from
  `sandloom.sdk.data` and a declaration it cannot read are findings too),
  and a module whose tables are built
  with `Array` or `fulltext_index`, or that declares a feature, is refused at
  load on an instance without it, naming the module, the form, the feature
  and the instance (SANDLOOM-B033, new). A statement compiled for a database
  without the feature refuses (SANDLOOM-R015, which now names the feature).
  Renderers write the forms through optional contract-2 members; one that
  declares such a feature without them is refused (SANDLOOM-B025), and
  `SqlDialectConformance` compiles each declared form, and runs
  `JSON_PATH_READS` (numbers as written) on the database through
  `read_json_paths`, which a renderer declaring `json_path` must override. Migrations render an
  `Array` as the database's type (`postgresql.ARRAY(sa.String(length=40))`).

- **An application starts on a SQLite file** (slice 8b of the database
  capability). `DATABASE_URL` and `DATABASE_SYSTEM_URL` accept any
  `<dialect>+<dbapi>://` URL, such as `sqlite+aiosqlite:///var/app.db`; startup
  refuses a dialect without a renderer, a DBAPI SQLAlchemy does not know and a
  synchronous DBAPI (SANDLOOM-B024). A database whose renderer says
  `schema_from_models` (SQLite) takes its schema from the models: the app runs
  `metadata.create_all` over every mapped table at startup, and a worker does
  too (`DatabaseProvider.ensure_model_schema`, default no-op). `create_all`
  never alters a table: a file whose columns, nullability, indexes or unique
  constraints differ from the models stops startup naming each difference
  (SANDLOOM-B028, new). Only the new ops setting
  `DB_RECREATE_ON_MODEL_CHANGE=true`, which `sandloom dev run` sets for the app
  it serves, sets such a file aside instead (renamed
  `<file>.stale-<UTC time>` with its WAL; nothing dropped) and builds a fresh
  one; a worker never does, production ignores it.
  `sandloom migrate` refuses such a database (B028; `migrate databases` still
  lists). A renderer that says `development_only` (SQLite) stops a production
  process unless it accepts the new risk `development_database_in_prod`, and
  the strict profile refuses it always (SANDLOOM-B029, new).
  `DATABASE_SYSTEM_URL` on a database without row security is refused instead
  of ignored (SANDLOOM-B026), and every system URL (`DATABASE_SYSTEM_URL`, an
  instance's `system_url`) gets B024's checks too. The platform's modules (identity, tenancy,
  billing, admin) start on SQLite and serve requests; the test harness on
  SQLite is next (slice 8c).
- **SQL dialect renderers, contract version 2.** A renderer states its
  `contract_version`, `name`, `minimum_server_version`, the database features it
  can give (`features()`) and what an error means (`classify_error`). The loader
  refuses another version or an unknown feature (SANDLOOM-B025). A database
  instance now advertises the renderer's features (`skip_locked`,
  `transaction_locks`, `snapshot_reads`, `readonly_transactions` on PostgreSQL),
  and the first connection refuses a server older than the renderer supports
  (SANDLOOM-B030: PostgreSQL 14, SQLite 3.35). An architecture test counts every
  place outside the driver that names a database, and the count may only shrink
  (design/infra-agnostic/spec.md).

- **The kernel's claims and upserts go through the SQL dialect renderer.** A
  renderer gains `greatest(*exprs)` (PostgreSQL `greatest`, SQLite `max`),
  `id_in(column, ids, key=)` (PostgreSQL one array bind, `= ANY(:key)`; others
  `IN`) and `array_insert_sql` (the outbox and audit bulk insert's SQL,
  PostgreSQL `unnest`; `None` elsewhere). The loader refuses a renderer missing
  any member of the contract (SANDLOOM-B025); `SqlDialectConformance` checks
  the new forms.
  The command, HTTP and inbox claims, the outbox writes and `id` matches, the
  saga store, the projection upserts and watermarks build their `INSERT ... ON
  CONFLICT` and `greatest` from it instead of naming PostgreSQL. A completed
  command or HTTP record restarts its expiry on every database (computed in
  Python from the claim, or read back from the row with a warning when the
  completing session did not claim it; it was PostgreSQL-only), and a lock timeout of the
  claim is recognised through `classify_error` (still `Busy`, the unit rolls
  back). The kernel's outbox, inbox, audit and saga tables declare `Uuid` and
  `partial_index`: the PostgreSQL DDL is unchanged, no migration follows. The
  claim and the outbox statements render the same SQL on PostgreSQL.

- **The kernel's locks go through the SQL dialect renderer.** A renderer's
  `transaction_lock` form becomes `lock_statement(key, *, scope=, wait=)` and
  `unlock_statement(key)`: a `LockStatement(statement, parameters)` (from
  `sandloom.driverkit`) that takes (or releases) a named lock, or `None` when
  the database has none. The statement is built once per key shape and the key
  travels in `parameters`; the kernel runs `execute(lock.statement,
  lock.parameters)`, so a lock taken per request builds no SQL. A key is a 64-bit `int`, a
  `(namespace, int | str)` pair or a `str`; PostgreSQL hashes a string on the
  server (`hashtextextended`, or `hashtext` in a pair), so two releases still
  exclude each other during a rolling deploy. The HTTP idempotency try-lock
  (still "busy, never wait"), the projection purge and rebuild lock, the
  migration run-wide and provisioning locks and the chain lock of a direct
  `alembic upgrade` ask it instead of naming PostgreSQL; on PostgreSQL the SQL
  is the same (the migration locks bind the key instead of inlining it).
  `transaction_lock` keeps its signature and SQL and still refuses on SQLite
  (SANDLOOM-R015). `SqlDialectConformance` checks that each lock is a
  `LockStatement` whose parameters bind its statement, or absent, that a
  never-waiting lock selects a boolean, that a session lock has its unlock,
  and that two keys of one shape share the statement and differ in their
  parameters (`test_a_lock_binds_its_key_at_execution`). A renderer may state
  the optional fact `aware_times` (PostgreSQL does): `DateTime(timezone=True)`
  keeps the instant and its DBAPI reads values back aware, so `UtcDateTime`
  converts nothing there and only refuses a naive time; a renderer stating it
  overrides the kit's `read_aware_time` to show it on the database.

- **Locks through the renderer.** The kernel's advisory locks (the HTTP
  idempotency try-lock, the projection purge lock, the migration and
  provisioning locks) come from the SQL dialect renderer's `lock_statement`;
  PostgreSQL takes the same locks with the same keys. `transaction_lock(key)` now
  refuses a key that is not a 64-bit integer (a `bool` included) with
  `TypeError` before it reaches the database.

- **Queue claims without `SKIP LOCKED`.** On a database whose renderer lacks
  `skip_locked`, the outbox relay claims each due row by compare-and-set on its
  lease (`locked_until`/`locked_by`, taken when one row changed, re-checking
  that the row is still due) and the saga sweep claims each due instance by
  compare-and-set on `swept_at`, one short transaction per instance, skipping
  an instance a transition moved since the sweep read it. On this path relays
  and sweeps run safely in parallel but do not split the work: a claim waits
  for a row another worker holds instead of skipping it, and a saga transition
  waits for the instance row lock. Delivery stays at-least-once. The batched
  relay claim needs `skip_locked` and `UPDATE ... RETURNING`; other databases
  use the per-row relay. On PostgreSQL nothing changes; no migration.
- **Timestamps on any database.** `CreatedAt` / `UpdatedAt` are stamped by
  the database's clock at the call through the new renderer member
  `wall_clock()` (PostgreSQL: `clock_timestamp()`, as before; SQLite: the UTC
  time to the millisecond, ties ordered by the UUIDv7 id), and read back as
  timezone-aware UTC times through `UtcDateTime`, new in `sandloom.sdk.data`
  (advanced): a `DateTime(timezone=True)` that writes aware times in UTC and
  attaches UTC to the naive values SQLite returns (the planner's raw executor
  still serves these columns). Use it for your own time columns. On
  PostgreSQL the DDL, the update SQL and the migrations `sandloom migrate new`
  writes do not change; no migration. `wall_clock()` is a required member of
  renderer contract version 2 and checked by `SqlDialectConformance`.
- **Two more renderer forms.** `for_update(stmt, *, skip_locked, read=False)`
  locks the rows a query reads (PostgreSQL: `FOR UPDATE [SKIP LOCKED]` /
  `FOR SHARE`, as before; SQLite: unchanged, its writers are serial) and
  `relaxed_commit_clause()` lets the outbox's mark-delivered commit without a
  durable-flush wait (PostgreSQL: today's `synchronous_commit` term; SQLite:
  none). Both are required members of renderer contract version 2 and checked
  by `SqlDialectConformance`; the PostgreSQL SQL is byte-identical (pinned by
  tests). The kernel's row locks and the mark-delivered statement use them, and
  the kernel names a database at 33 sites, down from 40.
- **Row security through the renderer; application-enforced isolation said
  aloud.** The renderer member `row_security()` returns a `RowSecurity`
  (`sandloom.driverkit`, with `TENANT_SETTING`): the tenant policy DDL of
  `enable_tenant_rls` / `disable_tenant_rls`, the per-transaction binding of
  the tenant (the merged `BEGIN` text included) and the startup role and
  coverage queries. PostgreSQL returns them byte-identical to before (pinned
  by tests); SQLite returns `None`. The binding text is built once per engine.
  `tenant_rls`, `system_url` or `require_system_channel` on a database without
  row security stops startup (SANDLOOM-B026, new). Without them, its
  `TenantScoped` isolation is the ORM guard only: startup warns
  "application-enforced tenant isolation" for the instance, the guarantee
  lattice computes that label from the instance's features
  (`Mechanism.from_features`, `tenant_isolation_label`), and
  `multi_tenant_strict` still refuses it. `enable_tenant_rls_statements` (an
  internal helper) now takes `dialect=`. The kernel names a database at 23
  sites, down from 30.
- **Read-only and snapshot units through the renderer; no silent snapshot
  downgrade.** Renderer members `begin_statement(write=, read_only=, snapshot=)`
  (the `BEGIN` text, `None` for the plain one) and `transaction_options(read_only=,
  snapshot=)` (the SQLAlchemy execution options to the same effect) shape the
  planner's read units and `ReadOnlyDbSession`. PostgreSQL returns today's
  texts and options (pinned against the pre-slice code by tests), computed once;
  SQLite the plain `BEGIN` and no options. A `SnapshotReads` route or a
  `read_scope(..., isolation="repeatable_read")` on a database without
  `snapshot_reads` is refused (SANDLOOM-B027, new): at startup when the default
  database lacks it, else when the scope's read unit opens, before any I/O.
  Before, the planner silently read without a snapshot there. A read-only unit
  on a database without `readonly_transactions` relies on the read-only scope
  and the ORM read guard. The planner adapter's `read_unit` takes
  `snapshot=` instead of an isolation name (`ISOLATION_LEVELS` is now
  `SNAPSHOT_ISOLATION`). The kernel names a database at 15 sites, down from 23.
- **The DBAPI adapter; `DB_READ_ONLY_SESSIONS` needs a guard.** What the
  `sqlalchemy` driver knows about one DBAPI moved behind a `DbapiAdapter`
  (`sandloom.driverkit`: `DbapiAdapter`, `DBAPI_CAPABILITIES`,
  `register_dbapi_adapter`), chosen by the URL's DBAPI: how a lost connection
  looks, the connect arguments carrying the server timeouts, and the fast paths
  (the merged row-security `BEGIN`, the planner's raw executor, the client
  read-only policy, the dead-connection recovery, the reconnect backoff). The
  asyncpg code lives in `sandloom.core.drivers.dbapi_asyncpg`; any other DBAPI
  takes the plain path. An adapter states `contract_version` (1); a driver
  package's adapter may claim only `read_only_policy` and `reconnect_backoff`
  and is never replaced by the shipped one (SANDLOOM-B025). The plain path
  classifies a lost connection through the renderer, so `postgresql+psycopg`
  answers 503 too. The merged `BEGIN` takes its text from the renderer's
  `begin_statement`, the planner's source. On PostgreSQL with asyncpg every SQL
  text, connect argument and error classification is unchanged (pinned against
  the pre-slice code by tests). A new error kind, `transaction_aborted`
  (PostgreSQL's 25P02), replaces the kernel's own SQLSTATE checks.
  `DB_READ_ONLY_SESSIONS=true` on a default database with neither
  `readonly_transactions` nor a client read-only policy stops startup
  (SANDLOOM-B031, new). The kernel names a database at 6 sites, down from 15.
- **SQLite in the `sqlalchemy` driver, for development and tests.** The driver
  serves `sqlite+aiosqlite://` (the new `sqlite` extra:
  `pip install 'sandloom[sqlite]'`). The aiosqlite adapter
  (`sandloom.core.drivers.dbapi_aiosqlite`) turns pysqlite's implicit
  transactions off and emits each unit's `BEGIN` itself, so savepoints work.
  Every unit not declared read-only begins `BEGIN IMMEDIATE`, reads before its
  first write included, so writers are serial; a read-only unit begins deferred
  with `PRAGMA query_only` on. Each connection opens with WAL, `foreign_keys`
  and `busy_timeout` (`DB_STATEMENT_TIMEOUT_MS`: 30 s by default, 5 s when it
  is 0). SQLite now
  declares `transaction_locks` (`transaction_lock` and the projection purge
  lock succeed inside a unit that may write, R015 in a read-only one; new
  `WRITE_LOCK_HELD` in `sandloom.driverkit`), `snapshot_reads` and
  `readonly_transactions`. `SQLITE_BUSY`/`LOCKED` classify as
  `lock_not_available`, `SQLITE_BUSY_SNAPSHOT` as `serialization_failure`. An
  in-memory database, a SQLite URL without `+aiosqlite` and a missing aiosqlite
  stop startup (SANDLOOM-B032, new); only a test engine on one shared
  connection may be in memory, and it advertises neither `snapshot_reads` nor
  `transaction_locks` (its `transaction_lock` refuses, R015). A second unit of
  the same task that begins a write while the task's first unit holds the
  write lock (a local contract call, a saga step, a child `open_uow()`) is
  refused at its BEGIN (SANDLOOM-R016, new) instead of waiting for itself until
  `busy_timeout`. Startup asks each database's DBAPI adapter for a
  build-free `config_problem(url)`. The outbox's AUTOCOMMIT mark-delivered runs on any
  database. `DbapiAdapter` gains `config_problem`, `ping`, `connection_closed`
  and `prepare_engine`: the pool's idle probe and closed-connection check are the
  adapter's (asyncpg's unchanged). The conformance kit requires non-empty
  `transaction_options` for a snapshot unit when `snapshot_reads` is declared;
  a per-statement read unit may begin like the snapshot one only when the
  test sets `read_units_share_one_snapshot` (SQLite). The
  kernel names a database at 5 sites, down from 6.

- **A process refuses a SQLite file replaced under it** (SANDLOOM-B028). A
  recreate (`sandloom dev run` after a model change) renames the file while
  workers, beat or the admin may still hold it open, and they would go on
  writing to the renamed file. Each process now remembers the file it opened
  (device and inode) and checks the path at each connection checkout, one
  `stat`; once the path names another file, or none, the checkout fails with
  `DatabaseFileReplaced`, which names the file and says to restart the
  process. The process that sets the file aside opens the new one. Only a
  development-only database (SQLite) is checked; PostgreSQL pays nothing.
  Restart the workers, beat and admin after a recreate.

### Changed

- **A full-text `language` is the text's language by its English name**
  (`fulltext_index`, `matches`): one of `FULLTEXT_LANGUAGES`, or `"simple"` for
  no stemming. Each SQL renderer maps it to its own configuration; PostgreSQL
  uses its text search configuration of the same name, so existing values and
  indexes are unchanged. A name outside the list is refused.
- **`conflict_insert`'s shape is documented as database-neutral**: the key a row
  conflicts on, and skip or update. A renderer for a database without
  `ON CONFLICT` returns its own insert with the same methods.

- **The inspection index records no line numbers** (`.sandloom/inspection.json`,
  schema 2). A route's or saga step's source is its file and qualified function
  name; `sandloom context` and `sandloom graph` look the line up when they
  print. An edit or a reformat above a route no longer makes the tracked index
  stale, and two branches no longer conflict on it for that reason. Its
  `distributions` record the public version, without a local `+g<sha>` build
  tag, so a framework rebuild leaves it current. Readers still accept a
  schema 1 index, and `sandloom graph diff` compares across both. It
  compares a route's or step's source by file only: moving a handler within
  its file is not a reported change (a rename still shows in the route's
  `name`).

  **Upgrade:** run `uv run sandloom generate` and commit
  `.sandloom/inspection.json`.

- **Docs: the organization billing gate on a route without `org_id`.** The
  `org-gate` recipe (`sandloom docs recipe org-gate`) and
  `docs/modules/http/authorization.md` say to use
  `of=Account.org(param=None)`, the organization the request is bound to, on a
  tenant-bound route with no `org_id` in its path or query
  (`Account.org(param="org_id")` fails SANDLOOM-C027 there), and that
  `Account.bound_tenant()` also accepts a personal tenant.

- **`UtcDateTime` is a beginner name**: `from sandloom.sdk import UtcDateTime`,
  like `CreatedAt`, with no `# sandloom: advanced` marker; the beginner tier
  is 30 names. SANDLOOM-C029 requires `UtcDateTime()` for every time column
  in module code, so its fix used to fail SANDLOOM-T009 unless the import
  carried a marker. `sandloom.sdk.data` still exports it.

- **Command dedupe and saga crash recovery are stated for both databases**
  (G-CMD-01, G-SAGA-07, G-UOW-09). A command's dedupe record commits in the
  same transaction as its effect: the provider's own unit, or on SQLite the
  caller's transaction a local command joins. A saga step that crashed after
  its effect committed is replayed from the command log where the provider
  commits on its own (PostgreSQL); on SQLite the effect, its record and the
  step's state commit together, so a crash loses all three and the redrive
  runs the step once. G-UOW-09 now says a unit nests only inside a caller
  that has already run a statement. The saga crash tests are laws that hold
  on PostgreSQL and SQLite. One behaviour changed, on SQLite only: a forward
  command step whose provider commits on its own (over RPC, or on another
  database instance) first commits its "started" marker in a transition of
  its own (`marked`), then calls in the next one. Before, the marker joined
  the calling transition and a crash after the provider committed lost it,
  so the deadline compensated without resolving the step and the post-TTL
  guard could re-issue it. PostgreSQL is unchanged.

- **An application from `sandloom new` runs on SQLite out of the box.** Its
  `dev` dependencies bring the framework's `sqlite` extra (aiosqlite), and its
  README says how: `DATABASE_URL=sqlite+aiosqlite:///./data/app.db` with
  `CACHE_URL` / `BROKER_URL` in `.env`, outside the `sandloom dev` block.
  A `DATABASE_URL` whose database takes its schema from the models (SQLite)
  is the checkout's own database when it is the assignment that wins in
  `.env` (the last one) and stands outside the block. `sandloom dev run` then
  serves it and starts no dev services; before, it started PostgreSQL and
  Redis in Docker and served the app on them. `sandloom dev up` refuses and
  leaves `.env` as it is; before, it commented the line out. A PostgreSQL
  `DATABASE_URL` outside the block is handled as before. `sandloom migrate
  drift`, the "migration drift" step of `sandloom test`, says it skips on a
  database without migration chains instead of failing with SANDLOOM-B028,
  so `sandloom test` passes on SQLite.

  **Upgrade:** an existing app adds `sqlite` to its `sandloom[check,testing]`
  dev requirement to run on SQLite. The `.env` change of `sandloom dev up`
  and `dev run`: see Breaking changes.

- **The API docs look like sandloom.dev outside production.** `/docs` (Swagger UI) and
  `/redoc` get the site's header with the app's name and links (console, Swagger UI,
  ReDoc, Sandloom docs), the caracal in the tab, Manrope and Hack, the
  site's surfaces and accent, operation cards with method colours that fit them, and
  a theme switch in the header (system, light or dark, kept in the browser; ReDoc is
  redrawn in a dark theme of its own). The fonts ship in the wheel with their licenses and are
  served by the app (no font service). Swagger UI keeps the authorization across
  reloads and shows request durations. Production keeps FastAPI's own pages: a
  deployed app's API docs are the app's.
- **Seeded test runs hand the slow files to the workers first.** With a seed
  (every `sandloom test`, `--sandloom-shuffle`), the shuffle plugin turns off
  pytest-xdist's `--loadscope-reorder`, which handed the files of `--dist loadfile`
  out largest first, and puts the files that hold a test marked `slow` first. A long
  one-test file used to start after every other file; it now starts at once. In the
  framework's full suite this alone took ~150 s off the wall time. (tm: test-speed)
- **`sandloom test` runs the architecture rules once.** The `sandloom check` step
  records what it found, and the framework's architecture tests read it back instead
  of running every rule again; run alone, they still run the rules. (tm: test-speed-2)
- **The framework's own checks are faster.** The package check builds, installs and
  smoke-tests the distributions side by side (131 s to 29 s alone), the docs
  generator check builds its scratch apps side by side (15 s to 7 s), and the
  pre-push hook runs the suite in a printed random order, like CI. Fixed waits in the
  tests became waits for the event itself (a lock waited on, a key expired, a
  statement running). (tm: test-speed, test-speed-2)

### Fixed

- **Two applications in one process no longer share a runtime.**
  `create_app` moves the process default core to the app it builds last, and
  kernel paths outside a request handler (event publishing, units, services)
  read the active core. A request of the first app could then publish to the
  second app's event bus. Every request and the app's lifespan now run with
  their own core bound to the context (`use_active_core`), and the
  background tasks they start inherit it. The binding carries the app's
  infrastructure registry too, so `cache()`, `notifier()`, the object store
  and the task queue a module resolves are its own app's.
- **The service registry's strict name check cannot be skipped.** It was
  imported under `except Exception`, so a failed import registered services
  under the weaker check; the import is now required.
- **A refresh no longer mints a role removed while it ran** (CWE-362). The
  refresh read the user's roles before its issuance check and minted the
  access token later; a role change committed in between revoked it only if
  the token was minted within the revocation's clock skew. The refresh now
  collects the claims after the issuance check and the token's consume, and
  stamps the token at that read (`create_access_token(..., claims_read_at=)`).
  A revocation mark the check saw lifts the token past it; a mark noted after
  the read never does, whatever the issuer and revoker clocks say. A refresh
  takes one more statement (the claims are no longer read with the token).
  A later change revokes a token stamped past a mark set into the future,
  too: a refresh that saw a mark within the last clock skew stamps its token
  past it, and a second change committed before that mark passed, from a
  clock whose `now + skew` was lower, kept the stored maximum and left the
  token valid. A revocation that finds the stored mark at or past its own
  instant now writes it one microsecond further, above every token stamped
  past it. The tokens the revoking request mints after the call stay valid:
  they are stamped past that value, and the writes of one revocation (the
  `require_immediate` early mark, the post-commit write, the durable
  applier's replay) recognise each other through an identity in the value's
  digits past the microsecond, so a repeated write does not move the mark
  again. `core.auth.tokens_revoked.v1` gains the optional `revocation_id`
  field. The durable applier also lifts past a `require_immediate` early
  mark whose post-commit write was lost. Concurrent revocations of one
  subject still all succeed; the stored mark ends at their maximum or a few
  microseconds past it.
- **Billing named PostgreSQL in its code.** Its intent insert read the partial
  index's predicate back as `dialect_options["postgresql"]`; the predicate is
  now the model's own constant. SANDLOOM-C028 now reports a module that reads
  one database's option of a schema construct.
- **A system channel on another database than its instance is refused at
  startup** (SANDLOOM-B026). `DATABASE_SYSTEM_URL` naming a SQLite file next to
  a PostgreSQL database passed when no row-security option was set.

- **A green `sandloom test` no longer leaves a generated file stale.** With a
  project's `fix = true`, `ruff check` could rewrite source (an import sorted
  onto several lines) after the generated-files check had passed, moving the
  routes the inspection index pointed at: `sandloom test` was green and
  `sandloom test --ci` on the same tree failed with SANDLOOM-T015. Developer
  mode now runs `ruff check` first; `--ci` is unchanged (it fixes nothing).

- **The read planner's prefix filter is literal, and keeps its case, on
  SQLite.** The general form bound `_` and `%` escaped with backslashes,
  PostgreSQL's default `LIKE` escape, without naming the escape; SQLite has
  none, so `D_v` found nothing (`examples/projecthub`'s filter test on
  SQLite). And SQLite's `LIKE` folds ASCII case, so a case-sensitive `prefix=`
  matched case-insensitively. The filter is now the renderer's: two optional
  members of renderer contract 2, `prefix_pattern` and `prefix_match` (both or
  neither, SANDLOOM-B025; checked by `SqlDialectConformance`). PostgreSQL
  keeps `LIKE` / `ILIKE` with a backslash `ESCAPE`; SQLite writes a
  case-sensitive prefix as a `GLOB` (`*`, `?`, `[` as one-character classes)
  and a case-insensitive one as `lower(...) LIKE lower(...) ESCAPE`.
- **Framework tests that failed on a busy machine.** Waits sized for an idle
  machine (a 100 ms projection read, a 30 ms reconnect budget, 0.1 s RPC attempts
  in a 0.4 s call, a 60 ms stream lease, a wall-clock micro-benchmark, a 2 s RPC
  timeout) failed under load; each now has room, or waits for the event, and
  still asserts the same thing. `test_dev_external` used Redis index 860 while the
  test Redis has 256 databases. (tm: test-speed, test-speed-2)
- **A failed `sandloom dev up` names the cause.** Docker wraps long errors, and the
  message showed only the last wrapped line (`(exit status 4))`); it now starts at the
  line that names the error and joins the lines it wraps onto. Images the dev and test
  services share are listed and pulled once, without `docker pull` output.
- **A cancelled post-commit dispatcher no longer outlives its event loop.**
  On SQLite a cancel landing in a statement came back as
  `PendingRollbackError`: the SQLite adapter's rollback hook read the state of
  the connection the cancel had invalidated. The dispatcher logged it and waited
  on its queue forever, and the loop's close (a test's teardown, a worker's
  shutdown) never returned: a third to half of the runs of a test file hung. The
  hook now skips an invalidated connection, so the cancel comes out, and the
  write-lock mark of an invalidated connection is released when the pool
  invalidates it: the same task's retry on another connection is no longer
  refused as a nested writer (SANDLOOM-R016). The dispatcher, its marks, and the
  in-line publish and mark of a `db_session()` unit (workers, scripts) end on a
  cancellation whatever error a driver turns it into. The unmarked rows stay
  the relay's, as before.
- **Identity, tenancy and billing stamp and compare times with the database's
  clock in the stored format** (`WallClock()`, was `func.now()`): enabling 2FA,
  issuing an operation intent, and superseding, revoking and accepting an
  invitation. On SQLite `func.now()` wrote whole seconds, which compared with
  the microsecond text of the `expires_at` column within a second off. On
  PostgreSQL the time is the statement's (`clock_timestamp()`) rather than the
  transaction's start; no schema change.
- **Billing's reconciler could not escalate an intent on SQLite.** Closing an
  owed compensation whose checkout session was paid meanwhile wrote
  `greatest(...)`, which SQLite does not have; it is a `CASE` now.
- **The test harness never waits without bound.** The PostgreSQL schema reset
  sets `lock_timeout` (30 s), so a database shared by mistake with another
  run's open transaction fails at once instead of hanging (no fallback reset
  waits again); creating a worker's database connects with a 5 s timeout. The
  SQLite and PostgreSQL fixtures of the nested-units tests, and
  `open_test_engine`, finish the post-commit dispatch before closing their
  engine (bounded; a slow drain never fails the teardown).

## [0.7.0a10] — 2026-11-03

### Added

<!-- lookup -->
- **`sandloom admin list`** names the platform administrators, whether each has a
  password and the last sign-in. In production it needs `--confirm-production
  --reason`, is audited (`identity.admin.listed`) and masks addresses unless
  `--show-emails`. (tm: first-run-polish)
- **A start page at `/` outside production**: the address `dev run` prints links
  to the console, the API docs and `/health` instead of answering 404.
- **The console keeps its session across a reload outside production**
  (sessionStorage, never localStorage); production keeps it in memory only.
- **An initial commit** after the wizard's own `git init`, when git has a user name
  and email (`.env` stays out).
- **PostgreSQL and Redis without Docker**: `sandloom dev up --services external
  [--database-url URL] [--redis-url URL]` uses servers you already run (Homebrew,
  Postgres.app). It creates the app's database and writes the same `.env` block and
  `.env.test.local`; the choice is kept, and no `dev` command calls Docker after it.
  `sandloom new` asks how to run them (Docker, your own servers, or later) and takes
  `--services docker|external|none`, `--database-url`, `--redis-url`. (tm: wizard-ux-2)
- **`sandloom dev run` says what runs** once the app answers: the app, the admin
  console, the API docs and `/health`, in one block after uvicorn's own lines.
- **A favicon outside production**: `/favicon.ico` is the caracal in the brand
  colours; the start page links it.
- **`sandloom admin create --quiet`** prints no success line (the wizard's summary says
  it). `--if-missing` no longer needs `--invite` or `--password-stdin` when the address
  is already an administrator.

### Changed

<!-- lookup -->
- **`sandloom explore` is marked experimental** in `sandloom --help` and the docs;
  `graph` and `context` stay stable. The wizard's summary also lists the API docs.
  (tm: wizard-docs-line)
- **The `sandloom new` wizard, round two** (tm: wizard-ux-2). It welcomes with the
  site link and asks in plain words. The setup question says what it does and how
  long it takes. After the setup comes the console administrator: `admin create`
  asks the password on the terminal, so the wizard never handles it. A closing
  summary says how to start the app and what to open; help and the repeat command
  come after.
- **`sandloom new` makes a git repository with a first commit by default**, as
  `cargo new` does; `--no-git` skips it, also with `--yes`. Inside an existing
  repository it creates none and commits nothing. The first commit runs no hook and is
  not signed.
- **identity's password policy checks the length** (8 to 100 characters) wherever it
  applies. `admin create`'s prompt names the address, the app and the minimum length.
- **Two apps in one repository** share the dev containers but never a database, a
  test database, a port or a `.env` block.
- **The Architecture explorer's header** shows the caracal instead of a letter.

- **Each checkout has its own app port.** The first `dev up` picks 8000, else the
  next free port, and keeps it; `dev run`, `admin create` / `invite` links and the
  wizard's summary use it.
- **`dev run` starts stopped dev services** first, the way `dev up` does, and exits
  0 when stopped with Ctrl-C.
- **Quieter boot.** Module loading, built drivers, the planner, a first boot's
  schema file, the pool's dispose and the generated dev override log at DEBUG;
  `--debug`, `SANDLOOM_DEBUG=1` or `LOG_LEVEL=DEBUG` show them. The wizard's
  spinner shows no log line. `dev down` prints one line (`--verbose` for
  Compose's output); `dev up` suggests the migrations only for a new or empty
  database.
- **App names that shadow an import are refused**: `test`, `tests`, any stdlib
  module, and the import names of installed packages (`redis`, `sandloom`).
- **A failed wizard step** shows its reason first, no traceback that repeats it,
  and the remaining commands one per line.
- **The administrator's email** from `git config` needs a confirmation; the summary
  says "Sign in as …" and how to get a new password. The console's failed sign-in
  names `admin list` and `admin invite` outside production.
- **The console names users by email** in "Recent activity" and the audit pages.
- **A first password set through a set-password link** is audited as
  `identity.user.password_set`, not `identity.password.reset`.
- **A faster owner console.** `/admin/` serves its hashed assets precompressed
  (brotli, gzip; built by `admin/scripts/compress.mjs`, also used by the standalone
  nginx through `gzip_static`), gzips the admin manifest, and lets the browser
  keep its fonts and icons with `no-cache` and an ETag (a 304) instead of
  `no-store`; the entry page stays `no-store`. A list no longer shows its pagination under the loading
  rows, where it jumped down once rows arrived. Lighthouse, median of 3, cold
  cache: transfer 783 → 223 KiB (sign-in) and 839 → 278 KiB (Users); mobile
  Users performance 72 → 95, LCP 5.9 → 2.8 s, CLS 0.146 → 0.009. (tm: console-perf)
- `sandloom new --help` hides the contributors' `--framework-*` flags.
- The generated README no longer hard-codes `:8000` and drops the outdated
  verification-mail paragraph.

### Fixed

- **A fresh app with a short name passes its own format gate.** `asgi.py` lays out
  its `create_app(...)` call as `ruff format` does for that name; `demo` failed
  before. The docs snippet generator creates its apps with `--no-git`.
  (tm: wizard-ux-2)
- `dev down` then `dev up` no longer moves a port Docker Desktop releases late.
- A startup task that cannot reach PostgreSQL or Redis logs one line with the
  fix instead of a traceback; in development the app reports an unreachable
  database once.
- **`dev up --services external` never adopts a database it did not create.**
  An existing database with the app's name on your server (another project's) is
  refused instead of migrated into; the databases it creates carry a comment
  (`sandloom dev: <checkout>`), and `dev reset` and `dev prune` drop only those.
  A database an earlier build created and `dev.json` records is marked on the next
  `dev up`.
- **`--services external` takes only empty Redis indexes** on your own server, below
  its `databases` setting: `dev reset` and `dev prune` flush them. `dev.json` is
  owner-only (0600) once it holds the servers' URLs.
- **`sandloom new` with your own servers on the usual ports** no longer fails after a
  successful setup; a failed setup with `--yes` exits 1. Its git calls ignore a hook's
  `GIT_DIR`.
- **`/admin/index.html` carries the console's CSP**, `Accept-Encoding` q-values are read
  as numbers, and a browser that blocks `sessionStorage` still gets a working console.
- **`sandloom admin list`** shows sign-in times in UTC whatever zone the driver returns.
- **`--services external` refuses URLs whose security it would drop:** `rediss://`,
  a Redis ACL user, Redis URL options and PostgreSQL `?sslmode=…` (or any query
  option) were silently lost, sending passwords in cleartext.

## [0.7.0a9] — 2026-11-03

### Added

- **`sandloom new` asks at a terminal.** Without a name it asks what the flags
  leave open, from the app's name to its first administrator. The setup runs
  the quickstart up to the migrations, then `admin create`, one line per step.
  A failed step shows its last lines and the commands left. The summary ends with a pinned `uvx`
  command that makes the same app with no question. `git init` runs first
  unless the app is inside a repository.
- **Scripts and agents get no question.** `--yes` (alias `--no-input`), CI,
  a pipe or a coding agent's shell (`CLAUDECODE`, `CODEX_SANDBOX`,
  `GEMINI_CLI`, `CURSOR_AGENT` and others) mean no question. Then the output
  ends with the "Next steps" block, as before. `--setup` and
  `--admin-email` run the setup without questions; `--plain` turns off
  colour and redrawn lines.
- **The Sandloom look in the terminal.** `new`, `dev up`, `dev status`,
  `dev run`, `admin create`, `admin invite` and the one-line connection
  errors use one accent colour, symbols with an ASCII fallback, and colour
  only at a terminal. `NO_COLOR` turns it off and `FORCE_COLOR` turns it on.
  Without colour the text is unchanged.

### Changed

- **An app name may not shadow a standard library module** (`json`,
  `email`): `sandloom new` refuses it, with or without the wizard.

## [0.7.0a8] — 2026-11-03

### Added

- **`sandloom dev`: local services without collisions.** `uv run sandloom dev up`
  starts Postgres and Redis once per repository, on free ports it keeps. Each
  checkout or git worktree gets its own database and Redis indexes, written into a
  marked `.env` block and `.env.test.local`. Several agents can run it at once:
  commands wait for each other, and `reset` and `down` refuse while others use the
  services. Also `dev status [--json]`, `run [--port N|auto]`, `reset`,
  `down [--all] [--volumes]`, `prune` and `logs`.
- **Separate test databases for parallel runs.** Two `sandloom test` runs in one
  checkout get separate test databases and Redis indexes (test-run slots).
- **One-line connection errors.** A command that cannot reach PostgreSQL or Redis
  prints one line with the fix (SANDLOOM-R014); `--debug` shows the traceback.
- **`sandloom admin create --email ADDRESS`** creates the first console
  administrator. At a terminal it asks for the password twice; `--invite` prints a
  one-time set-password link (15 minutes) for agents and CI; `--password-stdin`
  reads one line from a pipe. No password is ever written to a file.
  `sandloom admin invite` issues a new link.
- The console has a set-password page. While no administrator exists, its sign-in
  page and the app's startup log name the command.
- **Two-step console sign-in.** The sign-in page asks for the email and password
  first, and for the two-factor code on a second screen only when the account has
  two-factor sign-in turned on.
- `NOTIFIER_LOG_LINKS=true` (local development) prints the links an email carries.
  Production refuses it unless the risk `log_links_in_prod` is accepted.

### Changed

- The quick start of a new app is `uv sync`, `uv run sandloom dev up`,
  `uv run sandloom migrate upgrade-all --apply`, `uv run sandloom dev run`. The dev
  compose file has no fixed `name:` and needs its port variables.
- Test-resource owners include a machine id; markers written before stay valid.
- `sandloom security grant-admin` no longer marks an unverified address verified.
  It refuses it; `sandloom admin create --take-over` makes the account an
  administrator and ends its old password.
- **Choosing coding agents is a checklist.** `sandloom new` and `sandloom add skills
  --choose` show the agents with the installed ones checked: arrows move, space
  checks, Enter confirms. Where single keys cannot be read, the typed answer stays.

## [0.7.0a7] — 2026-11-03

### Added

- **Skills for your coding agents.** `sandloom new` asks which coding agents the
  project uses, with the installed ones marked, and writes the skills where each
  reads them: `.claude/skills` (Claude Code), `.agents/skills` (Codex, Gemini CLI,
  Cursor, Copilot, OpenCode, Amp, Devin Desktop), `.kiro/skills` (Kiro), plus
  `CLAUDE.md` / `GEMINI.md` pointers. `--agents claude,codex` chooses without a
  prompt. The choice is kept in `.sandloom/agent-files.json`.
- An `sandloom new` app gets the module-author contract in `<app>/modules/AGENTS.md`.
- The `schema & deploy` step of `sandloom test` reports an advanced `sandloom.sdk`
  import without a marker in an application's modules (SANDLOOM-T009).

### Changed

- Applications from `sandloom new` and the examples limit function complexity
  (ruff `C901`, threshold 10, outside tests), as the framework does.
- `sandloom add skills` replaces the agent files it wrote earlier with the
  installed version's, and keeps only the files you edited (`--force` replaces
  those too). `--agents` and `--choose` change which agents it writes for. Run it
  once after upgrading: files written before this version are treated as edited.
- `sandloom check` prints the error catalogue's text for each rule, naming the
  1.0 API (`Event.publish()`, `@on`, `Contract(REF)`), the same text as
  `sandloom explain`.

### Fixed

- `sandloom add --help` lists `deploy`, and `sandloom add --root . deploy --help`
  reaches the deploy options.
- The `sandloom[admin]` extra requires the current `sandloom-admin` version; it
  pointed at `0.7.0a5`, which was never published.

### Security

- The admin console's runtime image upgrades expat to 2.10.0 (four advisories,
  one high).
- The 13 Debian advisories new since the runtime baseline (sqlite3, gcc-14) are
  reviewed and recorded; Debian ships no fix for any of them.

### Documentation

- The docs sections follow one path: Getting Started, Develop with AI, Modules
  (with the batteries), Infrastructure, Admin Console, Deploy and operate,
  Advanced, Reference, Contribute. Old URLs forward to the new pages.
- New pages: the toolchain, generators, agent tooling and checks.

## [0.7.0a6] — 2026-11-01

### Added

- **The admin console is a package.** `sandloom-admin` carries the prebuilt console and
  serves it at `/admin/`, with no Node or separate image. Add it with `sandloom new
  --with admin` or `sandloom[admin]`. It is off in production until
  `ADMIN_CONSOLE_ENABLED=true`, and `ADMIN_CONSOLE_DIST` serves your own build.
- `sandloom generate` writes `<app>/shared/admin-manifest.json`, and `sandloom test`
  fails when it is stale (SANDLOOM-T016).
- `sandloom security grant-admin` creates an application's first platform
  administrator, with a dry run and an audit record.

### Fixed

- The console no longer calls billing's API in an application without billing.

### CI

- Wait for a newly created GitHub draft release to become visible in REST reads.
  Retry only reads, with a fixed limit; preserve single create/upload operations
  and fail-closed checks for conflicting source tags and asset hashes.

## [0.7.0a5] — 2026-10-31

### Breaking changes

This alpha changes infrastructure extension APIs. Review the
[0.7.0a4 → 0.7.0a5 upgrade steps](https://sandloom.dev/docs/operate/upgrade/#upgrading-from-070a4-to-070a5)
before upgrading an application or a driver package.

- Replace string-based `capability()` / `Capability()` declarations with typed
  `CapabilityRef[P]` references. The built-in accessors retain their names and
  now expose typed operation Protocols.
- Replace `sandloom.infra` registration callbacks with named driver classes in
  `sandloom.drivers`; custom capability specs use `sandloom.capabilities`.
  Drivers declare a contract version and their supported features.
- Remove required operations from feature requirements; explicitly accept the
  `experimental_capability` risk when using experimental capabilities in production.
- Install a real driver package before selecting the removed S3/GCS/YC placeholders.

### Developer experience

- Document the framework's vocabulary in a glossary and use one name per concept:
  "standard module" for identity, tenancy and billing, "driver" for a backend
  implementation. Add guides to write a driver and to customize a standard module.
  Reorganize the navigation into Learn, Concepts, Build, Operate, Reference and
  Project. Remove the compatibility page `docs/CONTRACTS.md`.
- Add `sandloom.driverkit`, the API for driver packages, with the `notification`
  contract and the kit `sandloom.driverkit.testing.NotifierConformance`. Modules may not
  import it, or an installed driver (SANDLOOM-C004). See ADR-0011.
- Drivers are found by name: an entry point `"<capability>.<driver>"` in
  `sandloom.drivers`, imported only when `INFRA` selects it. This replaces the
  `sandloom.infra` group and `register(registry)`. A selected name with two providers,
  or a driver that cannot be used, stops startup (SANDLOOM-B021, B022), and a built-in
  driver can no longer be replaced silently.
- `cache()`, `notifier()`, `objectstore()` and `taskqueue()` return Protocols of what a
  module may call. `capability(ref, name)` and `Capability(ref, ...)` take a typed
  `CapabilityRef[P]`.
- A driver declares features once, on its class. An instance reporting others is refused.
  Required methods are no longer features: `CapabilityRequirement("cache",
  features={"incr"})` becomes unnecessary and is refused as unknown.
- An experimental capability in production needs the accepted risk
  `experimental_capability` (SANDLOOM-B023).
- Remove the `s3`, `gcs` and `yc` placeholder drivers. Selecting one without a driver
  package is the ordinary "no registered driver" error.
- The architecture explorer now answers who is linked and why. The module layer
  draws one labelled line per direction of a module pair and no longer collapses
  mutually linked modules into one column. Selecting a block or line highlights its
  links. The inspector lists what a module provides, calls, publishes and listens
  to, and who publishes and receives an event. A module filter in the contracts
  layer shows the modules on the far side of each interface. A contracts layer too
  large to read opens with a module chooser. Declarations without a provider appear
  at the top of the page. Search also finds routes and tables. `sandloom graph`
  prints unresolved declarations as a sentence rather than a raw dictionary.

### Fixed

- `sandloom generate` no longer opens a database when `SANDLOOM_TEST_DATABASE_URL`
  is set. Following the quickstart's port note failed with `Connection refused`
  before the services were started.
- The test harness refuses a URL with `localhost` and a port from 32768 up. Such a
  connection can reach itself, and the client reads its own request as the reply, which
  shows up as random test failures.
- `INFRA` no longer passes a default instance's options to a different driver.
  Selecting a plugin driver handed it the `SMTP_*` values, including the password.

### Added

- `Notification.body_fields`: the values a notification's body was built from, as
  sensitive as the body. Identity's mail fills them, so a notification driver can
  render its own templates by `data["kind"]` without parsing the text.

### Security

- Restrict shipped and generated application containers to a read-only root,
  no capabilities and no privilege gain, with a bounded private `/tmp` and
  persistent `/app/data`. Apply the same boundary to migrations, workers and beat.
- Remove nonessential runtime administration packages and SUID/SGID bits without
  forcing Debian dependency or protected-package removal. Keep package metadata
  for full vulnerability scans; add a real kernel/filesystem check to Docker CI.
- Review all 68 Debian findings individually, with repeatable component and
  authenticated zlib source/binary evidence. Keep unresolved CVEs visible;
  the evidence checker rejects new findings and invalidates stale conclusions.

- Quote restored PostgreSQL catalog identifiers, including foreign-key columns,
  before checking backups; handle quoted names without changing query semantics.
- Restrict architecture-viewer documentation links to HTTP/HTTPS and retain
  text-only rendering of imported model labels.
- Extract local CI source archives with Python's `data` filter, rejecting
  escaping links and special files. Keep profiler output private and run its
  container as the operator; quote benchmark catalog identifiers.
- Remove vulnerable admin build dependencies `uri-js` and `braces`. Replace
  Ajv's URI parser with `fast-uri` and guard picomatch's repeated-wildcard
  extglobs with a version/hash-pinned install patch and timeout regressions.
  Snyk still reports picomatch's version advisory; the mitigation is documented
  without a scanner ignore. Record the complete Snyk finding review.
- Apply pinned Alpine security backports for brotli, curl, musl and zlib in
  the admin runtime image, retaining its unprivileged user. Record unresolved
  Debian runtime advisories separately from fixed application dependencies.

## [0.7.0a4] — 2026-10-31

### Developer experience

- Add `sandloom graph` for a compact project map and `sandloom context MODULE`
  for module purposes, public signatures, money risk, dependencies and owned
  file navigation. Text is the default; JSON exposes stable IDs and evidence.
- Add `sandloom explore` to generate and open a standalone architecture viewer.
  Show module/interface/infrastructure layers with obstacle-aware arrows,
  separate disconnected modules, readable scale and scoped filters.
- Inspect declared saga sequences, compensation and states, framework route
  lifecycles, interface impact and architecture differences against saved
  indexes or local Git revisions. Explain concrete changed declarations.
- Ship the module-context agent skill and current inspection instructions in
  generated applications. Cold graph/context reads need no app imports,
  environment configuration or backend connections.
- Align all four package versions, generated application dependency floors,
  README and documentation with `0.7.0a4` through the release synchronizer.

### Reliability

- Give the RPC compatibility self-test one overall startup deadline so a slow
  first loopback connection does not spuriously reject a working transport.
  Preserve bounded probing, guard checks and the httpx fallback.

- Classify installed modules inside a project-local environment as external
  providers so cold inspection preserves source ownership.

### Security

- Update admin build tools to fixed glob 14.0.2 and minimatch 11.0.1. Preserve
  legacy consumers with a version-specific export compatibility patch and
  install-time regression tests; keep the complete npm audit gate enabled.

## [0.7.0a3]

### Security

- Require Authlib 1.9.1 or newer in identity, update the dev virtualenv to
  21.14.13, and refresh the benchmark lock and Cachegrind export with patched
  Authlib, Pillow, certifi and joserfc. Check the export against the benchmark
  lock in CI and include all maintained dependency manifests in PR review.

### Developer experience

- Synchronize current package pins, scaffold requirements, README and documentation
  from the framework package version; fail CI, package checks and site builds
  when release references drift. Preserve historical releases and audit evidence.

- Make tenancy's initial migration install its own `citext` prerequisite on a
  fresh database, including deployments without identity.
- Fix the standalone CLI handoff for all standard batteries: defer generation
  when app dependencies are missing, print a clear installation note, and run
  generation immediately after `uv sync` in every quickstart. Validate bare,
  identity, tenancy, billing and combined applications in parallel CI jobs.

- Link framework error diagnostics, warnings and offline explanations to their
  exact sections at `sandloom.dev/docs/reference/error-codes/`.
- Update installation instructions, README badges and examples to the current
  release baseline, `0.7.0a3`.

### CI and publishing

- Make the load-client precision check independent of response-time variance;
  keep coarse-clock negative controls and verify the client with a controlled
  high-resolution clock.

- Start full serial isolation without a change-classification barrier, and
  avoid repeating additional audited child-only probes in that pass. Keep
  every in-process isolation case and all full-matrix tests. Record fixture
  measurements and primary-source comparisons of mature projects' CI.

- Use reviewed Node.js 24 Actions, with a single uv cache writer per Python key.
- Speed up child-server readiness probes and close the conformance matrix at
  the end of its test module. Avoid repeating child-process-only probes in the
  serial isolation pass; the full compatibility matrix still runs them.
- Create GitHub Releases after successful PyPI verification, using the exact
  reviewed archives, changelog notes, source commit and SHA-256 manifest.

## [0.7.0a2]

### Publishing

- Prepare all four `0.7.0a2` packages and update the generated-application baseline.
- Publish through GitHub OIDC after full validation and explicit owner approval.
  Reuse the exact validated archives, retain their hashes, resume matching partial
  uploads and verify the final PyPI files. Manual runs default to dry-run mode.

### Package metadata

- Refresh PyPI descriptions for the framework and its identity, tenancy and billing
  modules, including the AI-driven positioning, pluggable infrastructure and
  deployment model. Description changes take effect with a new PyPI release.

### CI

- Add one required merge check covering the suites selected for each PR. Run
  documentation, packages, architecture, admin and dependency review through
  the main workflow without duplicate PR triggers. Failed, cancelled or
  unexpectedly skipped checks block merging; manual full validation stays opt-in.

### Documentation

- Update contributor instructions, installation, package descriptions and
  generated application READMEs to reflect the published alpha. Clarify the
  build-only release workflow and branch-protection verification procedure.

## [0.7.0a1] — 2026-10-28

Initial public alpha release of `sandloom`, `sandloom-identity`,
`sandloom-tenancy` and `sandloom-billing`. Dependencies and generated apps
explicitly admit the alpha release. Earlier headings describe internal versions.

### Fixed

- Remove compromised emSign CA trust: require `certifi>=2026.10.19`, use
  `uv>=0.13.4` in tooling/builds and Python 3.12.16 in the production image.
- Contract DTO copies preserve matching validation/serialization aliases and
  distinguish models and enums with the same name across provider submodules.
- Automatic module discovery refuses ambiguous providers before registration.
- A fresh reviewed source export passes its own selection check without a commit.

### Breaking — `sandloom generate`; contract DTOs authored in the provider (2026-10-28)

- `sandloom generate [module ...]` writes every generated file: module, route
  and contract snapshots, contract stubs, DTO copies and exported event
  schemas, after the compatibility checks (an incompatible change, SANDLOOM-T001
  / T004 / T005 / T007 / C012, writes nothing; a weaker route policy is still
  approved only by `sandloom routes snapshot --write`). `sandloom test` never
  writes, in dev or with `--ci`: a stale file fails with "generated files are
  stale: run `sandloom generate`". Every `sandloom add` generator and
  `sandloom new` run `sandloom generate` last.
- A provider authors its contract DTOs (pydantic models, Enums) in its own
  `schemas` module. `sandloom generate` writes a wire-shape copy of each one a
  signature names into the shared DTO package (`<shared>/contracts/<module>_dto.py`,
  marked `@dto_copy`); the stub names the copy and carries its Protocol
  (`ContractRef("x.y.v1", Protocol)`). A `Contract(REF)` caller receives the
  copy, locally and over RPC; the provider receives its own class. A copy
  carries fields, types, constraints, defaults, aliases, Literal / Enum values,
  nested models and `model_config`; not methods or validators.
- SANDLOOM-C012 is restated: refused are a DTO outside the closed set of wire
  shapes or one a copy cannot carry (at `@contract`), another module's or a
  function-local DTO (at registration), a missing copy (`sandloom check`) and a
  stale or hand-edited copy (`sandloom test`). `sandloom add contract` scaffolds
  the DTO in the module's `schemas.py`.
- Contract DTO dumps on the wire use each field's alias (`by_alias=True`), so
  an aliased DTO round-trips.
- `sandloom.shared.schemas` is removed (no shims): `UserContact` moved to
  `sandloom_identity.schemas.contact` (callers: `sandloom.shared.contracts.identity_dto`),
  `MemberState` to `sandloom_tenancy.schemas.membership` (callers:
  `sandloom.shared.contracts.tenancy_dto`), the member-event ordering rule to
  `sandloom.shared.membership` and `UserContext` to `sandloom.shared.subject`.

### Breaking — module ecosystem distributions (2026-10-28)

- Eight distributions became four. `sandloom` is the framework: the kernel,
  `sandloom.sdk`, `sandloom.shared` (framework DTOs, guards, authz), the CLI
  and the composition root `sandloom.main`. `sandloom-identity`,
  `sandloom-tenancy` and `sandloom-billing` are ecosystem module packages.
  `sandloom-framework`, `sandloom-shared`, `sandloom-platform`,
  `sandloom-infra-redis` and `sandloom-infra-celery` are removed (no shims):
  depend on `sandloom[redis,celery]` or `sandloom[standard]` instead.
- Extras of `sandloom`: `redis`, `celery`, `standard` (Redis + Celery + the
  three modules, the former platform), `fast`, `check`, `testing` (pytest,
  pytest-asyncio, pytest-xdist for the test kit) and `sentry` (imported only
  when `SENTRY_DSN` is set). Third-party dependencies are ranges
  (`>=tested,<next breaking release`); exact versions live only in `uv.lock`.
  `requests` and `python-multipart` are dropped; `itsdangerous` and
  `email-validator` moved to the module packages that use them.
- The bundled modules moved to top-level import packages: `sandloom.identity`
  is `sandloom_identity` (source `src/sandloom_identity/`), likewise
  `sandloom_tenancy` and `sandloom_billing`.
- Modules register in the `sandloom.modules` entry-point group (formerly
  `sandloom.plugins`), e.g. `identity = "sandloom_identity"`. The entry-point
  name is the module name (tables `identity_*`, migration chain
  `alembic_version_identity`, channels and contracts `identity.*`); any other
  module's name is the last segment of its import path. `SANDLOOM_MODULES`,
  `MODULE_REQUIRED` and deploy profiles accept either the name or the import
  path. `MODULE_LOAD_ENTRYPOINTS=true` loads every installed module package.
- `sandloom.main:app` composes every installed `sandloom.modules` entry point
  when `SANDLOOM_MODULES` is unset; `DEFAULT_MODULES` is removed and the kernel
  names no module. `MODULE_FS_SCAN` is removed; setting it to true fails
  startup.
- A requested module name with two providers (two installed packages, or an
  installed package and an app-local module) fails startup with
  SANDLOOM-B020, naming both.
- `sandloom.shared` is unchanged and ships whole inside the `sandloom`
  distribution: the hand-written contract DTOs (`schemas/`), the generated
  stubs and event schemas of the standard modules (`contracts/`, `events/`),
  `authz/` and `guards/`. Every process installs `sandloom`, so every caller
  imports the DTOs (SANDLOOM-C012). An app's `<app>.shared` and
  `CONTRACT_DTO_PACKAGES` are unaffected.
- `sandloom new <app> [--with identity,tenancy,billing]`: the app depends on
  `sandloom[redis,celery]>=0.7,<0.8` plus `sandloom-<module>>=0.7,<0.8` per
  module; `--framework-source <path>` adds editable sources for exactly those
  distributions.
- `sandloom check`: an app module importing an installed module package, in
  any spelling, is SANDLOOM-C001. The framework tree's manifest uses
  top-level module packages (`modules_path: src`, `modules_package: ""`), the
  layout of a third-party module repository.
- `tools/packaging/check_package_distributions.py` (CI: `packages.yml`) builds
  the four wheels, checks import layering across distributions, installs
  `sandloom` alone to run every CLI subcommand, and composes the modules from
  `sandloom[standard]`. The default Docker image tag is `sandloom:local`.
- Module distributions version independently, starting at 0.7.0, and depend
  on `sandloom>=0.7,<0.8`. Release tags: `sandloom-vX.Y.Z` and
  `sandloom-<module>-vX.Y.Z`.

### Breaking — Sandloom rename

- The framework is now **Sandloom**. Python imports use `sandloom`, the CLI is
  `sandloom`, and distributions use the `sandloom-*` prefix (including
  `sandloom-framework` and `sandloom-platform`). Replace the former `aja` names
  in applications and reinstall their environment from the updated dependencies.
- Environment variables, diagnostic codes, metrics, RPC headers, Redis keys,
  PostgreSQL tenant settings and internal endpoints use the new Sandloom prefix.
  The rename includes RPC wire tags and cryptographic domain labels;
  rebuild all services together and recreate development databases/caches. Do not
  mix builds from before and after the rename. No compatibility aliases ship.
- The deployment model, module boundaries, transaction ownership, tenancy and
  public API behavior are unchanged.
- Documentation uses **Zensical** with `zensical.toml`, native search and native
  `llms.txt` / Markdown exports. Build or preview with `tools/docs/docs_site.py`.
  CI builds a documentation artifact; deployment is a separate operator action.
- The owner console uses the Sandloom mark, Manrope / IBM Plex Mono typography
  and a sand, forest and clay palette in both light and dark themes.


### Added — Public API 1.0, hand-off H0 (ADR-0010)

- `sandloom.sdk`: the public namespace skeleton (spec §2.2). Every existing name
  of the capability table is re-exported from its kernel home; the names new
  in 1.0 are typed stubs frozen to the §2.3 signatures that raise
  `NotImplementedError` naming their hand-off. Layers `_decl` (L1) and
  `_prim` (L2) with import-linter contracts (`sdk-kernel-surface`,
  `kernel-never-imports-sdk`, `sdk-decl-l1`, `sdk-layers`). The `sandloom.sdk`
  modules are public alongside today's (`architecture_rules.yml`,
  `public_api_baseline.json`).
- Lazy settings: importing the framework, `sandloom.sdk` or a bundled module reads
  no environment and builds no `Settings`; entry points force them
  (`load_settings()`, `enforce_configuration()`). `SANDLOOM_JSONSCHEMA_BACKEND` is
  now the ops field `Settings.jsonschema_backend` (read at schema compile).
  `tools/checks/import_audit.py` and `test_sdk_import_is_side_effect_free`.
- `sandloom.core.loader.manifest.AppManifest` (spec §5.7): the loader records one
  `ModuleManifest` per loaded module (`ModuleLoadSummary.manifests`);
  `create_app` publishes `app.state.app_manifest`.
- Error catalogue: every code of spec Appendix A, reserved with
  `ErrorCode.planned` naming the hand-off that implements its check
  (`sandloom explain` says so).
- `tools/packaging/public_api_migration.py`: the Appendix C scanner (no unmapped
  name), run by the architecture suite.

### Added — sagas (ADR-0008, phase 0 + phase 1)

- `sandloom.core.sagas`: a small orchestrator over the existing effect
  primitives. Definitions in `sandloom.core.descriptors` (re-exported by
  `module_api`): `Saga[InputT]`, `SagaContext[InputT]`, `step`,
  `local_step`, `StepKind`; `core.register_saga(DEF)` (adds the called
  contracts to the module's consumes; checked against the providers'
  describe documents at startup); `start_saga` / `saga_status` /
  `SagaHandle` / `SagaInputConflict` / `operation_key` in `sandloom.core.di`.
- Core migration `0010_saga_state`: `saga_instances` + `saga_steps` (tenant
  RLS with system access under `tenant_rls`). `sandloom-migrate extract-module`
  copies a module's instances.
- One advance consumer per saga module (`<module>:sagas@advance`), a
  lease-guarded timer sweep per database (`sandloom.core.beat_lease.FencedLease`,
  extracted from the beat lease), the closed outcome table
  (`sandloom.core.sagas.outcomes`), resolve-before-compensate, parking (metric +
  `core.saga.parked` audit row). Ops settings: `SAGA_SWEEP_INTERVAL_SECONDS`,
  `SAGA_SWEEP_BATCH`, `SAGA_COMPENSATION_DEADLINE_SECONDS`,
  `SAGA_RETRY_BASE_SECONDS`, `SAGA_RETRY_MAX_SECONDS`, `SAGA_RETENTION_DAYS`.
- Guarantee window / accepted risk `saga_horizon_beyond_command_dedupe`.
- `sandloom sagas list|show|retry|park|resolve`, an `sandloom doctor` `sagas` section,
  `/diagnostics/core` lists the definitions, metrics `sandloom_saga_*` and the
  `SandloomSagaParked` alert.
- `saga_harness` pytest fixture (`sandloom.core.testing.sagas`) and
  `sandloom-scaffold saga <module> <name>`.
- `examples/academy`: the worked course-enrollment saga with a
  split-conformance case. `benchmarks/ladder/saga_rung.py`.
- `sandloom.core.descriptors` is a package now (`descriptors/__init__.py` +
  `descriptors/sagas.py`); its public names are unchanged.

### Breaking (kernel internals only) — kernel layers and import contracts (ADR-0007)

- The kernel is layered and acyclic, checked by import-linter
  (`.importlinter`, `lint-imports`, pinned in the dev extras; run by
  `tests/architecture/test_kernel_import_contracts.py` and pre-commit).
  The documented public API (`sandloom.core.di`, `facade`, `module_api`,
  `descriptors`, `orm`, `projections`, `testing`, `contract_testing`,
  `sandloom.shared.*`) is unchanged; `public_api_baseline.json` and the contract
  snapshots are unchanged.
- Internal modules moved WITHOUT re-exports at the old paths (code importing
  kernel internals must follow): `sandloom.core.infra.drivers.*` /
  `infra.bootstrap` → `sandloom.core.drivers.*` / `drivers.bootstrap`;
  `eventing.consumers` / `eventing.runtime` → `consumers.callbacks` /
  `consumers.runtime`; `eventing.versioning` → `unit_of_work.versioning`;
  `eventing.transport` removed (`PoisonEventError` etc. in
  `eventing.delivery`); `migrations.migrations_cli` / `migrations.extract`
  → `cli.migrate` / `cli.extract` (`sandloom-migrate` entry point updated);
  `migrations.rls` → `sql_alchemy.rls`; `sandloom.core.database` removed (`Base`
  via `sandloom.core.orm`); `sql_alchemy.testing` → `testing.database`;
  `http.client_ip` / `http.rate_limit` → `ingress.*`;
  `loader.service_registry` → `module_api._service_registry`.
- `unit_of_work`, `idempotency`, `facade`, `sql_alchemy.planner_adapter`
  and `sql_alchemy.outbox_store` are packages split by responsibility; their
  package modules no longer re-export submodule names. Patch a kernel global
  in the module that defines it.
- Removed aliases: `idempotency._ArmedKey`, `ingress.rate_limit._client_ip`,
  `migrations.migrations.ensure_outbox_table`, `infra.config.CAPABILITIES`,
  `consumers.callbacks.ALLOW_VOLATILE_CONSUMERS_ENV`.
- The non-production default-registry fallback is gone (`sandloom.core.infra.defaults`
  removed): `di.cache()` / `di.notifier()` and every capability accessor with
  no bound registry raise in every environment, with a hint (bind one via
  `create_app()`, `sandloom.core.composition.load_composition_core()` or the
  `sandloom.core.testing` harness). `.importlinter` has no `ignore_imports`; the
  kernel has no import cycle.
- Logger names follow the new module paths for modules that log under
  `__name__` (`sandloom.core.drivers.redis_cache`,
  `sandloom.core.drivers.sqlalchemy_database`, `sandloom.core.consumers.runtime`).

### Breaking — every compatibility path removed (no deprecation period)

During this internal development milestone, old behaviour was removed instead
of kept behind modes,
aliases or warnings. Replacements: `docs/guides/upgrading-0.8.md`, section
"Removed without a deprecation period (0.8)".

- The accepted-risk boolean aliases listed in the ADR-0006 entry below
  (`BILLING_ALLOW_FAKE_PSP_IN_PROD`, `NOTIFIER_ALLOW_LOG_IN_PROD`,
  `STORAGE_ALLOW_LOCAL_IN_PROD`, `SANDLOOM_ALLOW_VOLATILE_CONSUMERS`,
  `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`, `PASSWORD_HASH_ALLOW_WEAK`,
  `JWT_ALLOW_SHARED_SECRET_SPLIT`) are removed settings, refused unless set
  to their inert value: `SANDLOOM_ACCEPTED_RISKS` is the only way to accept a
  production risk. The *migration* tier is gone (tiers are semantic + ops);
  semantic 16, ratchet ceiling 16.
- Internal RPC has one authentication, the ADR-0004 pair MAC (+ the
  caller's Ed25519 signature on commands, `RPC_ALLOWED_CALLS`):
  `RPC_AUTH_MODE` and `RPC_RESPONSE_BINDING` are removed settings (inert
  values `service_mac` / `strict`). Every `SERVICE_BINDINGS` entry pins
  `"service_id"`; a process with bindings must hold `RPC_DH_PRIVATE_KEY`;
  responses are always keyed (an unkeyed error answer is `503
  RemoteServiceUnavailable` with the provider's code). `INTERNAL_API_TOKEN`
  and `INTERNAL_ENDPOINTS_OPEN_IN_DEV` never open `/_sandloom/rpc`.
- Contracts: undecorated session-taking providers and session arguments
  are refused (`@contract_method` + `contract_session()` only);
  `rpc_idempotency_key` is removed from `sandloom.core.di`.
- `AccessFact` `claim_interpret` / `ClaimInterpreter` removed — use
  `claim_rule=ClaimRule...`.
- An access token without `iat_us` is revoked by any revocation mark of its
  subject (no whole-second comparison).
- Event consumers no longer adopt pre-namespacing group names (groups are
  `<module>:<name>` or an explicit `group=`).
- RLS: `upgrade_tenant_rls` and the legacy bypass-policy detection removed.
- `ensure_outbox_table` alias removed (`ensure_core_infra_tables`).
- Identity: no legacy SHA-256 Fernet key fallback for TOTP secrets, no 8-hex
  backup codes. Tenancy list endpoints: the `after` query parameter is
  removed (`cursor` + `X-Next-Cursor`).
- The `sandloom.core` package re-exports (`from sandloom.core import settings`, ...)
  are removed.

### Changed — kernel package splits (no behaviour change)

- `sandloom.core.security`, `sandloom.core.auth_freshness`, `sandloom.core.security_recovery`
  → package `sandloom.core.auth` (`hashing`, `tokens`, `bearer`, `marks`,
  `revocation`, `local_marks`, `verification`, `freshness`, `blocks`,
  `recovery`).
- `sandloom.core.settings` → package `sandloom.core.config` (`config.settings` with
  per-domain sections, `config.tiers` (was `config_tiers`),
  `config.accepted_risks`). The config layer imports nothing above it; the
  checks that need kernel code (asymmetric JWT key material, internal-RPC
  keys / bindings / allowlist, `multi_tenant_strict` refusals) run at boot in
  `sandloom.core.app_factory.enforce_configuration`, called first by
  `create_app` and reported by `sandloom doctor` as `config/boot`.

### Breaking — accepted risks replace prod opt-outs; single-valued flags removed (ADR-0006)

- New `SANDLOOM_ACCEPTED_RISKS` (JSON or comma list of stable risk ids,
  `sandloom.core.accepted_risks`): the one place a deployment accepts a weaker
  guarantee. Ids: `fake_psp_in_prod`, `log_notifier_in_prod`,
  `local_storage_in_prod`, `volatile_event_bus`, `unversioned_ddl_repair`,
  `weak_password_hash`, `shared_hs_secret_split`, `relay_disabled`. An
  unknown id fails startup; every accepted risk in effect is logged at every
  start, exported as `sandloom_accepted_risk_active{risk}` (and
  `sandloom_config_override_active{kind="accepted_risk"}`), listed by
  `/diagnostics/core` (`configuration.accepted_risks`) and `sandloom doctor`.
- Deprecated (removed in 0.9), still honoured as aliases of their risk id:
  `BILLING_ALLOW_FAKE_PSP_IN_PROD`, `NOTIFIER_ALLOW_LOG_IN_PROD`,
  `STORAGE_ALLOW_LOCAL_IN_PROD`, `INFRA.eventbus.<name>.allow_volatile`,
  `SANDLOOM_ALLOW_VOLATILE_CONSUMERS` (outside prod), `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR`,
  `PASSWORD_HASH_ALLOW_WEAK`, `JWT_ALLOW_SHARED_SECRET_SPLIT`.
- `OUTBOX_RELAY_ENABLED` is semantic: `off` in a prod app process is refused
  at startup unless `relay_disabled` is accepted. Gunicorn non-leader
  workers now run with the new value `delegated` (was `off`).
- Removed, with their code paths: `LEGACY_UNSCOPED_EFFECTS`, `DB_LAZY_BEGIN`
  (`sandloom.core.sql_alchemy.lazy_begin` is now `read_only_policy`), the
  `INFRA.database.<name>.verify_roles` option, `SCHEMA_VALIDATION_SOFT`
  (off-schema events are refused in every environment), `MODULE_REQUIRE_LISTED`,
  `BEAT_LEASE_ENABLED`, `CONTRACT_READ_SCOPE_MEMO`,
  `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS` (and `FakeStripeClient`'s
  `allow_unsigned_webhooks`). A removed setting still set to its inert value
  logs a warning; any other value fails startup.
- `BILLING_DENY_ON_PAST_DUE` moved to the ops tier (product policy).
- Tiers: semantic 30 → 18 (ratchet ceiling 18), migration 3 → 8 (the
  aliases), ops 179 → 179. Upgrade steps: `docs/guides/upgrading-0.8.md`.

### Added — durable HTTP idempotency and guarantee windows (ADR-0005 amendment)

- `idempotency_guard(..., durable=True)`: the record is claimed in the
  request's own `DbSession` Unit of Work (new core table
  `http_idempotency_log`, migration `0009_http_idempotency_log`, additive)
  and completed with the response right before `COMMIT`, so it commits iff
  the endpoint's effect commits: replay of the stored response, `422` on a
  reused key, `409` at once while in flight, no record after a rollback, a
  returned `5xx` never replayed, an ambiguous COMMIT never double-applies.
  Third adapter of the shared dedupe log; expired records are purged by the
  relay housekeeping / `sandloom outbox purge` and copied by `extract-module`.
- **Breaking (startup check)**: `create_app` refuses an
  `operation_class("F")` route whose idempotency guard is the
  response-storing cache form or fail-open (`FinancialGuardError`). Use
  `durable=True`, or `store_response=False` + `fail_closed=True` when the
  endpoint owns its durable dedupe record. The bundled F routes already
  comply (no change).
- `sandloom.core.guarantee_windows`: production refuses (other environments
  warn) `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` below the relay's redelivery
  horizon; `sandloom outbox purge --inbox-days` refuses a cutoff inside
  `EVENT_STREAM_RETENTION_SECONDS` + 1 day unless `--force`. Each problem has
  a stable risk id (`command_dedupe_below_retry_horizon`,
  `inbox_purge_below_redelivery`).
- ADR-0005 wording: at-most-once (was best-effort) for `on_commit` /
  unscoped publish / `required=False` audit; exactly-once-effect scoped to the
  recording database; replay-only = response dedupe; ordering, the
  ambiguous-COMMIT table and the stage-3 hand-off documented.
  `effects.Guarantee.AT_MOST_ONCE` (`BEST_EFFORT` is now its alias).

### Changed — one effect model (ADR-0005)

- `docs/adr/0005-effect-model.md` defines effects, boundaries, guarantee
  levels, the dedupe record and the commit pipeline, and maps every
  mechanism (outbox + relay, durable / cache consumer inbox, command dedupe,
  `idempotency_guard`, enqueue dedupe, `on_commit`, `before_commit`,
  `revocation_at_commit`, `record_audit`) to boundary x guarantee.
- New `sandloom.core.effects` (guarantee levels, record statuses, bounds, key /
  args digests, replay encoding) and `sandloom.core.sql_alchemy.dedupe_log`: the
  one relational claim / complete / replay / purge implementation.
  `durable_inbox` (`consumer_inbox`) and the contract command log
  (`contract_command_log`) are now adapters over it — same tables, same SQL,
  same public surface. The cache dedupe forms take their bounds, tags and
  digests from `sandloom.core.effects`.
- `unit_of_work.COMMIT_PIPELINE` documents the stages every UoW ends through
  (before_commit -> COMMIT -> outbox dispatch -> on_commit); the test seams
  (`get_db` override, `sandloom.core.testing.run_on_commit`) now run the same
  stage function instead of re-assembling it.
- The ADR-0005 rule for new effect mechanisms is enforced in review; a
  heuristic `sandloom check` rule (`effect-primitives`) was tried and removed —
  `sandloom check` keeps only deterministic rules.

### Added — configuration tiers and profile-driven semantic settings

- Every setting is classified in `sandloom.core.config_tiers` as **semantic**
  (changes a guarantee: 30, incl. two `INFRA` driver options), **ops**
  (capacity / operations: 179) or **migration** (transitional, with a removal
  version: 3). A new `Settings` field fails `test_config_tiers.py` until it is
  classified; the semantic count is ratcheted (it may only go down, a new one
  needs an `adr=` reference).
- The semantic settings' effective values are defined per profile (`dev`,
  `test`, `prod`, `prod` + `PRODUCTION_PROFILE=multi_tenant_strict`) in one
  table, generated as `docs/reference/configuration-tiers.md`. The table is
  today's behaviour — no default, refusal or runtime decision changes.
- An *override* (a semantic setting moved towards its weaker end, a
  deprecated value, an active migration flag) is now visible: a WARNING at
  every production start (INFO for a deliberate dev opt-in), the gauge
  `sandloom_config_override_active{setting,kind}`, `configuration.overrides` in
  `/diagnostics/core`, and one `config/override:<setting>` check in
  `sandloom doctor` (WARN in prod: exit code 1).
- Migration flags carry `removed_in` and a test fails once the package
  version reaches it: `LEGACY_UNSCOPED_EFFECTS`, `DB_LAZY_BEGIN` and the
  `INFRA.database.<name>.verify_roles` option are due in 0.8.

### Deprecated — RPC auth modes `shared_token` / `dual`, `RPC_RESPONSE_BINDING=permissive`

- Review-map D-14: `RPC_AUTH_MODE=shared_token` and `dual`, and
  `RPC_RESPONSE_BINDING=permissive`, are removed in 0.9 (target:
  `service_mac` + `strict`). A process that calls internal RPC
  (`SERVICE_BINDINGS`) — or, in prod, serves it with `INTERNAL_API_TOKEN` —
  logs `DEPRECATED: … is removed in 0.9` at startup, in every environment
  (previously only `shared_token`, only in prod). It is a logged warning plus
  the gauge above, deliberately NOT a Python `DeprecationWarning`: those are
  errors in the framework and app test suites (`filterwarnings`), so a
  default-config app would fail its tests on an operator decision.
- `sandloom.core.app_factory.warn_shared_token_rpc` is replaced by
  `warn_configuration_overrides` (returns the overrides it logged).

### Changed — `sandloom.core.rpc` is a package (internal, behaviour-neutral)

- The 3,200-line `sandloom/core/rpc.py` is split into `sandloom/core/rpc/`: `proxy`,
  `client`, `outbound` (caller), `server`, `inbound_auth`, `dispatch`
  (provider), `context`, `binding`, `envelope`, `wire`, `errors` (both
  sides) and `readiness`. Nothing changes on the wire; every
  `from sandloom.core.rpc import X` keeps working.
- Tests that monkeypatch an RPC internal patch it where it is looked up
  (`sandloom.core.rpc.proxy._rpc_client`, `sandloom.core.rpc.server._unpack`, …):
  setting such a name on the `sandloom.core.rpc` package itself raises
  `AttributeError` naming the right target, instead of silently patching
  nothing.

### Fixed — read scope recovers from an aborted shared transaction

- A local read contract that swallowed a database error left the request's
  shared read connection (`ContractReadScope`) in an aborted transaction, so
  every later local read contract on that database in the same request
  failed with "current transaction is aborted" (SQLSTATE `25P02`). The scope
  now drops that connection when a call on it hits `25P02` and reruns the
  call in its own Unit of Work (what it would have done without the scope);
  later calls share a fresh connection. Counted as `stats["replaced"]`.

### Changed — `sandloom.core.module_api` is a package

- `module_api.py` is split into internal submodules (`sandloom.core.module_api._*`:
  registration, `define_module`, contract method / runtime / dedupe, read
  scope, schema, provider). The public surface is unchanged —
  `from sandloom.core.module_api import X` works for every previous `X`. Business
  modules must not import the `_` submodules (`architecture_rules.yml`
  `forbidden_patterns`).

### Added — opt-in idempotency cache instance (write-path p99)

- `IDEMPOTENCY_CACHE_INSTANCE` (default empty = unchanged: records stay in
  `SECURITY_CACHE_INSTANCE`) moves idempotency records — `idempotency_guard`
  reservations / replays / releases, task-enqueue dedupe
  (`taskqueue().enqueue(..., idempotency_key=...)`) and the cache fallback of
  contract-command dedupe — to their own named cache instance. Revocation
  marks, emergency blocks and the RPC replay guard stay in the security
  cache. Rationale: on a security Redis with `appendfsync always` the
  idempotency writes' fsyncs queue the hot revocation-mark reads
  (`docs/design/write-login-profile-2026-10-22.md`). Run the instance with
  the security Redis's durability (`appendfsync always`, `noeviction`);
  `fail_closed=True` guards still answer `503` when it is down. New
  accessors `sandloom.core.infra.access.idempotency_cache()` /
  `idempotency_cache_name()`.
- `PRODUCTION_PROFILE=multi_tenant_strict`: a set idempotency instance must
  be declared in `INFRA["cache"]` on a Redis server other than the default
  cache's (`Settings` validator; `default` is refused, sharing the security
  Redis's server warns) and is attested as a separate Redis process at
  startup, like the security cache.
- Both compose profiles carry a commented-out `idempotency-redis` service and
  env block; `deploy/README.md` → "Separate idempotency Redis (opt-in)".
  `sandloom backup plan` labels the idempotency Redis's role.

### Added — operator tooling (review map H-10, C-05, H-22, H-08)

- **Beat singleton lease** (`sandloom.core.beat_lease`): the framework beat
  (`celery -A sandloom.core.worker beat`, and `worker -B`) schedules only while it
  holds a fenced lease `sandloom:beat:lease:<scope>` in the cache (`SET NX` + TTL,
  renewed by compare-and-set on its own token every TTL/3). A second beat on
  the same broker and module set stands by and takes over when the holder
  releases or dies; a holder that lost its token stops at once. New settings
  `BEAT_LEASE_ENABLED` (default `true`), `BEAT_LEASE_TTL_SECONDS` (30),
  `BEAT_LEASE_NAME`, `BEAT_LEASE_CACHE_INSTANCE`. **Behaviour change:** beat
  refuses to start (exit 1) when that cache lacks the `cas` feature; set
  `BEAT_LEASE_ENABLED=false` to opt out.
- `sandloom-migrate upgrade-all|upgrade-core|upgrade-module --lock-timeout SECONDS`
  bounds the wait for a concurrent run's advisory lock (default
  `MIGRATION_LOCK_TIMEOUT_SECONDS`). While waiting the runner logs the holder
  (pid, `application_name`, user, client, query start — `pg_locks` +
  `pg_stat_activity`) every 5 s, and the timeout error names it. Runners now
  connect with `application_name='sandloom-migrate'`. `migration_lock()` takes
  `timeout=`.
- `sandloom config dump [--json] [--only PREFIXES] [--non-default]`: the effective
  settings with the source of each value (env / dotenv / secrets_dir /
  default); credentials redacted by name and type, URL passwords masked,
  recursively inside `INFRA` / `SERVICE_BINDINGS`. `--show-secrets` is
  refused with `ENVIRONMENT=prod`.
- `sandloom doctor [--json] [--skip SECTIONS] [--timeout S] [--lag-warn N]`:
  read-only preflight — config sanity (incl. `multi_tenant_strict`), the
  composition root and consumed-contract bindings, health of every
  configured infra instance, applied vs code migration heads per database
  (`sandloom.core.migrations.migrations.migration_status`, read-only
  connections), Streams consumer groups (pending / lag) and RLS role /
  coverage checks. Exit `0` ok, `1` warnings, `2` failures; never prints a
  secret. Runbook: `docs/operate/operator-reference.md` → Preflight and effective
  configuration.

### Added — supply-chain gates (review map A-30)

- CI job `supply-chain` (`tools/checks/supply_chain.sh`, also runnable via
  `tools/ci/ci_local.sh --jobs supply-chain`): CycloneDX SBOM from `uv.lock` as
  the `backend-sbom` artifact, plus a PR dependency-review workflow. SAST is
  covered by ruff `S`. See `docs/contributing/supply-chain.md`.
- Redis script SHA-1 marked `usedforsecurity=False` (bandit B324; no behaviour change).

### Changed — static analysis ratchet (ruff)

- ruff now also enforces `PLC0415`, `BLE`, `S` (bandit), `ASYNC`, `SIM`,
  `PERF`, `C901` (max-complexity 10), `RUF` (incl. RUF100: dead `# noqa`
  fails) and `PGH004` (no blanket `# noqa`). Existing violations are
  baselined as per-line markers; behaviour is unchanged.
- The 395 `# noqa: WPS433` markers (wemake-python-styleguide never ran) are
  now `# noqa: PLC0415`; 167 dead markers of tools that never run (ANN, SLF,
  A, N, D, PT, WPS…) or that suppressed nothing are gone, their reasons kept
  as plain comments. Every `BLE001` and bandit marker carries a one-line
  reason.
- New architecture test `tests/architecture/test_lint_suppression_ratchet.py`:
  the number of markers per ratcheted code may only go down, and the
  config-level ignores of those rules are pinned.
- The kernel package `sandloom/core/migrations` (migration runner, `sandloom-migrate`,
  `sandloom backup`, `extract-module`) is linted and formatted for the first time —
  the `src/sandloom/*/migrations` exclude meant for generated revisions also
  matched it; only `*/migrations/versions` is excluded now.
- Safe simplifications applied (collapsed ifs, a ternary, explicit and/or
  parentheses, literal unpacking, `_`-prefixed unused unpacked names).
- Policy and how to pay suppressions down: `docs/contributing/lint-policy.md`.

### Changed — pool checkout fairness (Oracle 2026-10-23 read tail)

- `LingeringQueuePool` fair checkout now uses **bounded barging**
  (`DB_POOL_FAIR_BARGE_MS`, default 25): while the oldest waiting checkout
  has waited less than the bound, a returned connection goes back to the
  idle queue and wakes that waiter, and a caller already running may take it
  first. Past the bound the connection is handed straight to the waiter and
  newcomers queue, so starvation stays bounded. `0` restores the strict FIFO
  handoff of 2026-10-22, whose reserved connections idled until their
  waiter's task resumed (mean 6.7 ms on Oracle). Timeout, cancellation,
  room-wake-up and `sandloom_db_pool_checkout_waiters` semantics are unchanged.
  `tools/checks/bench_pool_checkout.py --e2e` adds an end-to-end stand (real CPU,
  `--cpu-clock`, `--stall-ms`). Re-measure on Oracle before relying on it:
  `docs/design/pool-checkout-fairness-2026-10-22.md`.

### Garbage collector tuning for server processes

- **Behaviour change (default on):** gunicorn workers (`sandloom.core.cli.gunicorn_conf`)
  and the Celery worker (`sandloom.core.worker`) now configure CPython's cyclic GC
  once per process (new module `sandloom.core.gc_tuning`):
  - gunicorn workers: when the app reports `lifespan.startup.complete`, before
    the worker accepts traffic.
  - Celery: in the parent before the pool forks.
  - The configuration: `gc.collect(); gc.freeze()` (`GC_FREEZE_AFTER_STARTUP`,
    default `true`), then `gc.set_threshold(2000, 10, 10)` (`GC_THRESHOLDS`,
    default `2000,10,10`, CPython 3.13+'s own default; empty = interpreter
    default; skipped on CPython 3.14.0–3.14.4).
  - Local stand (identity + bench, 2 workers, Python 3.12): a full collection
    drops from ~35 ms to ~2 ms of CPU, GC from 4–6% to ~0.2% of worker CPU,
    and the median calm-host client p99 from 48 ms to 12–14 ms.
  - `create_app` itself never touches the collector.
  - Restore the old behaviour with `GC_FREEZE_AFTER_STARTUP=false GC_THRESHOLDS=`.
  - Details: `docs/design/gc-tuning-2026-10-23.md`, `docs/operate/operator-reference.md`
    → *Garbage collector tuning*.
- **New metrics** (`GC_METRICS_ENABLED`, default `true`, gunicorn workers):
  - `sandloom_gc_pause_seconds{generation}` (histogram)
  - `sandloom_gc_collected_objects_total{generation}`
  - `sandloom_gc_frozen_objects`
  - The `gc.callbacks` hook never calls prometheus_client inside a collection
    (its value lock is not reentrant). It fills a preallocated ring that the
    event loop flushes once per second.
- **New benchmark stand** `benchmarks/ladder/gc_stand.py` (+ `gc_stand_conf.py`,
  `gc_stand_report.py`): the shipped gunicorn config with a per-worker GC pause /
  thread-CPU probe under open-loop load.

### Enforced promises (review map A-03, A-23, I-19)

- **Breaking (startup):** `create_app` refuses (`GuardOrderError`) a route
  whose `idempotency_guard` FastAPI solves before its authenticating
  dependency — the guard would reserve / replay for a caller the route has not
  authenticated. Declare the guard after the auth (an endpoint parameter after
  `Depends(get_current_user_id)`). Authenticating dependencies are
  `sandloom.shared.authz.current_context` (so `get_current_user_id`, `require(...)`,
  `authorize_resource`) and the platform console's operator guard; mark a
  custom one with `@authenticates` (`sandloom.core.di`). Fixed in the `helpdesk`
  example (ticket create) and the benchmark `/dw/7` route.
- **New `sandloom check` rule `tenant-scoped-models`:** an ORM model or Core
  `Table` declaring `tenant_id` must inherit `TenantScoped`, or be listed under
  the new manifest key `tenant_scope_exempt` (`table` + required `reason`;
  stale entries fail). `billing_operation_intents` is exempt (its `tenant_id`
  is the billing subject, C-13 policy). The `projecthub` taskboard projection
  and project snapshot are now `TenantScoped` with RLS (migration
  `0004_taskboard`); under `tenant_rls` an unscoped `rebuild_all` / cold
  `rebuild_entity` sees no rows — rebuild with `--tenant`.
- **New gate:** every bundled module boots alone through `create_app` +
  lifespan (`src/sandloom/core/tests/test_module_boots_alone.py`), refusing only
  with the documented `ContractBindingError`; new test seam
  `sandloom.core.testing.boot_module_alone` / `BootOutcome` / `ModuleBootError`,
  used by each example app.
### Recovery — backups, security-state reconciliation, incident commands

- `sandloom backup plan` lists what to back up per database instance (with the
  chains it hosts) and per Redis endpoint (security cache, event-bus streams,
  task broker), plus object store and secrets, with backup and restore steps.
- `sandloom backup verify <dump>` restores a `pg_dump` into a throw-away database,
  checks every hosted migration chain is at its head, core tables, index /
  constraint validity and foreign keys, reports the newest audit row, drops it.
- `sandloom backup reconcile-security`: after restoring the security Redis from an
  older snapshot, re-derives emergency blocks (newest `auth.user.blocked` /
  `.unblocked` audit row per user, or an off-site `--audit-export` file) and
  revocation marks (`core.auth.tokens_revoked.v1` outbox rows inside the mark
  lifetime). A stale snapshot no longer silently unblocks a user. Exit code is
  the traffic gate of the restore runbook.
- `sandloom security revoke-sessions | force-password-reset (--all | --tenant |
  --user)` and `sandloom security audit-export`: dry-run by default, `--apply`
  needs `--reason`, production also `--confirm-production`; batched, durably
  deduplicated (`--operation-id` resumes), audited per user
  (`actor_type="operator"`).
- identity: new `identity.security.v1` contract (`list_user_ids`,
  `revoke_sessions`, `require_password_reset`); `users.password_reset_required`
  (migration `0008_identity`, additive) — a password sign-in of such a user
  answers `403` with `X-Password-Reset-Required: true` until the reset flow or
  a password change clears it.
- identity: `POST /api/v1/users/{user_id}/reset-2fa` — class `A` operator
  reset of a lost second factor (platform admin, not on oneself,
  `Idempotency-Key` required, audited `identity.twofa.reset`, ends every
  session).
- tenancy: `tenancy.membership.v1.list_member_user_ids` (additive read).
- `OutboxStore.list_channel_since` (non-abstract; SQLAlchemy implements it).
- docs/operate/operator-reference.md: "Backups & restore" rewritten with the restore order and
  traffic gate; new "Security incident runbook" (database leak, credential
  leak, key compromise).

### Added — operational metrics (review map B-07, H-09, C-07, H-14)

- Delivery lag: `sandloom_outbox_oldest_pending_age_seconds{database}` (relay, one
  indexed `min(emitted_at)` every 10 s, also while the broker circuit is
  open), `sandloom_streams_group_oldest_unacked_age_seconds{stream,group}` (group
  heartbeat), `sandloom_event_end_to_end_latency_seconds{consumer}` (`emitted_at` →
  handler success) and `sandloom_event_consumer_handler_duration_seconds{consumer}`.
- Database pools: `sandloom_db_pool_connections{database,pool,state}`,
  `sandloom_db_pool_max_connections`, `sandloom_db_pool_overflow` and
  `sandloom_db_pool_checkout_waiters` (the fair-gate queue), sampled every 5 s off
  the request path; `DatabaseProvider.pool_stats()` on the SQLAlchemy driver.
- `sandloom_cache_operation_errors_total{instance,operation}` (Redis cache driver),
  `sandloom_auth_revocation_check_duration_seconds{mode}`,
  `sandloom_rate_limit_rejected_total{route}` /
  `sandloom_rate_limit_unavailable_total{outcome}`,
  `sandloom_rpc_client_request_duration_seconds{peer,outcome}` and
  `sandloom_http_untrusted_forwarded_headers_total` (`X-Forwarded-For` while
  `TRUST_PROXY_HEADERS` is off).
- New optional store / transport hooks with `None` defaults:
  `OutboxStore.oldest_pending_age_seconds()` and
  `MessageTransport.oldest_unacked_age_seconds()`.
- `docs/operate/operator-reference.md`: metric reference and a suggested alert table. All new
  gauges are Gunicorn-multiprocess safe (`livesum` / `livemostrecent`); a test
  now requires a `live*` mode on every registered gauge.

### Security and correctness — 22 October review remediation

- Harden tenant ORM/Core boundaries, SSO step-up and concurrent unlink/reset,
  revocation-aware idempotency replay, strict RPC response binding, transport
  limits/deadlines and HTTP authority isolation. Preserve DB-atomic financial
  dedupe and live fail-closed revocation checks.
- Isolate malformed Streams entries, targeted replay and pending ownership;
  renew handler leases, bound reconnect/drain behavior and preserve outbox
  provenance. Add versioned projection tombstones and repair task shutdown.
- Aggregate Gunicorn metrics correctly; align shutdown grace, independent
  health probes, broker/relay readiness, persistent deployment storage and
  safe operational logs. Retire unsafe prototype lazy autocommit reads.
- Repair package sources, generated typing/Compose, key generation, test
  resource admission, secret detection and API-baseline coverage.
- Licensing: no open-source license is granted. The MIT statement in
  `backend/README.md` was removed; the distributions carry no `LICENSE` file
  or `license` metadata (all rights reserved).
- Apply additive core migration `0008` before deployment. Review
  the archived hardening upgrade notes for
  intentional security compatibility changes; current requirements are in the
  [security model](https://sandloom.dev/docs/advanced/security-model/). Historical benchmarks do not
  qualify this new artifact; external credential rotation remains separate.
- Outbox relay: a failed / broker-parked row holds the rows behind it on its
  channel (no attempt counted) so a later event never overtakes an earlier one
  parked in backoff.
- Task enqueue: a send that provably never reached the broker (payload not
  serializable, unknown task) releases the idempotency reservation instead of
  leaving it pending forever.
- Redis cache: tests pin that non-idempotent commands (INCR, Lua scripts,
  `SET NX`) are sent once on a lost reply (the stock redis-py client); they
  fail if an upgrade starts re-sending commands.

### Changed — billing reads never write (behaviour change)

- `GET /api/v1/billing/subscription` and `GET /api/v1/billing/credits` are
  pure declared reads (execution planner, a `READ ONLY` transaction). They no
  longer create the tenant's billing customer (and its zero balance row) on
  first use: a tenant without a customer answers exactly what a freshly
  created one did — `{"status": "none", "current_period_end": null,
  "trial_end": null, "cancel_at_period_end": false, "plan": null}` /
  `{"balance": 0, "debt": 0, "in_debt": false}` — and nothing is written.
  The customer is created by `POST /checkout-session` / `POST
  /customer-portal` (the operation intent), which every purchase, grant and
  webhook goes through. Observable elsewhere: a tenant that never started a
  checkout has no row in `/api/v1/platform/billing/customers` (nothing to
  adjust), and a token minted for a user without a customer carries no
  `subscription_status` claim (it carried `"none"` once a `GET` had run);
  every claim rule (`Subscribed`, `subscription.active`) denies both alike.
  The `billing.entitlements.v1` / `billing.credits.v1` contracts already
  answered "no customer" as false / 0 / `{}` / 402 and are unchanged.

### Added — execution planner

- `JoinRead` may be keyed on the joined row only (`by=()`) when `join_by` /
  `join_where` pin it to one row (a unique key of `join`) and `via` is unique
  on the entity; anything that could return several rows is refused.
- `ListOperation(optional={arg: column}, prefix={arg: column},
  prefix_ignore_case=...)`: equality / literal-prefix (`LIKE` / `ILIKE`)
  filters applied only when the argument is not `None`, on both executors;
  one statement per combination of present filters (at most four filters),
  residual to the covering-index rule. The cursor is bound to every argument.
- projecthub's `GET /api/v1/taskboard/tasks` runs on the planner
  (`taskboard.rows.recent`, migration `0003_taskboard` adds
  `ix_taskboard_ptqi_tenant_updated`); a prefix's `%` / `_` are now matched
  literally, and equal `updated_at` values are ordered by `task_id` DESC.

### Added — test API

- `sandloom.core.testing` seams for module tests that previously reached kernel
  internals: `use_security_cache`, `revocation_mark_key`, `block_mark_key`,
  `record_revocation_checks`, `stop_revocation_listener`, `set_environment`,
  `operation_classes` / `route_policy` (`RoutePolicy`), and migration-test
  helpers `temporary_database` (fixture `scratch_database`),
  `upgrade_core_chain`, `migrate_module`, `run_sql`. `revoked_tokens.available
  = False` simulates an unreachable revocation store.

### Changed — tests

- The bundled identity / tenancy / billing tests (and the helpdesk example)
  pass the `test-imports` whitelist with no allowlist entries: billing seeds
  organizations through tenancy's HTTP API, conformance tests take the
  `sandloom_core` / `sandloom_app` fixtures, and the error-handler / rate-limit unit
  tests moved to the kernel suite. The `sandloom check` help lists every rule.

### Added — pair-MAC internal RPC authentication (ADR-0004)

- `RPC_AUTH_MODE=service_mac` (target for split deployments) and
  `service_mac_dual` (migration from `service_identity`): every RPC request
  carries `X-SANDLOOM-RPC-Auth`, an HMAC-SHA256 under a per-pair, directional key
  HKDF-derived from the two services' X25519 keys (`sandloom.core.rpc_pair_mac`).
  Audience-bound (a captured request cannot be replayed to another service),
  per-attempt nonce with a replay guard (per process, plus a `SET NX` in the
  security cache for commands: `RPC_REPLAY_GUARD`), freshness window
  `RPC_AUTH_MAX_SKEW_SECONDS` (default 60 s). Per read call ≈ 31 µs → 3 µs on the
  caller and ≈ 95 µs → 8 µs on the provider (measurements in the ADR).
  Rotation via `RPC_DH_EXTRA_PRIVATE_KEYS`; `sandloom keys generate --purpose
  rpc-dh`. The microservices profile and the split conformance matrix run
  `service_mac`. Metric `sandloom_rpc_auth_rejected_total{reason}`.
- Key-compromise impersonation mitigation: in `service_mac` COMMAND calls
  also carry the caller's Ed25519 signature
  (`RPC_COMMANDS_REQUIRE_SIGNATURE`: `auto` = on in prod, `on`, `all`,
  `off` — refused under `multi_tenant_strict`), so a leaked provider X25519
  key can forge reads at most, never effects. Stale
  `RPC_DH_EXTRA_PRIVATE_KEYS` are warned about (refused under the strict
  profile); the signed context carries the attempt's nonce; per-caller
  replay quota (`RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES`, default half the
  table); metric `sandloom_rpc_auth_clock_skew_total{caller}`; `RPC_REPLAY_GUARD=local` is
  refused in prod; `HEAD` on the RPC status route is `405`.
- Ed25519 identity contexts carry `aud` when the binding pins its provider;
  a provider refuses a context addressed to another service.

### Changed — internal RPC error mapping

- A provider's identity / authorization rejection (`rpc_identity_*`,
  `rpc_caller_forbidden`, `rpc_auth_*`, `rpc_replay_unavailable`) now reaches
  the caller as `503 RemoteServiceUnavailable` with that code, never as the
  provider's 401 / 403 (a deployment problem is not the end user's).

### Changed — dependencies

- pydantic 2.14.1 / pydantic-core 2.50.1, pydantic-settings 2.16.1,
  SQLAlchemy 2.1.5, authlib 1.9.1 / joserfc 1.7.7, hiredis 3.5.0, httptools
  0.9.0, websockets 17.3, sentry-sdk 2.72.0, jsonschema-rs 0.59.0 (`fast`
  extra); dev tools ruff 0.17.1, pytest-mock 3.17.0, pre-commit 4.7.0 (also
  the `sandloom new` dev pins); docs mkdocstrings 1.0.8 / mkdocstrings-python
  2.0.10, pymdown-extensions 12.3. SQLAlchemy 2.1.5's stock async queue now
  takes an idle connection without `wait_for` too; the linger-pool tests
  accept both behaviours.

- Preserve Boolean parameter coercion, deferred raiseload and root discriminator
  handling through conservative planner fallback to the ORM.

### Changed — production execution planner

- Eligible declared reads use the optimized PostgreSQL/asyncpg executor by
  default. `PLANNER_OPTIMIZED_EXECUTOR=false` restores the general executor.
- Automatic ORM fallback preserves custom column processors, application
  listeners, eager loading and nonunique single-row cardinality checks. Cached
  plans also respect policies installed after preparation.
- All optimized reads use READ ONLY transactions, including databases without
  tenant RLS. Nullable keyset ordering is refused and partial indexes no longer
  prove unconditional uniqueness or covering order.

### Fixed — release qualification

- PostgreSQL connection establishment uses a finite timeout and capped
  exponential backoff with equal jitter after transport failures. One recovery
  probe per process/pool prevents queued requests from reconnecting together;
  connection failure returns 503 and never replays SQL or a transaction.
- FIFO admission for contended database checkouts prevents new arrivals from
  starving existing waiters; one timeout covers admission and queue waiting.
- Identity propagates infrastructure outages instead of reporting bad tokens
  or silently treating authenticated callers as anonymous.
- Planner cleanup invalidates cancelled connection returns and releases a lease
  when read-unit setup fails.

### Fixed — framework review hardening

- Self-contained source distributions for all eight packages, with source-to-wheel
  roundtrip validation and clean-environment installs.
- Atomic enqueue deduplication using the security cache: replay successful task
  IDs, reject conflicting parameters, and preserve ambiguous reservations for
  reconciliation. Task effects still require DB-atomic idempotency.
- Matching local/RPC read memo policy, isolated mutable results, and invalidation
  that cannot be undone by an in-flight read.
- Fail closed on missing architecture module directories; export the documented
  `db_uow` fixture; reject keyword/invalid scaffold names; generate RLS policies
  only when the target database enables the matching runtime hook.
- Release publishing waits for complete CI and package checks, verifies tag
  versions, and blocks on dependency audit failures. Coverage includes the kernel.


## [0.7.0] — 2026-10-13

Security, billing-correctness and split-deployment release with **breaking
changes** (pre-1.0: a minor may break; `docs/contributing/versioning.md`). It
contains everything merged after 0.6.0 (internal change #39 and internal change #41–internal change #87; #80 is an open issue, #82 is this release). Every
distribution (`sandloom-framework`, `sandloom-shared`, `sandloom-identity`, `sandloom-tenancy`,
`sandloom-billing`, `sandloom-infra-redis`, `sandloom-infra-celery`, `sandloom-platform`) is
versioned at 0.7.0 as an internal baseline (not published to PyPI), and `sandloom new` now pins
`>=0.7.0`.

### Upgrade notes (0.6.0 → 0.7.0)

The historical 0.7 step-by-step guide is archived.
For the current source API, use [Upgrading to Public API 1.0](https://sandloom.dev/docs/operate/upgrade-1.0/).
In short:

1. Run `sandloom-migrate upgrade-all --apply` — new core-chain revision
   `0006_contract_command_log` (every hosting database), tenancy `0004`–`0005`,
   billing `0004`–`0009`. Billing `0005` changes who is entitled (see
   "Billing"): list legacy subscriptions with no catalog price BEFORE
   deploying.
2. Fix every `publish_event` / `record_audit` call that ran outside a Unit of
   Work (they now raise), or mark the deliberate best-effort ones;
   `LEGACY_UNSCOPED_EFFECTS=true` is a 0.7-only escape hatch (removed in 0.8).
3. Split production deployments: asymmetric JWT (`JWT_PRIVATE_KEY` on the
   issuer only, `JWT_PUBLIC_KEYS` everywhere) or the explicit
   `JWT_ALLOW_SHARED_SECRET_SPLIT=true`; plan the move to
   `RPC_AUTH_MODE=service_identity` + X25519 response-binding keys.
4. Move contract DTOs out of module packages (`sandloom.shared.*` or
   `CONTRACT_DTO_PACKAGES`); commit `contracts.snapshot.json` per module.
5. Billing: a Celery worker + exactly one beat must run wherever billing is
   loaded (intent reconciler); the microservices profile now ships
   `billing-worker` / `billing-beat` (internal change #83). Custom PSP adapters implement
   the extended `StripeClientPort`; custom cache drivers backing the security
   cache need `set_max` or `cas` (internal change #85).
6. Apps: add `operation_class(...)` to every route and the
   `assert_operation_classes(app)` test.

### Breaking changes

- **`publish_event` / `record_audit` are guaranteed by default** (internal change #52).
  `publish_event` (and `publish_event_versions`) outside a Unit of Work raises
  `UnitOfWorkRequiredError` (new, `sandloom.core.di`) instead of publishing straight
  to the bus; `record_audit` defaults to `required=True` and raises
  `AuditUnavailableError` outside a UoW, without an audit store or with a
  non-UUID `actor_id`. Best-effort is explicit (`required=False`) and, in
  business-module code, needs a `# sandloom: best-effort <reason>` marker — new
  `sandloom check` rule `best-effort-effects`. Transitional
  `LEGACY_UNSCOPED_EFFECTS` (default `false`; 0.7 only, **removed in 0.8**)
  restores the 0.6 defaults for calls without an explicit `required=`, with a
  logged warning and a `DeprecationWarning` per call. New test fixture
  `db_uow`.
- **Production split processes refuse a shared HS secret** (internal change #51). A prod
  process that loads no token issuer (no module declared
  `issues_tokens=True`) or binds remote services (`SERVICE_BINDINGS`) does
  not start with an HS* `JWT_ALGORITHM` unless
  `JWT_ALLOW_SHARED_SECRET_SPLIT=true`; under
  `PRODUCTION_PROFILE=multi_tenant_strict` asymmetric JWT is required. A
  monolith (issuer loaded, no bindings) is unaffected.
- **Contract DTOs must be importable by every caller** (internal change #54). A contract
  registered by a module may not name a pydantic model / Enum defined inside
  a (loaded) module package or a function: `ContractTypeError` at
  registration, `sandloom check` rule `contract-dtos`. Allowed: `sandloom.shared.*` and
  the packages in the new `CONTRACT_DTO_PACKAGES`.
- **Billing grants fail closed** (internal change #73, internal change #77). A subscription whose price is
  not in the catalog grants nothing (`price_id` NULL; a change to an unknown
  price withdraws the previous entitlements); a subscription created without
  an operation intent (`origin='psp'`) grants only with
  `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true` (default `false`); existing
  rows are backfilled `origin='legacy'` and keep granting **only if their
  price is known**. A credit-pack completion without a `payment_intent`
  grants nothing (audited `billing.credits.grant_refused`). Credit balances
  may become negative after a refund / dispute (the `amount >= 0` CHECK is
  dropped). Details under "Billing".
- **PSP port (`sandloom.billing.psp.StripeClientPort`)** (internal change #69, internal change #76):
  `create_checkout_session` returns `PspSession(id, url, expires_at)` (was the
  URL string) and takes `subscription_metadata`; new required methods
  `expire_checkout_session(session_id)` and
  `list_checkout_sessions(customer_id=, created_gte=)`. Custom PSP adapters
  must implement them.
- **Tenancy revokes on every role change, fail closed** (internal change #47). `change_role`
  and `transfer_ownership` revoke the affected members' access tokens (the
  client gets `401 "Token has been revoked"` and refreshes); every tenancy
  revocation (removal, leave, role change, transfer, org deletion) is
  `require_immediate=True` — with the revocation store down the change is
  refused with `503` instead of committing without its mark.
- **`RPC_RESPONSE_BINDING=strict` means keyed** (internal change #67): v1 (unkeyed) replies
  are rejected, an `x25519` reply must come from the binding's
  `"service_id"`, and strict with a service identity refuses to start with an
  unpinned binding or without the service's X25519 key.
- **Route markers are pruned from the executed dependencies** (internal change #74).
  `operation_class(...)`, `strict_revocation()` and `revocation_freshness(...)`
  stay in `route.dependencies` but no longer appear in `route.dependant`;
  read them with `sandloom.core.route_markers.route_marker_calls(route)`. Only
  factory-registered markers are pruned (a `functools.wraps` copy of a
  marker around a real guard is kept and runs).
- **Dependencies** (internal change #48, internal change #62, internal change #64): `sandloom-framework` now requires
  `aiohttp>=3.14.5,<3.15` (default internal-RPC transport) and pins
  `asyncpg>=0.32.0,<0.33` and `sqlalchemy[asyncio]>=2.1.4,<2.2` to the ranges
  whose driver internals the fast paths are verified against.

- **Revocation marks need `set_max` or `cas` on the security cache** (internal change #85).
  `CacheProvider` gains `set_max(key, value, *, ttl_seconds=None) -> str`
  (feature `set_max`; the base class supplies a portable read / `add` /
  `compare_and_set` default). A custom cache driver serving
  `SECURITY_CACHE_INSTANCE` with neither `set_max` nor `cas` now REFUSES
  revocation-mark writes (retried, then the durable applier; `503` for a
  `require_immediate=True` change) instead of risking a lowered mark. The
  bundled Redis driver and `sandloom.core.testing.InMemoryCache` implement
  `set_max` atomically.

### Security

- **Asymmetric access tokens** (internal change #51). `JWT_ALGORITHM=EdDSA|RS256|ES256`:
  the issuer (identity) holds `JWT_PRIVATE_KEY` + `JWT_KID`, every service
  verifies with `JWT_PUBLIC_KEYS` (`{"<kid>": "<PEM>"}`, several during a
  rotation) and cannot mint (`TokenIssuanceUnavailable`). Each algorithm is
  bound to one key type (no algorithm confusion; no kid / unknown kid / HS
  token / `none` rejected). Critical startup role check
  (`sandloom.core.security.token_signing_problem`); `JWT_SECRET` is optional in
  asymmetric mode and refused on verify-only processes. `sandloom keys generate
  [--alg] [--kid] [--out-dir]`; rotation runbook in
  `docs/advanced/security-model.md`. `PLANNER_CURSOR_SECRET` keys planner
  cursors where neither `INTERNAL_API_TOKEN` nor `JWT_SECRET` exists. The
  microservices profile signs with EdDSA.
- **Service identity for internal RPC** (internal change #57). `RPC_AUTH_MODE=service_identity`:
  each service signs as `RPC_SERVICE_ID` with its own Ed25519
  `RPC_PRIVATE_KEY` + `RPC_KEY_ID`; providers verify with `RPC_SERVICE_KEYS`
  and authorize per contract / method against `RPC_ALLOWED_CALLS` (deny by
  default; 401 unverifiable, 403 `rpc_caller_forbidden`). A caller whose
  allowlist entry misses one of its own `SERVICE_BINDINGS` refuses to start;
  `sandloom keys allowlist` derives the map, `sandloom keys generate --purpose rpc
  --service <id>` makes a key. `dual` = migration mode; `shared_token` stays
  the default and logs a DEPRECATED line on every production start. The
  microservices profile uses `service_identity`.
- **RPC responses bound to their request** (internal change #64, internal change #67). Every RPC request
  carries a nonce (`X-SANDLOOM-RPC-Nonce`); the provider answers with
  `X-SANDLOOM-RPC-Response-MAC`; a response bound to another call is never
  returned (retryable transport error, pool discarded). v2 binds with a
  SECRET key: per-pair X25519 ECDH + HKDF over each service's DEDICATED X25519
  key (`RPC_DH_PRIVATE_KEY`, `RPC_DH_KEY_ID`, shared `RPC_SERVICE_DH_KEYS`;
  never the Ed25519 signing key), else a key derived from the internal token;
  the MAC also covers the request's signatures. `RPC_RESPONSE_BINDING`
  (`permissive` default — v1 / unbound replies counted in
  `sandloom_rpc_response_unkeyed_total` / `sandloom_rpc_response_unbound_total`;
  `strict`, the microservices profile's default). `SERVICE_BINDINGS[...]
  ["service_id"]` pins the provider. Rejections:
  `sandloom_rpc_response_binding_rejected_total{reason}`. Not a substitute for
  TLS / mTLS.
- **Configurable argon2id work factor + rehash on login** (internal change #61).
  `PASSWORD_HASH_PROFILE` (`strong` default = unchanged m=65536/t=3/p=4;
  `owasp_min` = m=19456/t=2/p=1; `custom`) with `PASSWORD_HASH_TIME_COST` /
  `_MEMORY_KIB` / `_PARALLELISM` overrides. Production refuses parameters
  below the OWASP minimum unless `PASSWORD_HASH_ALLOW_WEAK=true` (logged as a
  SECURITY warning on every start). Identity re-hashes a stored hash with
  other parameters after a successful login, in the same UoW (failures never
  block the login; `sandloom_password_rehash_total{outcome}`).
  `sandloom.core.di.password_hasher()` / `rehash_password_if_needed(...)`.
- **Revocation windows closed** (internal change #45, internal change #47). Tokens carry `iat_us`
  (microseconds); a token is revoked iff its issue instant is `<=` the mark
  (legacy whole-second tokens in the mark's second are revoked too). The mark
  is written at `now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS` (default 2 s) and
  never moves backwards (compare-and-set). `revoke_user_tokens_on_commit`
  also writes `core.auth.tokens_revoked.v1` to the outbox, retries the
  post-commit write (`AUTH_REVOCATION_WRITE_ATTEMPTS`,
  `AUTH_REVOCATION_RETRY_BASE_SECONDS`; `auth_revocation_deferred_total`), and
  identity's durable consumer `sessions@tokens_revoked` applies a lost write.
  `require_immediate=True` writes the mark before commit or refuses the change
  (`503`); identity (password change / reset, role revocation, deactivation)
  and `block_user` use it. A corrupt (unparsable) mark rejects every token of
  the subject (`auth_revocation_corrupt_mark_total`) until the issuance check
  repairs it. Revocation consumers are `dead_letter=False` (retried forever
  with capped backoff); while one is stuck `/ready` answers 503
  `revocations_pending` (`auth_revocation_pending_seconds`), `/health` is
  unaffected.
- **Authority at commit — ADR-0003 TOCTOU gaps G1–G3** (internal change #69). Tenancy's
  class-A writes lock the acting member's row `FOR SHARE` (target + actor in
  user-id order, no deadlock between admins acting on each other); identity's
  admin mutations re-check the admin role inside the write transaction under
  an RBAC advisory lock; new `before_commit(callback)`,
  `revocation_at_commit()` and `subject_commit_check(request)`
  (`sandloom.core.di`) re-read the caller's revocation / block marks right before
  COMMIT (tenancy `POST /invitations/accept` uses it); billing's class-F
  routes run operation intents (see "Billing"). Guarantees stated precisely
  in ADR-0003 ("Guarantees, precisely").
- **Route operation classes, machine-checked** (internal change #68).
  `operation_class("P"|"R"|"RS"|"W"|"F"|"A")` (`sandloom.core.di`, a marker on a
  route or router). `create_app` refuses a declared class that contradicts
  its route (two classes, a staleness budget outside class R, an RS/W/F/A
  route that is not always-strict — `OperationClassError`); the CI gate
  `sandloom.core.testing.assert_operation_classes(app)` fails on an unclassified
  route (generated by `sandloom new`, used by `examples/` and the platform's
  `tests/architecture/test_operation_classes.py`, which pins ADR-0003's
  route map). Billing routes split into `billing_read_router` (GET `/plans`,
  `/subscription`, `/credits`) and the always-strict `billing_router`
  (checkout, portal) — same paths, responses and OpenAPI.
- **`idempotency_guard(store_response=False)`** (internal change #69): in-flight dedupe only
  (409 / 422), nothing stored or replayed. Billing's checkout / portal routes
  use it with renamed scopes (`billing.checkout_intent`,
  `billing.customer_portal_intent`): a checkout / portal URL is never replayed
  from the cache.
- **`PRODUCTION_PROFILE=multi_tenant_strict`** (internal change #54): in prod refuses to
  start unless `TENANCY_MODE=multi`, `tenant_rls` + role verification on every
  database, fail-closed revocation, `SECURITY_CACHE_INSTANCE` on a different
  Redis server than the default cache, contract compat check on when split and
  (since internal change #51) asymmetric JWT when split. A critical startup task checks that
  every `TenantScoped` table has RLS enabled + FORCEd with its policy.
  `SECURITY_CACHE_INSTANCE` (default `default`) carries revocation marks,
  blocks and idempotency records.

### Billing

- **Operation intents for checkout / customer portal** (internal change #69; ADR-0003 G1).
  Authorize (live role + membership version) → a billing-owned intent
  (`billing_operation_intents`, migration `0004_billing`) committed before the
  PSP call → PSP call keyed by the intent (a timeout leaves it pending, `503`;
  the retry re-sends the same key; a PSP 4xx fails it, `502`) → re-check
  authority and session marks right before issuing (`403` lost / `409`
  changed; the intent is revoked and the checkout session expired). An issued
  checkout intent is final: a retry returns its stored URL after the re-check
  (an issued portal intent answers `409`). The routes no longer use the
  request `DbSession` (each step is its own short UoW). Intent paths are
  owner-scoped. Webhook gate: a credit grant or a NEW subscription applies only
  for an issued checkout intent of the event's customer (and session);
  otherwise journaled failed, audited `billing.intent.unauthorized_completion`,
  intent flagged `followup_at`. `invoice.paid` must come from the
  subscription's customer; unknown Stripe statuses are not applied;
  `incomplete_expired` is terminal.
- **Fail-closed entitlements** (internal change #73; ADR-0003 items 9, 11).
  `billing_subscriptions.origin` (`0005_billing`: `intent` | `psp` | `legacy`;
  existing rows backfilled `legacy`, later inserts default to `psp`). A
  subscription event with items sets the price from them (unknown → NULL,
  audited `billing.subscription.unknown_price`). `grants_access`: no catalog
  price → no access; `psp` rows only with
  `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS=true`. A downgrade past `0005`
  refuses while `psp` rows exist unless `SANDLOOM_BILLING_DROP_ORIGIN=1`.
  `billing.subscription.created/updated/canceled.v1` carry
  `data.grants_access` (additive).
- **One subscription per checkout intent, at its price** (internal change #76; item 16):
  exactly one item at the intent's catalog price, quantity 1
  (`billing_subscriptions.intent_id`, partial UNIQUE, `0006_billing`); a
  credit-pack completion must name the intent's price.
- **Intent reconciliation** (internal change #76; items 7, 13, 15). Beat task
  `sandloom.billing.tasks.reconcile_operation_intents` (every 5 min, `acks_late`)
  closes `pending` intents older than `BILLING_INTENT_RECONCILE_AFTER_MINUTES`
  (default 30) by finding their PSP session through the intent marker
  (expire → `revoked`; none → `failed`; paid → `followup_at`) and retries
  failed expire compensations, bounded by
  `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` (default 5). An issued intent is
  never touched. A retry of an issued checkout whose session expired answers
  `410` (`psp_session_expires_at`; legacy rows: created + 24 h). Migration
  `0007_billing`.
- **Refunds and disputes reverse credits** (internal change #77; item 12).
  `charge.refunded`, `charge.dispute.created` / `.updated` / `.closed` reverse
  (a won dispute restores) a credit-pack purchase's credits through the
  append-only ledger, deduped per PSP event; partial refunds / disputes
  proportional, inquiries audit-only; events arriving before the purchase are
  recorded (`billing_psp_payment_states`) and applied at grant time; orphan
  payment states are purged after 90 days. New tables `billing_credit_grants`,
  `billing_psp_payment_states` (`0008_billing`); events
  `billing.credits.reversed.v1`, `billing.credits.restored.v1`; audits
  `billing.credits.reversed` / `.restored` / `.reversal_refused`,
  `billing.charge.disputed`. A balance may be negative (spends are refused
  while short); `GET /credits` adds `debt` and `in_debt`. Subscription access
  is not changed by a dispute (temporary policy, ADR-0003 item 17).
- **Portal change attribution** (internal change #77; item 10): a plan /
  `cancel_at_period_end` change with no portal intent of the customer issued
  within `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` (default 60) is audited
  `billing.subscription.unattributed_change` — never blocked
  (`billing_operation_intents.issued_at`, `0009_billing`).

### Contracts, events and data

- **Durable command idempotency** (internal change #50). The dedupe record of a command
  contract lives in the PROVIDER's database (`contract_command_log`, core
  chain `0006`), claimed in the command's own UoW and committed iff the effect
  commits: a crash after the commit replays, never re-runs; same key + other
  arguments → `409`; a concurrent duplicate waits up to
  `CONTRACT_DB_LOCK_TIMEOUT_MS` (`503 ContractCallInProgress`); results
  > 256 KiB are non-replayable markers. Records expire after
  `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` and are purged by the relay
  housekeeping / `sandloom outbox purge`. The cache is no longer involved (a driver
  without `command_idempotency` falls back to the fail-closed cache dedupe).
- **Rolling-upgrade contract compatibility** (internal change #54). Per-module
  `contracts.snapshot.json` (`sandloom contracts snapshot [--write|--check]`; a
  stale snapshot fails the suite), `sandloom contracts check --against
  <snapshot|dir|url> [--as provider|consumer]`, runtime comparison of every
  bound provider's live describe document (`CONTRACT_COMPAT_CHECK=ready`
  default → `/ready` fails with `contract_incompatible`; `strict` also aborts
  startup; `off`; `CONTRACT_SNAPSHOT_PATHS`); a startup warning for remote
  bindings without a snapshot. `ContractRequirement(methods=...)` narrows the
  check.
- **`sandloom-migrate extract-module`** (internal change #53): moves a module's existing data out
  of a shared database (offline: fence, copy with manifest + checksums,
  sequences, outbox / inbox / audit / command-log hand-off, Streams
  group-owner transfer; `--verify`, `--drop-source`, `--unfence`; dry-run by
  default). Runbook: `deploy/README.md`.
- **`ListOperation` and planned read contracts** (internal change #39). Declared
  tenant-scoped keyset list reads with an opaque HMAC cursor
  (`PLANNER_LIST_MAX_LIMIT`, default 500; `InvalidCursor` / `PageLimitError`
  → 400); `@contract_method(planned=True)` + `contract_reads()` (no UoW,
  read-only lease on the contract pool). `tenancy.membership.v1` is planned;
  `GET /orgs`, `/orgs/{id}`, `/orgs/{id}/members` are planned routes (API
  unchanged, plus `cursor` / `X-Next-Cursor`); tenancy migration `0004`
  adds the keyset indexes.
- **Versioned member events** (internal change #45, internal change #47). `tenancy.member_*.v1` carry an
  optional per-(org, user) `version` (`tenancy_member_versions`, `0005_tenancy`);
  accepting an invitation / creating an org publishes
  `tenancy.member_added.v1`. Rolling-upgrade rule
  `sandloom.shared.schemas.membership.member_event_action`: a version-less event
  refreshes from the new `tenancy.membership.v1.get_member_state`.
- **Event consumers** (internal change #47): `register_event_consumer(...,
  dead_letter=False)` retries a failing delivery forever with capped backoff
  instead of dead-lettering it; a handler raises `PoisonEventError`
  (`sandloom.core.di`) for an event that can never apply (dead-lettered at once,
  CRITICAL).
- **Contract-read cache shadow** (internal change #68; off by default): `CONTRACT_CACHE_SHADOW`
  measures what a caller-side TTL cache of a read contract would have done
  (`sandloom_contract_cache_shadow_total`, per-request share, time avoided, stale
  hits by kind) without serving from it; `CONTRACT_CACHE_SHADOW_REPORT_DIR`
  + `benchmarks/ladder/agg_shadow.py`.

### Performance

- **Internal RPC client is aiohttp** (internal change #62, internal change #64): `RPC_TRANSPORT=aiohttp`
  (default) or `httpx` (keep it behind an egress proxy — aiohttp ignores
  `HTTP(S)_PROXY`). One RPC ~1.25 → ~0.36 ms CPU at concurrency 64; the
  mixed-profile split premium +21.6 % → +16.9 % at saturation. Bounded
  per-peer pool (`RPC_POOL_MAX_CONNECTIONS`, `RPC_POOL_IDLE_SECONDS`,
  `RPC_MAX_RESPONSE_BYTES` → 502), `sandloom_rpc_client_*` metrics. aiohttp
  internals are probed (`sandloom.core.rpc_transport_compat`,
  `VERIFIED_AIOHTTP_RANGE`); a failed probe falls back to httpx
  (`sandloom_rpc_transport_fallback_total`).
- **Fewer PostgreSQL round trips on the write path** (internal change #46): outbox
  mark-delivered is ONE autocommit statement (4 → 1 RT per flush; an
  event-publishing RLS request 9 → 6 RT); `non_tenant_statements()` skips the
  tenant setting for allowlisted system statements (relay tick 12 → 9 RT);
  **`DB_RLS_MERGED_BEGIN` is on by default** (`BEGIN …; set_config` in one
  message on `tenant_rls` + asyncpg; kill switch `false`).
- **Zero per-request cost of route markers** (internal change #74): ~35–40k instructions per
  marker per request removed (strict + class route 314k → 241k instr/request).
- **Non-blocking pool checkout at max overflow** (internal change #66): −47 % instructions
  per checkout+return cycle at max overflow (guarded by the
  `pool_fast_checkout` driver probe).

### Fixes

- **Never a 500 from a pooled connection that died while idle** (internal change #55): a
  closed / broken asyncpg connection is replaced at checkout or, when provably
  nothing was sent, reconnected in place for the transaction's first message
  (kill switch `DB_DEAD_CONNECTION_RETRY=false`); otherwise
  `DatabaseUnavailable` (503, `Retry-After`) instead of a masked 500. The idle
  ping is really one round trip.
- **Driver-internals guard for the DB fast paths** (internal change #48):
  `sandloom.core.sql_alchemy.driver_compat` probes asyncpg / SQLAlchemy private
  state once per process; on a mismatch the plain path is used
  (`sandloom_db_driver_fast_path_disabled_total{probe}`). `non_tenant_statements()`
  checks every statement it covers (`NonTenantStatementError` outside prod).
- **Revocation marks no longer fail under contention** (internal change #85). The mark was
  written with a read / `add` / `compare_and_set` loop bounded at 8 attempts;
  a writer that lost 8 races in a row gave up (`revoke_user_tokens` returned
  `False`, the mark could stay below its value until the durable applier ran,
  a `require_immediate` change got a spurious `503`). It is now one
  `set_max` (Redis: one atomic Lua round trip; values compared exactly as
  plain decimals; TTL only when written). Mark readers share that value
  domain (`parse_plain_decimal`): a mark that is not a plain non-negative
  decimal (e.g. `1e9`, a sign) reads as corrupt — fail closed, repaired at
  issuance; framework writers never wrote such values. Portable default
  `sandloom.core.infra.contracts.portable_set_max` (read / `add` /
  `compare_and_set`, retrying while the stored value is lower;
  `CacheContentionError` only after `SET_MAX_MAX_ROUNDS` = 1000 lost races,
  i.e. a livelock against a non-monotonic writer); without `cas` it degrades
  to a plain `set` (NOT monotonic) unless called with
  `overwrite_without_cas=False`, which the revocation mark does. The Redis
  driver falls back to that loop only for a client without Lua scripting
  (logged once); errors inside the script path propagate.
- **Gateway's static IP kept out of the dynamic address pool** (internal change #87): the
  microservices network gets `ip_range: ${SANDLOOM_IP_RANGE:-172.28.0.128/25}`, so
  no container started before the gateway can take its static
  `SANDLOOM_GATEWAY_IP` (.10) — with #83's two extra containers one did, and the
  gateway failed with "Address already in use". `SANDLOOM_SUBNET`,
  `SANDLOOM_GATEWAY_IP` and `SANDLOOM_IP_RANGE` must change together (the gateway IP
  inside the subnet, outside the range); `test_deploy_profiles` pins it and
  `tools/ci/ci_local.sh` exports a matching range.
- **Microservices profile runs billing's worker + beat** (internal change #83): billing's
  periodic tasks (intent reconciler, orphan payment-state purge) never ran in
  `deploy/compose/microservices.yml`. New `billing-worker`
  (`BILLING_WORKER_CONCURRENCY`, default 1) and `billing-beat`, each on
  billing's own broker; connection budget 109 (+10 reserve) of
  `max_connections` 150. `test_deploy_profiles` now asserts that every module
  with `setup_periodic_tasks` has exactly one beat (and a worker on its
  broker) in every profile that serves it.
- **Revocation copies** (internal change #79, internal change #81): on a `revocation_freshness` route the
  staleness budget now holds at decision time (a copy that expired or was
  invalidated during the live block-mark read is replaced by a live read);
  only a drop of the SAME subject (or a wholesale clear) discards an in-flight
  read, so foreign broadcasts on the server-wide channel no longer stop
  caching. Strict routes were never affected.

### Testing & tooling

- **Test resource ownership** (internal change #75): a test database (comment) and Redis
  index (`sandloom:test:owner`) carry an owner (`SANDLOOM_TEST_OWNER`, else the
  checkout root); another owner's resource aborts the session before anything
  is reset (exit 4) unless `SANDLOOM_TEST_TAKE_OWNERSHIP=1`.
- **Connection budget** (internal change #75): small test pools by default (explicit
  environment wins), a fail-fast headroom check
  (`SANDLOOM_TEST_SKIP_CONNECTION_CHECK=1` disables it), `-n auto` capped to what
  fits, the split conformance matrix boots in waves of <= 40 connections.
- **Process-global state restored after every test** (internal change #75)
  (`sandloom.core.testing.isolation`); `run_on_commit` runs `before_commit` checks
  first (internal change #69).
- **`SANDLOOM_TEST_DATABASE_URL` / `_CACHE_URL` / `_BROKER_URL` win over
  `.env.test`** and the reset guard refuses any other database (internal change #43); the
  repository's pre-push full run requires an explicit `SANDLOOM_TEST_DATABASE_URL`
  (`tools/checks/prepush_full_tests.sh`, internal change #75).
- **Race-free subprocess servers** (internal change #78): `sandloom.core.testing.served`
  (`ReservedPort`, `spawn_served`, a per-spawn identity probe) for split
  conformance and the extraction / split e2e suites.
- **`sandloom.core.testing.wait_revocation_listener_subscribed()`** (internal change #79)
  replaces `PUBSUB NUMSUB` waits (flaky under xdist).
- `assert_contract_conformance(..., transport=)` runs the RPC side over a
  real socket (`aiohttp` / `httpx`); `served_rpc(core)` (internal change #62).
- `tools/ci/ci_local.sh`: race-free microservices gateway port (Docker
  allocates it) and opt-in `SANDLOOM_CI_LOCAL_TIMEOUT_SCALE` for overloaded hosts
  (internal change #84).
- Release measurement (internal change #86): cachegrind, login-free SaaS mixes, this
  release vs 276fe84 — no regression in instructions per request (monolith
  −0.5..−0.7 %, split ±0.2 %; split premium +5.3..+6.0 % in instructions);
  wall-clock (CPU µs, rps, latency percentiles) deferred to a dedicated Linux
  machine.
- Local Docker CI runner `tools/ci/ci_local.sh` (act + fallback) while GitHub
  Actions is unavailable (internal change #58, internal change #59); installed-wheel RPC transport smoke
  `tools/packaging/check_wheel_rpc_transport.py` (internal change #67).
- Benchmarks: ladder harness fixes (internal change #41), RPC / write-path diagnostic stands
  (internal change #42, internal change #60), merged-BEGIN soak (internal change #49), integration run (internal change #40), argon2
  presets and split premium (internal change #65), full SaaS-mix stand (internal change #39).

### Migrations in this release

| chain | revision | what |
|---|---|---|
| core (every hosting database) | `0006_contract_command_log` | durable command dedupe table |
| tenancy | `0004_tenancy_keyset_order_indexes` | keyset-order indexes (plain `CREATE INDEX`) |
| tenancy | `0005_tenancy_member_versions` | per-(org, user) member version |
| billing | `0004_billing_operation_intents` | operation intents |
| billing | `0005_billing_subscription_origin` | `origin` (backfill `legacy`, default `psp`) |
| billing | `0006_billing_subscription_intent` | `intent_id` + partial UNIQUE |
| billing | `0007_billing_intent_reconcile` | reconciliation columns |
| billing | `0008_billing_credit_grants` | credit grants, payment states, CHECK `amount >= 0` dropped |
| billing | `0009_billing_intent_issued_at` | `issued_at` |

## [0.6.0] — 2026-10-10

0.5.0 was never tagged: everything listed for it below ships in 0.6.0, and
the deprecations announced "for removal in 0.6.0" move to 0.7.0 (a
deprecation lasts at least one released minor). Breaking changes first.

### Upgrade notes (0.4.0 → 0.6.0)

1. **`get_core` is async.** `Depends(get_core)` is unchanged; replace every
   direct call `core = get_core(request)` with `core = await get_core(request)`,
   or — where the caller cannot await — with the new SYNC accessor
   `sandloom.core.di.core_from_request(request)` (same value). The same applies to
   a hand-called `Capability(...)` dependency and billing's `get_psp`.
   `sandloom check` flags every un-awaited direct call (rule `get-core-await`).
   Why no compatibility shim: FastAPI runs a SYNC dependency in the thread
   pool, so a `get_core` that is both a sync function (for direct calls) and
   a no-hop dependency cannot exist; a sync `get_core` returning an awaitable
   proxy would keep the per-request thread hop the change removes. The old
   form fails loudly (a coroutine, "never awaited" warning), never silently.
2. **`tenant_rls` deployments**: follow "Security — breaking (tenant RLS)"
   below (`upgrade_tenant_rls` revision per RLS table, table ownership off the
   app role, optional system channel).
3. **Execution planner (new, opt-in)**: nothing changes for code that does not
   use it. To adopt: declare `ReadOperation` / `JoinRead` next to the model,
   switch a read-only route from `db: DbSession` to `reads: PlannedReads` and
   call `await reads.one(op, **key)`; a route that writes keeps `DbSession`
   and adds `reads: UowReads`; code holding a session uses
   `sandloom.core.orm.session_reads(session)`. Enable the driver-level executor
   per deployment with `PLANNER_OPTIMIZED_EXECUTOR=true` (PostgreSQL +
   asyncpg). See `docs/design/execution-planner.md`.

### Added — execution planner (declared operations)

Public surface (additive; `public_api_baseline.json` updated):

- **`sandloom.core.orm.ReadOperation(entity, result, by=("id",), database=, name=)`**
  — a declared single-row read by an equality key; the result is ALWAYS the
  declared pydantic DTO (never an ORM entity), whichever executor runs it.
  Tenant scoping is inferred from `TenantScoped` and cannot be opted out of.
- **`sandloom.core.orm.JoinRead(..., join=, via=, join_by=, join_where=,
  joined_fields=)`** — a single-row read of `entity` joined to `join` on a
  DECLARED foreign key to its primary key; `by` must cover a primary / unique
  key (at most one row on every executor); the tenant predicate is applied to
  every `TenantScoped` side. Tenancy's membership guard
  (`MembershipService.get_role`) now runs the declared
  `member_role_in_active_org` (same SQL, same semantics).
- **`sandloom.core.orm.Reads`** (the handle) and **`sandloom.core.orm.session_reads(session)`**
  — declared reads ON an open Unit of Work (request `DbSession`,
  `db_session()`, `contract_session()`): the unit's transaction, connection
  and tenant; read-your-own-writes.
- **`sandloom.core.di.PlannedReads`** (`get_planned_reads`) — a route DECLARED
  read-only: `publish_event` / `record_audit` / `on_commit` raise
  `ReadOnlyScopeError`, the database transactions are `READ ONLY`, and the
  route may not also open a Unit of Work (`PlanningError`). General-executor
  reads of one route share ONE read-only transaction (the scope's read unit),
  so N declared reads cost what the same reads on a `DbSession` cost.
- **`sandloom.core.di.UowReads`** (`get_uow_reads`) — declared reads inside the
  route's read-write Unit of Work (mixed routes); always the general executor.
- **`ModuleAPI.register_operations(*ops)`** — a module declares its
  operations in `register(core)` / `setup`: each is planned by a CRITICAL
  startup task (a declaration that does not match the schema fails the boot),
  the executor the kernel picked and the reason are logged
  (`sandloom.core.planner`, e.g. `general executor — 'label' is not a plain
  column attribute`), and `/diagnostics/core` lists every registered
  operation with its current plan under `planner`. Tenancy registers
  `member_role_in_active_org`.
- **Isolation is part of the read contract.** A read-only scope is READ
  COMMITTED, explicitly (`BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY` /
  the read unit's `isolation_level`): two reads of one scope MAY see
  different snapshots. **`sandloom.core.di.SnapshotReads`** (and
  `read_scope(..., isolation="repeatable_read")`) gives the whole scope ONE
  `REPEATABLE READ READ ONLY` snapshot (every read runs in the scope's read
  unit, on either executor); any other isolation is refused at planning.
- **Locking reads are never planned**: `ReadOperation(..., lock=...)` is
  refused at declaration (`PlanningError`); locks stay on `DbSession` +
  `select(...).with_for_update(...)`, whose behaviour is unchanged (tested);
  a planner transaction (READ ONLY) refuses `FOR UPDATE` outright.
- `sandloom.core.di.core_from_request(request)` — sync accessor of the runtime;
  `sandloom check` rule `get-core-await`.
- `sandloom.core.planner.read_scope(core, owner)` — the read-only scope outside
  HTTP (workers, scripts). `DatabaseProvider.execution_adapter()` — optional
  capability-contract method (default `None`: declared operations cannot run
  on that database, planning says so).
- Setting **`PLANNER_OPTIMIZED_EXECUTOR`** (default `false`): run eligible
  operations (single table / FK join, every DTO field a plain column,
  PostgreSQL + asyncpg, read-only scope) on the driver-level executor —
  `BEGIN READ ONLY; set_config(tenant)` in one message, the generated
  statement with an explicit tenant predicate, `COMMIT` (one autocommit round
  trip without `tenant_rls`). Every other operation runs on the general (ORM)
  executor; `Plan.reason` says why.

Guarantees (both executors, one shared layer; tests in `benchmarks/tests/
test_planner_*.py` and the S5 suite; `test_planner_equivalence.py` runs every
declared operation on both executors and both entry points and requires equal
results, equal errors and no side effects): the tenant is bound per execution and
never stored in a plan (fail closed before any I/O), explicit tenant predicate
+ RLS, read-only enforced twice (scope + database), one connection lease that
terminates and invalidates on any error or cancellation.

Measured (`benchmarks/ladder/RESULTS.md`, S5 secure read): general executor =
request UoW (842 vs 842 µs CPU/request), optimized executor 478 µs (−43 %).
Two declared reads on the general executor now share one transaction
(ladder rungs `L6S5two` / `L6S5p2` / `L6S5po2`; see the design doc §16).

### Changed — breaking (request-path dependencies are async)

- **`sandloom.core.di.get_core` is now `async def`.** FastAPI runs a sync `def`
  dependency in the anyio thread pool on every request; `get_core` (an
  attribute read) cost a thread hop per request. `Depends(get_core)` is
  unchanged; a direct call becomes `core = await get_core(request)`. The
  `Capability(...)` dependency and billing's `get_psp` are async for the same
  reason. A test (`core/tests/test_async_dependencies.py`) enumerates every
  framework dependency of the composition root's routes and of the public
  dependency factories and fails on a sync one.

### Security — breaking (tenant RLS)

Breaking for deployments that enabled `tenant_rls` (opt-in; no bundled module
or example does). Per this project's 0.x policy (a **minor** release may
contain breaking changes, listed first) this ships in the next minor (planned
as 0.5.0, released as 0.6.0).
No transitional flag restores the old bypass — keeping it would keep the
vulnerability. The one transitional knob is `verify_roles: false`, which skips
the new startup role checks with a loud warning while roles are being fixed;
it is deprecated on arrival and will be removed in 0.7.0.

- **The tenant RLS policy no longer has a bypass.** The previous predicate
  accepted `current_setting('sandloom.tenant_bypass') = 'on'`, a custom setting any
  role may set, so any SQL-capable path running as the application role
  (`text()`, `exec_driver_sql`, the raw driver connection, injected SQL) could
  read and write every tenant on `tenant_rls` databases. The policy is now
  `tenant_id = <canonical sandloom.tenant_id or NULL>`: no "all tenants" value; an
  unset/empty/malformed setting yields zero rows and refused writes instead of
  a cast error.
- **`unscoped()` lifts only the ORM guard** on a `tenant_rls` database (a
  warning is logged when it runs with no tenant bound). Behaviour without
  `tenant_rls` is unchanged.
- **System channel** for cross-tenant work:
  `sandloom.core.infra.drivers.system_db.system_session(actor=, reason=,
  database=, tenant_id=)` — a separate engine logged in as a separate
  least-privilege role (`DATABASE_SYSTEM_URL` / INFRA `system_url`), reaching
  only tables migrated with `enable_tenant_rls(..., system_access=True)`;
  every session is logged and audited (`sandloom.system_db.session`). Kernel-only:
  the `infra-imports` rule now also rejects dynamic imports of driver
  modules and `sandloom.core.sql_alchemy.tenant_rls`.
- **Startup role verification** (`verify_roles`, default on): a `tenant_rls`
  database refuses to start — in every environment — when the application
  role is superuser / `BYPASSRLS` / owner of an RLS table / member of a
  privileged or the system role, or when a legacy bypassable policy is left.
  Runs in the API lifespan and at Celery worker boot (`verify_worker_infra`).
  New options `require_system_channel`, `verify_roles`, `system_url`.
- **Migration:** add a revision calling `sandloom.core.orm.upgrade_tenant_rls(op,
  "<table>")` for every RLS table and move table ownership off the app role
  (docs/modules/database/multi-tenancy.md). Residual risk (documented, not fixed):
  SQL running as the app role can still set `sandloom.tenant_id` to another
  tenant — see the RLS threat model in docs/advanced/security-model.md.

The framework absorbs per-module boilerplate
([ADR-0002](https://sandloom.dev/docs/adr/0002-framework-absorbs-module-boilerplate/)): a
business module now carries only its domain. Nothing here is breaking —
existing conftests, env.py files and hand-written `register(core)` keep
working.

### Performance — secure tenant reads (S5)

Measured on the benchmark stand in `benchmarks/` (`benchmarks/ladder/RESULTS.md`:
uvicorn uvloop + httptools, 1 worker, conc 64, interleaved A/B rounds, server
CPU per request; Apple M-series laptop, PostgreSQL in Docker — absolute
numbers are host-specific, the deltas are the point). S5 = JWT +
`TenantScope` + `require(Subscribed())` + a `TenantScoped` PK read under
FORCEd RLS as a NOBYPASSRLS role. Default ON (no configuration):

- **`tenant_rls` hook without engine events.** The tenant GUC is now set by
  wrapping the dialect's `do_execute` / `do_executemany` /
  `do_execute_no_params` (+ `do_begin` / `do_commit` / `do_rollback` for the
  per-transaction state) instead of `before_cursor_execute` and transaction
  listeners — any engine event moved every statement onto SQLAlchemy's
  instrumented path. Same semantics (transaction-local `set_config`, lazy
  re-sync, first statement of every transaction syncs, ROLLBACK TO SAVEPOINT
  re-syncs, fail-closed) and the same round trips; S5 −19…−33 µs/request,
  −196k instructions. Covered paths: ORM, Core, `text()`, `exec_driver_sql`,
  executemany / insertmanyvalues, streaming, savepoints,
  `session.connection()`, `run_sync`; the raw driver connection is not
  covered and fails closed in PostgreSQL (tests:
  `src/sandloom/core/tests/test_tenant_rls_paths.py`, incl. a reference-cycle guard
  that caught a first version's p99 regression).
- **`ProcessTimeMiddleware` caches its labelled Prometheus children** per
  (method, route template, status), reads method/route off the ASGI scope (no
  `Request`), bounded cache; identical `/metrics` output and `X-Process-Time`
  (tested against the previous implementation). −4…−17 µs/request.
- **The ORM tenant guard's criterion is built once**; the tenant value is a
  bound parameter read from the context at execution time (never cached in the
  statement or the compiled cache). −20…−32 µs/request, −174k instructions,
  cyclic garbage per request 5 → 0 (tests:
  `src/sandloom/core/tests/test_tenant_criteria_prebuilt.py`).

Experimental, default OFF:

- **`DB_RLS_MERGED_BEGIN`** (prototype, `tenant_rls` + asyncpg): `BEGIN …;
  SELECT set_config(tenant)` as ONE simple-query message, the adapter handed
  a started asyncpg transaction; any failure or cancellation in flight
  terminates and invalidates the connection. RLS read UoW 4 → 3 round trips,
  S5 −41 µs/request (tests: `tests/perf_budgets/test_rls_merged_begin.py`
  incl. cancellation at every await point).
- `DB_LAZY_BEGIN` and `DB_READ_ONLY_SESSIONS` (below) remain prototypes.

### Added

- **Declared read-only sessions prototype** (`DB_READ_ONLY_SESSIONS`,
  default **off**; `ReadOnlyDbSession` / `get_read_only_db` in `sandloom.core.di`).
  A route that DECLARES `db: ReadOnlyDbSession` gets an autocommit read
  executor (one round trip per SELECT, no BEGIN/COMMIT) that REFUSES any
  write, flush, `publish_event`/`record_audit`, locking read or raw statement
  with `ReadOnlySessionError` before it is sent. No guessing: undeclared
  routes are unchanged. With `tenant_rls` the reads keep their transaction so
  `SET LOCAL` covers them. Off: `ReadOnlyDbSession` is exactly `DbSession`.
  Tests: `tests/perf_budgets/test_read_executor_{semantics,requests}.py`.
- **Lazy BEGIN prototype** (`DB_LAZY_BEGIN`, default **off**,
  `sandloom.core.sql_alchemy.lazy_begin`). Defers a Unit of Work's `BEGIN` until
  its first non-read statement: plain compiled `SELECT`s before it run in
  autocommit and a read-only UoW sends neither `BEGIN` nor `COMMIT` (S3:
  3 → 1 PostgreSQL round trips, unchanged module code). Applied only where it
  is equivalent — server default isolation `READ COMMITTED`, no explicit
  isolation/readonly/deferrable, no `tenant_rls` on the engine, no locking
  `SELECT`, no `execution_options(sandloom_lazy_begin=False)` opt-out (required for
  a `SELECT` calling a side-effecting function). Measured in
  `benchmarks/ladder/RESULTS.md`; budget/semantics tests in
  `tests/perf_budgets/test_budget_lazy_begin.py`.
- **Overflow-linger connection pool** (`LingeringQueuePool`, the driver's pool
  for every server database; `DB_POOL_OVERFLOW_LINGER_SECONDS`, default 30,
  `0` = stock QueuePool). Stock QueuePool closes a returned overflow
  connection as soon as `pool_size` others are idle and reconnects on the next
  shortage, so load hovering above `DB_POOL_SIZE` churned connect/close
  continuously (TCP + TLS + SCRAM + session setup on the event loop: ~15.8 ms
  CPU per connect with asyncpg 0.30, 3.7 ms with 0.32). This was the root
  cause of the CI uvicorn `--loop uvloop` vs `asyncio` S3/S4 anomaly (a
  positive feedback loop sensitive to loop scheduling). Linux docker, S3, 64
  connections, old stack: uvloop 3200 -> 1310 us/req CPU, asyncio 1830 ->
  1400, ~350 -> 30 connections per run; current stack: 1540-1850 ->
  920-1140 us/req, p99 2-3x lower. The total bound
  (`DB_POOL_SIZE + DB_MAX_OVERFLOW`) is unchanged; after a quiet window the
  pool shrinks back to `DB_POOL_SIZE` on checkin (an idle process keeps its
  surplus until the next checkins or `pool_recycle`). Budget:
  `perf_budgets/test_budget_pool_churn.py`.
- **Route dispatch index** (`sandloom.core.http.route_index`, installed by
  `create_app` once every module is loaded): requests scan only the routes
  whose leading literal path segments (up to 3, e.g. `/api/v1/orgs`) can
  match, plus every parameterised-prefix / `Mount` / custom route, in the
  original registration order — matching semantics identical to Starlette's
  linear scan (FULL precedence, first PARTIAL -> 405, `redirect_slashes`,
  404, websockets). Rebuilt automatically when routes are added later.
  cachegrind, full ASGI stack, platform app (79 routes): `/health/simple`
  1.07M -> 0.60M instr/request, 404 1.44M -> 0.48M, `/api/v1/orgs` -2.8e5.
- **pytest-xdist support** (`sandloom.core.testing.workers`): under
  `pytest -n auto --dist loadfile` each worker gets database `<db>_gw<N>`
  (created with citext/uuid-ossp) and Redis DB index `+ 16 * (N + 1)`
  (`SANDLOOM_TEST_REDIS_DB_STRIDE`; the Redis needs `--databases 256`). The
  harness re-applies it after every `.env.test` load; tests reload with
  `sandloom.core.testing.reload_test_env` / read the file for subprocesses with
  `worker_env_values` instead of bare `load_dotenv` / `dotenv_values`.
- `GUNICORN_KEEPALIVE` (default 75 s) — the workers' idle keep-alive
  (uvicorn `timeout_keep_alive`), above common LB idle timeouts. Each worker
  logs its loop / HTTP implementation at boot.
- **PostgreSQL round-trip budget tests** (`tests/perf_budgets`): a wire
  counting proxy pins the steady-state round trips of `GET /auth/me` (4),
  `GET /api/v1/orgs` (3), `POST /api/v1/orgs` (9), members (4), credits (7),
  the Stripe credit-pack webhook (12), a relay batch of 50 (6), billing spend
  and the claims contributor.
- `OutboxStore.deliver_batch(...)` / `purge_delivered(...)` and
  `RelayBatchResult` (additive contract; the defaults keep the per-row path,
  the SQLAlchemy store claims and settles a batch in a constant number of
  round trips).
- `OUTBOX_RETENTION_HOURS` (default 168, 0 = keep) and
  `sandloom outbox purge [--hours] [--batch] [--inbox-days]`.
- `DB_POOL_PING_IDLE_SECONDS`, `DB_STATEMENT_TIMEOUT_MS` (30000),
  `DB_IDLE_IN_TX_TIMEOUT_MS` (60000).
- `sandloom.core.unit_of_work.drain_post_commit()` / `PostCommitDispatcher`.
- Tenancy: optional keyset pagination (`limit` <= 500, `after`) on
  `GET /api/v1/orgs` and `GET /api/v1/orgs/{id}/members`.
- **`sandloom.core.testing` pytest plugin.** `sandloom.core.testing` is now a package
  (every existing name — `bind_test_uow`, `recording_uow`, `run_on_commit`,
  `InMemoryCache`, `RecordingSession` — stays importable). An application
  activates the harness once with `addopts = ["-p", "sandloom.core.testing.plugin"]`
  (no `pytest11` entry point, so no other suite changes): it loads the nearest
  `.env.test` before any framework import and provides `sandloom_app`, `db_engine`
  (disposable-test-database guard, schema reset incl. core infra tables),
  `db`, `client` (requests share `db` as the UoW), `make_headers`,
  `user_headers` and the aliases `test_db` / `async_client` / `test_engine`;
  pools are released per test. Ini option `sandloom_composition_root`.
  Framework-tree modules import the fixtures from a one-line conftest
  (`from sandloom.core.testing.fixtures import *`).
- `sandloom.core.testing.make_user_headers(user_id=None, tenant_id=None, **claims)`
  — access token with `token_type`, `iat`, `tenant_id` + `tenant_ids`,
  `email` / `email_verified` defaults.
- `sandloom.core.migrations.run_module_migrations(__file__)` — a module chain's
  env.py in two lines (package, `<pkg>.models` and `<module>_` derived from
  the env.py location; keywords override).
- `sandloom.core.module_api.define_module(__name__, version=..., consumes=...,
  capabilities=..., setup=...)` — declarative `register`: mounts `<pkg>.api`'s
  router, registers `<pkg>.events.EVENT_SCHEMAS`, calls `setup(core)`,
  returns the `ModuleDescriptor`.
- `sandloom check` rule **`test-imports`**: module tests must not import
  `sandloom.core._internal`, `sandloom.core.sql_alchemy`, `sandloom.core.database` or
  `sandloom.core.infra.drivers` (use `sandloom.core.testing`). Manifest allowlists are
  honoured; the bundled identity suite is allowlisted legacy debt.
- `sandloom-scaffold --conftest/--no-conftest`.
- More harness fixtures: `real_uow_client` (requests through the REAL request
  UoW on the test database/cache, no `db` override), `sandloom_cache` (the
  `default` cache on `.env.test`'s `CACHE_URL`, FLUSHDB'd per test; yields the
  provider), `ambient_uow` (recording UoW for the whole test),
  `revoked_tokens`, `sandloom_core`; helpers `committing_uow(session_factory)`,
  `capture_effects(monkeypatch, *modules)`,
  `override_instance(core, capability, ..., driver=..., **options)` and
  `registration_surface(register)` (pin what `register(core)` registers).
- `InfraRegistry.instance_config(capability, name)`,
  `snapshot_instance(...)` / `restore_instance(snapshot)` — reconfigure an
  instance temporarily without touching registry internals.

### Dependencies (measured upgrade — see `docs/contributing/dependencies.md`)

Locked → new: FastAPI 0.116.1 → **0.136.3** (capped `<0.137`), Starlette
0.47.2 → **1.6.0** (floor `>=1.3.1`; lock-only cap `<1.7`), pydantic 2.11.7 →
**2.14.0** (core 2.33.2 → 2.50.0), pydantic-settings 2.10.1 → 2.15.0,
SQLAlchemy 2.0.42 → **2.1.4** (`sqlalchemy[asyncio]`), asyncpg 0.30.0 → 0.32.0,
alembic 1.16.4 → 1.20.0, celery 5.5.3 → 5.6.3 / kombu 5.5.4 → 5.6.2, uvicorn
0.35.0 → 0.54.0, uvicorn-worker 0.3.0 → 0.4.0, orjson 3.11.9 → 3.13.0, msgpack
1.1.0 → 1.2.3, cryptography 45.0.6 → 50.0.2, python-multipart 0.0.20 → 0.0.32,
prometheus-client 0.22.1 → 0.26.0, PyJWT 2.13.0 → 2.15.1, psycopg2-binary
2.9.10 → 2.9.13, authlib 1.6.1 → 1.8.0, pillow 11.3.0 → 12.3.0, requests
2.32.4 → 2.34.2, python-dotenv 1.1.1 → 1.2.4 (plus transitive: anyio 4.15.1,
urllib3 2.8.0, idna 3.20, …). **redis-py stays 5.2.1** (hiredis 3.4.2): 7.4
costs +11..35 % instructions per command, 8.x +29..92 %, and no advisory
affects 5.2.1. argon2-cffi 25.1.0 and httpx 0.28.1 were already current.
Most of the bumps carry security fixes (python-multipart, cryptography,
PyJWT, msgpack, starlette, authlib, pillow, urllib3, anyio, requests).

cachegrind, full ASGI stack of the platform app, instructions per request
(N = 1000; before → after): claims-only authz route 1.26 M → 1.14 M (−9 %),
50-item `list[Item]` response 3.38 M → 2.27 M (−33 %), `/health/simple`
0.72 M → 0.77 M and 404 0.59 M → 0.65 M (+8 / +10 %; with the GC frozen
+1 / +3 % (0.715 M → 0.725 M, 0.586 M → 0.602 M) — the rest is SQLAlchemy 2.1's larger import graph making
gen-2 collections dearer). ORM operations on SQLAlchemy 2.1.4 are within ±4 %
of 2.0 (reads −2..−4 %, insert / `text()` +2..+4 %).

- **The request UoW now commits BEFORE the response is sent.** FastAPI ≥ 0.118
  exits a yield dependency after the response unless it is declared with
  `scope="function"`; every framework UoW dependency (`DbSession`,
  `Database(name)`, the authz / identity / tenancy guards) now is, so a client
  never observes a `200` before the write and its on-commit hooks (token
  revocation) committed. A hand-written `Depends(get_db)` must pass
  `scope="function"` too (FastAPI caches per scope; mixing them opens a
  second UoW). Pinned by `test_uow_commits_before_response.py`.
- **PostgreSQL URLs always name the driver.** SQLAlchemy 2.1 maps a bare
  `postgresql://` to psycopg 3: migrations now use `postgresql+psycopg2://`
  (`to_sync_url` also accepts a bare `postgresql://` and rewrites asyncpg's
  `ssl=` to libpq `sslmode=`); `alembic.ini`'s placeholder is explicit.
- The `sqlalchemy.ext.mypy` plugin (removed in 2.1) is dropped from the mypy
  config; DML `rowcount` is read through a `CursorResult` cast.
- `gunicorn_conf.server_implementation()` reads the running loop (uvicorn
  ≥ 0.36 passes a loop factory instead of installing a policy); the boot log
  line resolves the worker's configured `loop` setting.
- `sandloom new` apps get an optional `fast` extra (`sandloom-framework[fast]`:
  hiredis, jsonschema-rs) that their `dev` extra includes and the generated
  Dockerfile installs (`--extra fast`); `packages/sandloom-framework` gains the
  same `fast` extra and the packages' dependency floors follow the backend.
- `benchmarks/` pins follow `uv.lock` (`check_versions` passes).
- Python 3.14: the locked set installs from wheels (`uv sync --frozen --python
  3.14`, no source builds) and the full suite passes on 3.14.8.

### Changed

- **Python floor 3.11** (framework, `packages/*`, examples, `sandloom new`
  template); `.python-version` and the images use **3.12**. ruff targets
  py311 (`datetime.UTC` etc. applied; `UP042` StrEnum rewrite deliberately
  ignored).
- Runtime deps: `uvicorn[standard]` (uvloop + httptools) and
  `uvicorn-worker`; gunicorn's worker class is
  `uvicorn_worker.UvicornWorker` (`uvicorn.workers` is deprecated upstream).
- **Production image** rebuilt around a uv-built `/opt/venv`: dependency
  layer cached on `uv.lock` (bind + cache mounts), dependencies
  byte-compiled at install, the sandloom package compiled separately, no uv / pip
  / setuptools / tests / `.env` in the runtime (new `.dockerignore`).
  `import sandloom.main` in the container: 2190 -> 940 ms CPU; rebuild after a
  code change 5.0 -> 2.7 s; image 441 -> 452 MB. Same layout for the
  `sandloom new` Dockerfile.
- `qrcode` / Pillow are imported only when a 2FA QR code is generated and
  `jsonschema` on the first payload validation (not at app import).
- CI: lint / format / mypy (cached) / architecture in a separate job parallel
  to a test matrix (3.11, 3.13; coverage on 3.12 with
  `COVERAGE_CORE=sysmon`) that runs `-n auto --dist loadfile`;
  `setup-uv@v10` (uv 0.12.24), `setup-python@v5`, `paths-filter@v3`.
- **Post-commit dispatch is off the request path.** `DbSession` /
  `Database(name)` / contract calls enqueue committed events to a per-process
  coalescer (publish in commit order, ONE batched mark-delivered UPDATE per
  database every <= 50 ms) instead of publishing and opening a second Unit of
  Work before the response. An in-process (memory) bus still receives events
  in line; `db_session()` still publishes and marks in line. At-least-once is
  unchanged (the relay delivers whatever the coalescer does not). Tests that
  assert `delivered_at` after a request `await drain_post_commit()`.
- **Pools**: `pool_pre_ping` (3 round trips per checkout) replaced by an
  idle-aware 1-round-trip ping; LIFO pools; `statement_timeout` /
  `idle_in_transaction_session_timeout` sent in the startup packet.
  `CONTRACT_DB_POOL_SIZE` / `_MAX_OVERFLOW` defaults 2 / 3 -> 5 / 5 (monolith
  compose app: 3 / 3).
- **Relay**: batched claim (`FOR UPDATE SKIP LOCKED`, per-batch token) and
  settle — 712 -> 6 round trips per batch of 50; no sleep after a full batch;
  the dead-letter sweep runs every 30 ticks.
- **Outbox indexes** (core migration `0005_outbox_partial_indexes`): full
  `ix_outbox_messages_pending` / `ix_outbox_messages_channel` replaced by
  partial `ix_outbox_messages_dead` / `ix_outbox_messages_delivered` (-33% WAL
  per insert + deliver).
- Module migrations drop duplicate indexes: tenancy `0003_tenancy`
  (`ix_tenancy_members_organization_id`), billing `0003_billing`
  (`ix_billing_credit_ledger_customer_id`).
- Tenant guard: no work on the ORM hot path when no `TenantScoped` model is
  mapped, and no loader criteria on statements that reach no tenant table.
- Billing: claims contributor 5 -> 2 statements, spend 5 -> 4, webhook mark 2
  -> 1, `BillingProduct.prices` no longer eager (`raise_on_sql`; the catalog
  loads it), an org billing context with a minimum role makes ONE membership
  contract call (was two).

- `sandloom-scaffold` / `sandloom new` generate the new forms: `define_module`
  `__init__.py`, 2-line `migrations/env.py`, tests on `client` /
  `user_headers` / `make_headers` / `db`; `sandloom new` projects load the plugin
  from pytest addopts and no longer get a root `conftest.py`; app modules get
  no `tests/conftest.py` (framework-tree modules get a one-line one).
- The example apps (linkly, helpdesk, projecthub) use the new forms; their
  bespoke conftests are gone (helpdesk keeps only its seed fixtures).
- The bundled tenancy and billing modules use the new forms: `register =
  define_module(__name__, ..., setup=_setup)` (registration surface pinned by
  a test) and conftests reduced to their domain fixtures on top of the
  harness; their `test-imports` allowlist entries are removed.
- The bundled `identity` module is declared with `define_module` (same
  routers in the same order, event schemas, critical claims contributor and
  2FA-key startup guard — pinned by `test_module_registration.py`). Its test
  suite runs on the shared harness (conftest 481 → 286 lines) and imports no
  kernel internals, so its `test-imports` allowlist entry is gone.
- `sandloom new` projects and the example apps set
  `filterwarnings = ["error::DeprecationWarning"]`; the framework's own suite
  errors on DeprecationWarnings attributed to `sandloom.*` code.

### Performance (contract runtime, internal RPC, event consumers, Celery)

Measured with deterministic counters (cachegrind instructions, Postgres /
Redis round trips); budgets pinned in `tests/perf_budgets/`.

- **Request-scoped read scope for local read contracts.** Every HTTP request
  opens a lazy `ContractReadScope` (`ContractReadScopeMiddleware`, wired by
  `install_service_rpc_routes`; `contract_read_scope()` for workers/scripts):
  local read-contract calls on one database share ONE contract-pool
  connection/transaction (READ COMMITTED, always rolled back, never the
  caller's transaction — a provider still never sees the caller's uncommitted
  writes, as over RPC), and identical read calls (contract, method, bound
  tenant, arguments) are memoised for the request; any command / legacy call
  (local or remote) clears the memo. The connection returns to the pool as soon
  as the request awaits anything else (`CONTRACT_READ_SCOPE_IDLE_MS=0`).
  A policy check (`get_role` + `has_role_at_least` + a repeat): contract-pool
  work 3 checkouts / 3 BEGIN / 3 statements / 3 ROLLBACK → 1 / 1 / 2 / 1;
  CPU per request −49%. Opt out: `CONTRACT_READ_SCOPE_ENABLED=false`
  (`CONTRACT_READ_SCOPE_MEMO=false` keeps sharing without the memo).
  Behaviour note: within one request an identical read returns the memoised
  value even if another transaction committed a change in between (a command
  made by this request always invalidates it).
- **Wire codec v2 + RPC codec negotiation.** Fully typed contract parameters /
  results (no `Any`, no ambiguous unions) cross the boundary as
  annotation-driven JSON-native values in pydantic-core
  (`sandloom.core.contract_wire.WirePlan`); immutable exact-type values skip the
  local round trip. Local `list[Item]`×100 call: 31.2M → 8.3M instructions
  (−73%); local UUID-pair call 315k → 182k. The describe document carries
  `contract.codecs`; a proxy sends typed arguments (`"codec": 2`) and accepts
  a typed result (`X-SANDLOOM-RPC-Accept-Codec: 2;<hash>`, answered with
  `X-SANDLOOM-RPC-Codec: 2`) only with a provider that advertises codec 2 — old
  and new peers interoperate both ways (codec 1 stays supported).
- **Thin internal RPC route.** `/_sandloom/rpc/*` are plain Starlette routes and an
  outermost `RpcFastPathMiddleware` serves them before the HTTP middleware
  stack and FastAPI dependency resolution (internal token, signed context,
  body cap, error envelope, request id and HTTP RED metrics are handled by the
  handler). RPC UUID-pair call through the full app: 2.53M → 0.91M
  instructions (−64%).
- **Streams consumer batching.** Consumers read `EVENT_STREAM_READ_COUNT`
  (default 64, was 16) entries per `XREADGROUP`; a batch's inbox dedupe is one
  `MGET`, its marks one pipeline and its acks one multi-id `XACK`: Redis round
  trips per 64-event batch 194 → 5 (3.0 → 0.08 per event). Failure handling is
  per message as before (a failed delivery is neither marked nor acked); marks
  / acks of a batch's successes are written after the batch, so a crash
  mid-batch redelivers them (at-least-once, as documented).
- **Event schema validation** goes through `sandloom.core.contracts.payload_validation`
  — jsonschema-rs when installed (pure-Python `jsonschema` fallback; same draft
  detection and format policy), compiled once per schema. The payload-size
  guard measures compact `orjson` bytes (the transport's encoding) instead of
  `json.dumps` with spaces / ASCII escapes, so the cap is slightly more
  permissive. Error messages of the Rust engine differ in wording. Preflight
  per event (5-field schema): 292k → 34k instructions.
- **Claims collection** runs independent contributors concurrently (issuer
  contributors first, in the issuer's session; same merge order and "first
  critical failure in registration order aborts" result; sequential when a
  test session is bound).
- Contract runtime: signatures / return hints cached; bound contract methods
  and proxy methods built once per instance.
- **Celery:** apps built by the driver and the worker default to
  `task_ignore_result=True` (opt out per task with `ignore_result=False`) and
  `worker_prefetch_multiplier=1`; `enqueue` sends with `ignore_result=True`
  (no per-task result subscription) on a small dedicated executor instead of
  `asyncio.to_thread`. Idempotent tasks should opt in to
  `acks_late=True, reject_on_worker_lost=True`.

### Added (performance)

- `sandloom-framework[fast]` extra: `redis[hiredis]` (C reply parser; consumer CPU
  per 64-event batch −15–20%) and `jsonschema-rs`. The production image
  installs it; the `dev` extra includes it.
- `CacheProvider.get_many` / `set_many` (features `get_many`, `set_many`;
  default fallbacks loop over `get` / `set`; Redis: `MGET` / one pipeline).
- `MessageTransport.ack_many` (Redis: one multi-id `XACK`),
  `ConsumerInbox.processed_many` / `mark_processed_many`.
- `BaseEventBus.publish_event(..., prevalidated=True)` is documented for the
  post-commit dispatch of already-preflighted UoW events too.

### Fixed (examples)

- helpdesk's ticket service no longer passes its session to
  `tenancy.membership.v1` contract methods (deprecated and ignored; a
  `TypeError` once the shim is removed).

### Fixed

- `test_uow_outbox` (and one `test_kernel_hardening` case) patched a dead
  module-global event bus and failed whenever an app had been built earlier
  in the same process (any API test first, or an xdist worker); they now bind
  the bus through the session / active-runtime seam like production.

- The Redis cache driver's default (and named-instance) client is now
  event-loop-aware: it is rebuilt when the running loop differs from the one
  it was created on (the stale client is closed best-effort), so a second
  loop in one process no longer fails with "Event loop is closed". Module
  conftests' `_reset_cache_client` workaround is obsolete.

### Reliability & performance — cache and auth hot paths

- **Bounded Redis cache client.** The `redis` cache driver's clients had no
  socket / connect timeout and an unbounded pool: a blackholed or hung Redis
  made every cache call await forever, so the fail-open / fail-closed
  branches of the rate limiter, token revocation, idempotency guard and login
  lockout never ran. Clients now carry `CACHE_SOCKET_TIMEOUT_SECONDS` (0.5),
  `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` (0.5), TCP keepalive,
  `CACHE_HEALTH_CHECK_INTERVAL_SECONDS` (30), a BLOCKING pool capped at
  `CACHE_MAX_CONNECTIONS` (64, waiting `CACHE_POOL_TIMEOUT_SECONDS` = 1.0) and
  a connection-error-only retry (`CACHE_RETRY_ATTEMPTS` = 1; timeouts are
  never retried). The default event-bus transport gets a sibling client
  without the read timeout (`get_default_transport_client`) — blocking
  `XREADGROUP` / pub-sub reads legitimately wait longer.
- **argon2 on a bounded pool.** Password hash/verify ran on the loop's
  default executor (`min(32, cpus + 4)` threads, ~64 MiB each): 32 concurrent
  logins peaked at 958 MiB RSS on a 10-CPU host. They now run on a dedicated
  pool of `PASSWORD_HASH_CONCURRENCY` threads (default `min(4, CPUs)`, cgroup
  aware) — 317 MiB for the same burst. `sandloom.core.di.run_password_hashing`.
- **passlib removed** (unmaintained; `crypt` DeprecationWarning on 3.11+).
  `sandloom.core.security` and identity's `AuthService` use argon2-cffi's
  `PasswordHasher` with the exact parameters passlib produced
  (`$argon2id$v=19$m=65536,t=3,p=4`, 16-byte salt, 32-byte digest): every
  stored hash verifies unchanged and does not need a rehash
  (`password_needs_rehash` / `AuthService.needs_rehash`). `passlib` /
  `types-passlib` are gone from the dependencies and the `sandloom new` wheel.
  `AuthService.pwd_context` is replaced by `AuthService.password_hasher`.
- **Fused cache operations** on `CacheProvider`, each with a portable default
  composed from the core ops (so every driver supports them) and a
  one-round-trip Redis implementation (features `incr_window`, `incr_many`,
  `set_nx_get`): the rate limiter's INCR+EXPIRE+GET-previous-window is one
  `EVALSHA` (2 → 1 round trips per limited request); the idempotency reserve
  is one `SET … NX GET` (Redis ≥ 7; degrades to SET NX + GET) — an idempotent
  first request costs 2 Redis commands instead of 3; a failed login bumps both
  lockout counters in one script. All Lua scripts now run via `EVALSHA`
  (script body sent once; `NOSCRIPT` reloads).
- **JWT verify** skips parsing the unverified header when no
  `JWT_PREVIOUS_SECRETS` are configured (the key is the current secret
  whatever the `kid`).
- **`scrub_text`** no longer rescans every position of long identifiers
  (lookbehind-anchored key + substring prefilters; output proven identical on
  a generated corpus): −58% instructions on a clean 80-char message,
  9.65 M → 79 k (~120x) on a 2 KB traceback.
- **identity migration `0003_identity`** drops the unselective boolean index
  `ix_refresh_tokens_is_revoked`: no query uses it (every `is_revoked`
  predicate is paired with an indexed `id` / `token` / `family_id` /
  `user_id`; the cleanup filters on `revoked_at`), it only cost an index
  write on every token mint and revocation. Run `sandloom-migrate upgrade-all`.
- **Login does 2 fewer Postgres statements** (9 → 7): the unused
  `selectinload(User.user_roles)` is gone and identity's claims contributor
  resolves roles + permissions in one LEFT JOIN.
- **Revocation freshness policy** (`sandloom.core.auth_freshness`, re-exported by
  `sandloom.core.di`). Revocation stays strict by default (every request reads the
  mark). A GET/HEAD route may declare
  `Depends(revocation_freshness(max_staleness_s=N))`, `0 < N <= 5`: a
  per-process copy of the mark (or its absence) at most N s old answers;
  `revoke_user_tokens` clears the revoking process at once and broadcasts on
  the Redis pub/sub channel `sandloom:auth:revocations`, other processes drop their
  copy on receipt, and a lost broadcast delays a revocation by at most N s.
  `create_app` refuses a budget on any other method (`RevocationPolicyError`);
  `strict_revocation()` (used by identity, tenancy and billing routers) and
  `AUTH_REVOCATION_STRICT_PATH_PREFIXES` make routes always strict (a budget
  there is ignored, logged). This replaces the global
  `AUTH_REVOCATION_LOCAL_TTL_SECONDS` knob of this release cycle (up to 300 s,
  every route), which is gone — the variable is now ignored (strict).
- **Emergency user block**: `block_user(user_id, reason=, actor_id=)` /
  `unblock_user(...)` (`sandloom.core.di`) write / remove
  `sandloom:auth:blocked:{user_id}` (no TTL), audited `auth.user.blocked` /
  `auth.user.unblocked` (`record_audit(required=True)`, so they must run in a
  UoW; a cache outage raises `BlockUnavailableError`, 503). The block mark is
  read live on EVERY authenticated request, whatever the route's budget
  (`401 "Account blocked"`); on a strict route it shares the revocation
  mark's round trip (`MGET`). Its cache outage follows
  `AUTH_REVOCATION_FAIL_CLOSED`. Issuers refuse a blocked user with a live
  check before any side effect — identity's password / 2FA login, SSO and
  SSO-2FA completion and the refresh exchange (`ensure_user_not_blocked`,
  `sandloom.core.di`). On commit the block also revokes the user's access tokens
  and publishes `core.auth.user_blocked.v1`; identity's new durable consumer
  `sessions@user_blocked` revokes every refresh token (works across the
  split: block from any service).
- Performance-budget tests (`tests/perf_budgets/`) pin the round trips:
  authenticated request = 1 Redis command (an `MGET` of the revocation and
  block marks; a single `GET` of the block mark on a budgeted route whose
  local copy is fresh), + 1 for a rate-limited route, idempotent first request ≤ 2 (+ auth), password login
  ≤ 3 Redis commands (+ 1 rate-limited) and ≤ 7 SQL statements.

## [0.4.0] — 2026-10-08

The split-deployment release: "one codebase, monolith or microservices by
configuration" is now verified for every module subset (the split conformance
matrix) and the review findings of that promise are fixed. Upgrade notes are
given with each breaking change.

### Breaking changes

**Contracts: identical semantics locally and over RPC**

- **A contract call is observably identical locally and over RPC.**
  Upgrade notes:
  - `register_service` now validates the provider: every PUBLIC member must be
    a `@contract_method` (or a deprecated legacy session-taking coroutine) —
    make helpers/state private (`_name`) — and every parameter and the return
    must be annotated with a contract wire type (`sandloom.core.contract_wire`:
    JSON scalars, `None`, `UUID`, `datetime`/`date`, `Decimal`, `Enum`,
    `Literal`, pydantic models, containers/unions of those, `Any`); `*args` /
    `**kwargs` are rejected. Violations raise `ContractTypeError` at
    registration (module load fails) instead of a remote `404`.
  - Local calls go through the same codec as RPC: arguments are bound with
    Python's rules and validated/coerced against the annotations, results are
    decoded against the return annotation (local callers now get exactly the
    types a remote caller gets; tuples/sets/Decimal/Enum/models survive RPC).
    A binding/validation failure is `ContractArgumentError` (422, a
    `TypeError`) on both paths, including unknown kwargs remotely.
  - `ContractProvider` (what `core.get_service` returns locally) exposes ONLY
    the contract's methods; private / non-contract attributes raise
    `AttributeError`.
  - A provider `HTTPException` / `http_status` exception now reaches a LOCAL
    caller as `ContractCallError` (an `HTTPException` subclass with the same
    status, JSON-encoded detail, headers and `code`) — exactly what the RPC
    client raises. `ContractIdempotencyKeyMissing` / `…Conflict` /
    `ContractCallInProgress` / `ContractIdempotencyUnavailable` are
    `ContractCallError` subclasses (still `ValueError` / `RuntimeError`).
  - Over RPC a domain `401`/`403` keeps its status (it was turned into a `503`
    "not authorized"); headers such as `Retry-After` are preserved; an unknown
    service/method at the provider is `501 ServiceUnavailableInDeployment`
    (it was a plain `404`); only transport/trust failures and provider crashes
    are `503 RemoteServiceUnavailable` (now with a `code`).
  - RPC keeps no idempotency record for reads, and none of its own for
    decorated commands (the contract runtime's dedupe, identical locally and
    remotely, covers them; TTL `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`, ≤ 7 days;
    results over 256 KiB are stored as a non-replayable marker →
    `ContractResultNotReplayable` 409 on a repeat). The proxy no longer invents
    a per-call idempotency key: legacy session-taking methods now really
    require an explicit `rpc_idempotency_key(...)` over RPC, and calls in one
    such block are keyed per (key, method, arguments) instead of colliding.
  - The proxy sends no signed context when there is neither a tenant nor a
    caller key, and a provider without `INTERNAL_API_TOKEN` in open-dev mode
    accepts an unsigned tenant-less context (every dev-mode RPC used to 503).

- **Adding an enum value is a breaking schema change.**
  `classify_schema_change` (schema registry, module-load preflight, contract
  diff) now reports added enum values as breaking, like removed ones: a
  consumer deployed in another service with the old schema rejects or
  mishandles the new value. *Upgrade:* publish the new shape as `.vN+1` and
  dual-publish (`publish_event_versions`) until every consumer moved.

**Eventing (split deployments)**

- **Consumer groups are namespaced by module: `<module>:<name>`.** Equally
  named consumers of two modules — or one module deployed in two services —
  used to share ONE Streams group and split each other's events (and, with
  their inboxes in different databases, double-apply after `XAUTOCLAIM`). The
  exactly-once inbox key follows the group. *Upgrade:* the new group is created
  at the old bare-name group's last-delivered position (no history replay);
  drain entries still pending in the old group (pin `group="<name>"`), then
  `XGROUP DESTROY` it. Consumers on a bare facade (tests/scripts) keep `name`.
- **Startup fails** on two consumers of one process resolving to the same
  group, and on a group already owned by another module / physical database
  (`sandloom:group-owner:<stream>:<group>`). After deliberately moving a module to
  another database: drain, then `sandloom streams release-group`.
- **Streams are trimmed**: `EVENT_STREAM_RETENTION_SECONDS` (default 7 days,
  `XTRIM MINID ~`) and optional `EVENT_STREAM_MAXLEN` (`MAXLEN ~`).
- **DLQ replay targets the dead-lettering group only** (other groups ack-skip
  the replayed entry); `sandloom streams dlq-replay --group`.
- **`sandloom projection rebuild` no longer enters the ASGI lifespan** (it joined
  live consumer groups and, in prod, started the relay); it runs the startup
  tasks only. Run it as a job after the source services are up.
- Post-commit dispatch publishes one unit of work's events sequentially in
  emission order (stops at the first failure; the relay delivers the rest).

**Access facts, billing tenant resolution, token revocation**

- **Access facts are split-safe.** Registering a fact exposes the owner's
  `<owner>.facts.v1` contract (`describe()` with a declarative `ClaimRule`,
  `evaluate()` for live answers). `Entitled(...)` / `Subscribed()` in a process
  without the owner resolve the fact through the bound contract (cached 300 s)
  and decide exactly like the monolith — previously `500 fact_unregistered`.
  An unregistered/unbound fact is now indeterminate `501` (was `500`); a bound
  but unreachable owner is `503`. `AccessFact(claim_rule=...)` is the new
  declarative interpretation (`claim_interpret` still works locally, but such
  a fact is offered to other processes without claim mode); billing's
  `subscription.active` uses it, so `shared` no longer duplicates billing's
  `past_due` grace rule (with no subscription owner deployed, `Subscribed()`
  accepts only `active`/`trialing`).
- `Subscribed()` honours provenance: when the token's authoritative
  provenance shows billing did not contribute (e.g. `claims_sources_failed`),
  it escalates to billing's live answer instead of denying 402 for the token's
  lifetime (indeterminate 503 without a live path).
- Billing's tenant resolution no longer falls back SILENTLY to the personal
  tenant when `tenancy.membership.v1` is neither local nor bound but the token
  shows tenancy is deployed (`tenant_ids`/`tenant_id`/`org_id` claim or
  `tenancy` provenance): it answers `501`. An explicit `org_id` without the
  contract is now `501` (was `404`). Personal-only deployments (no tenant
  claims, no tenancy provenance) keep resolving the personal tenant.
- Token-revocation marks no longer expire with the WRITER's
  `ACCESS_TOKEN_EXPIRE_MINUTES`: they live `AUTH_REVOCATION_MARK_TTL_SECONDS`
  (new; default `max(access TTL + 120s, 24h)`), so in a split deployment a
  mark written by tenancy cannot expire before tokens minted by identity.

**Split deployment (probes, modules, bindings)**

- **Remote peers no longer affect `/health`, and affect `/ready` only when
  opted in.** `SERVICE_BINDINGS` entries are probed for readiness only with
  `"required": true` (previously every binding was "required" unless
  `"required": false`, and the probe ran in `/health` too — one stopped
  service made all its consumers unhealthy and not-ready). Upgrade: add
  `"required": true` to the bindings a service truly cannot serve without.
  Probes are concurrent, capped by `SERVICE_PROBE_TIMEOUT_SECONDS` (default
  1 s) and use the binding's own `internal_token`; `/diagnostics/services`
  reports every binding's reachability.
- **Modules listed explicitly in `SANDLOOM_MODULES` are required by default**
  (when `MODULE_REQUIRED` is empty): an import failure aborts startup.
  Upgrade: set `MODULE_REQUIRED` to the subset you need, or
  `MODULE_REQUIRE_LISTED=false` for the old behaviour. `/ready` is 503
  whenever modules were requested but none loaded.
- **`SERVICE_BINDINGS` is validated at startup**: unknown keys, non-`http(s)`
  URLs, bad `timeout_seconds` / `required` / `critical` values, and bindings
  with no internal token (global or per binding, outside the dev open-endpoint
  opt-in) are configuration errors.
- **`ModuleDescriptor.consumes`** (`ContractRequirement(name, required=,
  reason=)`): the loader checks at startup that every consumed contract is
  provided locally or bound in `SERVICE_BINDINGS` — a missing required one
  aborts startup (`ContractBindingError`), a missing optional one warns; the
  result is in `/diagnostics/core` (`modules.consumes`). Billing declares
  `tenancy.membership.v1` (optional).

**Deploy profiles**

- The microservices compose binds exactly each service's consumed contracts
  (identity binds only the claims contracts it collects, tenancy binds
  nothing, billing binds `tenancy.membership.v1`); a module added to a service
  must declare and bind what it calls.
- Postgres `max_connections` is set explicitly to 150 in both profiles; the
  contract pool is `CONTRACT_DB_POOL_SIZE=1` / `CONTRACT_DB_MAX_OVERFLOW=2`
  and beat runs with 1 + 0 pools (the stock defaults exhausted
  `max_connections` 100 at peak). Raising concurrency or pools means raising
  `max_connections` too (deploy/README "Sizing", pinned by
  `test_deploy_profiles`).
- The gateway 404s every unrouted path and no longer 301-redirects
  `/api/v1/billing`.

### Added

- **Split conformance matrix** (`tests/split_conformance`, marker
  `split_conformance`, part of the default suite and required in CI): boots
  every non-empty subset of identity/tenancy/billing as real processes —
  bound to a peer via `SERVICE_BINDINGS` and alone — and requires the bound
  variant to answer a request script (auth, orgs, members, invitations,
  billing, revocation, and `OrgRole` / `SubscriptionActive` / `Credits` /
  `Entitled` / `Subscribed` gates) identically to the monolith, the unbound
  one identically or with an explicit 404/501/503.
- `SANDLOOM_SERVICE_NAME` is set for every long-running process of both compose
  profiles.
- Eventing: consumer `start_from` (`earliest` | `latest` | timestamp) and
  `group=` pin on `register_event_consumer` / `ProjectionConsumerDescriptor`;
  opt-in `bind_tenant_from="tenant_id"` binds the payload tenant for the
  handler; `SANDLOOM_SERVICE_NAME` labels consumer ids / diagnostics;
  `EVENT_STREAM_PREFIX` namespaces every Streams key; per-group lag / pending
  (`sandloom_streams_group_lag`, `sandloom_streams_group_pending`, `/diagnostics/consumers`
  fields `group`, `module`, `service`, `lag`, `pending`); a Redis subscription
  inventory with `sandloom streams consumers` / `sandloom streams groups`; a publish-side
  warning + counters when no live consumer reads a channel or a sibling
  version starves; `publish_event_versions` (from `sandloom.core.di`) for v1 → v2
  dual-publishing.
- Framework worker sizing: `worker_concurrency` from `WORKER_CONCURRENCY` or
  the container CPU quota (capped at 2) instead of Celery's host-core default;
  `WORKER_MAX_MEMORY_PER_CHILD` / `WORKER_MAX_TASKS_PER_CHILD`; a heartbeat
  file + `python -m sandloom.core.worker healthcheck` liveness check.
- Deploy profiles: one named image (`SANDLOOM_IMAGE`), worker healthchecks and
  512 MiB limits, persistent `noeviction` Redis in the monolith too, shared
  auth/notifier/past-due settings in the common env anchor, parametrized
  gateway port / subnet / IP; the nginx gateway re-resolves services via
  Docker DNS, fails fast (2 s connect timeout), caps bodies at 50 MiB like the
  app, no longer 301-redirects `/api/v1/billing` and 404s every unrouted path.
  The smoke test adds a worker-task check and a peer-down scenario.
- Contract wire codec `sandloom.core.contract_wire` and the RPC describe document
  (`GET /_sandloom/rpc/{service}` → `contract: {hash, methods}`; responses carry
  `X-SANDLOOM-Contract-Hash`, the proxy re-describes on change).
- Contract conformance harness `sandloom.core.contract_testing`
  (`assert_contract_conformance`: local vs in-process RPC); applied to every
  shipped contract.
- Dedicated contract DB pool per database instance (`CONTRACT_DB_POOL_SIZE`,
  `CONTRACT_DB_MAX_OVERFLOW`) with `lock_timeout`
  (`CONTRACT_DB_LOCK_TIMEOUT_MS`): an in-process contract call / claims
  contributor no longer starves the request pool (a request pool of 1 used to
  deadlock until the pool timeout).

### Fixed

- Projecthub taskboard: a project update racing a task insert no longer
  strands stale project fields (local `taskboard_project_snapshot` serialises
  both writers; rebuild re-fans project fields). Docs: CONTRACTS.md no longer
  calls new enum values non-breaking for cross-service consumers; event
  ordering (none, even within a channel) is documented.
- `register_live_adapter` is documented as consumer-side configuration and
  warns when a provider module registers an adapter for its own contract.

## [0.3.0] — 2026-10-08

The security / correctness hardening release (review findings). Upgrade notes
are given with each breaking change.

### Breaking changes

**Kernel and contracts**

- **`create_app` moved into the kernel** (`sandloom.core.app_factory`, shipped in
  `sandloom-framework`). `sandloom/main.py` is only the batteries composition root; an
  `sandloom new` app builds on the kernel factory. Workers, migrations and the ops
  CLIs boot the composition root named by `SANDLOOM_COMPOSITION_ROOT` (default
  `sandloom.main`; `sandloom new` apps use `<app>.asgi`).
- **Contract methods no longer take the caller's session.** Every
  service-contract call (local or RPC) runs in the PROVIDER's own Unit of Work
  on its own database (`@contract_method` + `contract_session()`); a caller
  that still passes a session gets a `DeprecationWarning` and it is ignored.
  Commands (`@contract_method(command=True, idempotency_key=...)`) are not
  atomic with the caller's transaction (saga).
- **Command contracts are deduplicated by the framework, fail-closed.** The
  runtime records `(contract, method, tenant, idempotency key) -> (args
  digest, result)`: a repeat replays the stored result, the same key with
  other arguments raises `ContractIdempotencyConflict` (409), a concurrent
  repeat `ContractCallInProgress` (503) and an unavailable idempotency store
  `ContractIdempotencyUnavailable` (503) — a command never runs without the
  store. RPC-level dedupe of commands is fail-closed too (it failed open).
- **Legacy undecorated session-taking contract methods** are treated as
  commands (with a `DeprecationWarning`) and now REQUIRE an idempotency key
  when invoked over internal RPC (`422` without one). **They will be removed in
  the next minor release** — decorate them with `@contract_method`.
- **Internal RPC error mapping.** Only a failed method lookup is `404` and only
  argument binding/validation is `422`; an `AttributeError` / `ValueError`
  raised inside a provider is now a provider error (`500`), and exceptions with
  the `http_status` protocol keep their status.
- **Tokens require `iat`** (and `exp`); access tokens must carry
  `token_type: "access"` everywhere — including the idempotency principal and
  tenant identity (one bearer parser: `sandloom.core.security.bearer_claims`).
- **Token revocation after commit**: use
  `sandloom.core.di.revoke_user_tokens_on_commit(user_id, reason=...)`; the
  revocation mark is written atomically (CAS) and never moves backwards.
- **`sandloom check` gained the `kernel-handles` rule** (`core.event_bus`,
  `core.infra`, `core.service_registry`, `app.state.core` from module code).
  Applications that reached kernel handles now fail the gate.
- **`sandloom.core.module_api._contract_target`** carries the contract name (only
  relevant to code that set this private context variable).

**Production configuration** (startup fails otherwise)

- `TWOFA_ENCRYPTION_KEY` is required wherever identity is loaded (no
  `JWT_SECRET` fallback in prod).
- `BILLING_PROVIDER` must be chosen explicitly; the fake PSP is refused in
  prod unless `BILLING_ALLOW_FAKE_PSP_IN_PROD=true` (demos only); `stripe`
  requires `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET`.
- `INTERNAL_API_TOKEN` is required for split deployments: it authenticates
  internal RPC (signed caller context) and unlocks the internal endpoints.

**Identity**

- `POST /auth/register` always answers **`202`** with the same body (no
  account enumeration); password accounts sign in only after email
  verification (`403` before).
- 2FA management endpoints (`/auth/2fa/init`, `/enable`, `/disable`,
  `/backup-codes/regenerate`) require **`current_password`** (password-less
  accounts: an access token issued in the last 300 s).
- **SSO flow**: `/redirect` returns `{auth_url, state, binding}` and sets the
  `sandloom_sso_binding` cookie; the callback must present the binding (cookie or
  `X-SSO-Binding`), uses PKCE / OIDC nonce, and a 2FA account receives an MFA
  challenge to finish with `POST /auth/sso/mfa`. An email auto-link to a 2FA
  account is written only after the second factor; a mis-bound callback no
  longer consumes the state.
- **Refresh rotation grace window**: replaying a token rotated at most 10 s ago
  (or losing a concurrent refresh) while the family has a live successor
  answers **`409`** ("retry with the new token") instead of revoking the
  family; beyond the window reuse still revokes the family (`401`).
- **Lockout keyed per (account, client IP)** with a soft per-account delay
  across IPs (from 20 failures, up to 5 s per attempt) — a third party can no
  longer lock an account out. `lockout.ensure_not_locked` /
  `register_failure` / `reset_failures` take an optional `client_ip`.

**Tenancy**

- `PATCH /api/v1/orgs/{id}` no longer accepts `is_active` (`422`);
  deactivation is the owner-only delete.
- Accepting an invitation requires the token's `email` to match **and**
  `email_verified: true`.
- Only the **owner** may invite with `role: "admin"` (`403` for admins).

**Billing**

- The PSP client is no longer a service contract: **`billing.psp.v1` was
  removed** (use billing's own endpoints; tests use `install_psp_client`).
- Subscription / credits gates deny with **`402`** (payment required).
- `billing.credits.v1.spend`: an `"organization"` spend must match the bound
  tenant (`403`); personal (`"user"`) spends are not checked against it.
- Billing services raise domain errors (`sandloom.billing.exceptions`:
  `InsufficientCreditsError` 402, `CreditOperationConflictError` 409,
  `BillingTenantMismatchError` 403) instead of `HTTPException`; the HTTP
  statuses are unchanged and the contract still raises `HTTPException`.
  `CreditsService.get_balance` was removed (use the contract's `get_balance` /
  `EntitlementsService.get_credit_balance`).
- Credit-pack checkout webhooks require `metadata.customer_id` and an
  `amount_total` + `currency` equal to the catalog price; otherwise the event
  is journaled `failed` and nothing is granted.

### Added

- `sandloom check` — the framework's architecture analyzers as a CLI for apps.
- `sandloom new` emits per-module migration chains, worker/beat, CI and `sandloom check`;
  `sandloom-migrate revision <module>` for per-module chains.
- Framework Celery worker entrypoint (`sandloom.core.worker`, `run_async`).
- Signed RPC caller context (tenant + idempotency key) and idempotent RPC.
- Opt-in PostgreSQL row-level security (`tenant_rls` database feature).
- JWT key rotation (`JWT_KID`, `JWT_PREVIOUS_SECRETS`) and framework-wide token
  revocation (`revoke_user_tokens`, `revoke_user_tokens_on_commit`).
- `sandloom.core.testing.InMemoryCache` — a full `CacheProvider` (incl. `cas`) for
  unit tests.
- Optional PSP capability `retrieve_subscription` (Stripe + fake): webhook
  same-second ties apply the PSP's current subscription state.
- Two-process split-deployment e2e (`tests/e2e`); with
  `SANDLOOM_E2E_REQUIRED=1` (CI) missing infrastructure fails instead of skipping.
  CI also smoke-tests the microservices compose profile through the gateway
  (`deploy/smoke/microservices-smoke.sh`).

### Fixed

- Many security fixes across identity (SSO takeover/login CSRF/2FA bypass,
  TOTP replay, refresh reuse detection, single-use reset links), tenancy
  (org lifecycle, invitations) and billing (fail-closed webhooks, monotonic
  subscription events, catalog-based credit grants); fail-closed tenant
  isolation and internal endpoints; CAS-guarded idempotency transitions;
  durable event consumers with supervision and DLQ tooling.
