---
title: Configuration Tiers
---

# Configuration tiers

Generated from `sandloom.core.config.tiers`. Every setting belongs to one tier: **semantic** (changes a guarantee or security property; its value is chosen per profile below), **ops** (capacity and operations — pools, timeouts, TTLs, sizes, endpoints, credentials, backend selection). There is no migration tier: the framework carries no transitional flags.

Counts: **15 semantic** (ratcheted: may only go down; a new one needs an ADR), **187 ops**.

## Profile table (semantic tier)

`prod_strict` = `ENVIRONMENT=prod` + `PRODUCTION_PROFILE=multi_tenant_strict`. A value is the EFFECTIVE one when the setting is left unset. Setting one towards the weaker end is an *override*: logged as a WARNING at every production start, counted in `sandloom_config_override_active{setting,kind}`, listed by `/diagnostics/core` (`configuration.overrides`) and `sandloom doctor`. *(refused)*: a weaker value aborts startup in that profile. — : no framework opinion (a deployment or product choice).

| Setting | dev | test | prod | prod_strict | Meaning |
| --- | --- | --- | --- | --- | --- |
| `ENVIRONMENT` | `dev` | `test` | `prod` | `prod` | Selects the profile: prod flips every fail-open default to closed. |
| `PRODUCTION_PROFILE` | `standard` | `standard` | `standard` | `multi_tenant_strict` | multi_tenant_strict turns the multi-tenant trust model into startup checks. |
| `SANDLOOM_ACCEPTED_RISKS` | `[]` | `[]` | `[]` | `[]` | Stable ids of the weaker guarantees this deployment accepts (sandloom.core.config.accepted_risks); each is reported at every start. |
| `TENANCY_MODE` | — | — | — | `multi` (refused) | single binds one canonical tenant; the strict profile isolates many. |
| `INFRA.database.<instance>.tenant_rls` | — | — | — | `true` (refused) | PostgreSQL Row-Level Security under the ORM tenant guard. |
| `SECURITY_CACHE_INSTANCE` | `shared` | `shared` | `shared` | `separate` (refused) | Revocation marks / blocks / idempotency on a separate non-evicting Redis. |
| `AUTH_REVOCATION_FAIL_CLOSED` | `false` | `false` | `true` | `true` (refused) | A revocation-store outage rejects tokens (503) instead of accepting them. |
| `RPC_COMMANDS_REQUIRE_SIGNATURE` | `off` | `off` | `on` | `on` (refused) | Pair-MAC COMMAND calls also carry the caller's Ed25519 signature (KCI). |
| `RPC_REPLAY_GUARD` | `commands` | `commands` | `commands` | `commands` | Pair-MAC nonce replay guard shared across replicas (commands / all). |
| `CONTRACT_COMPAT_CHECK` | `ready` | `ready` | `ready` | `ready` (refused) | Rolling-upgrade check of bound providers' contracts against snapshots. |
| `INTERNAL_ENDPOINTS_OPEN_IN_DEV` | `false` | `false` | `false` | `false` | Opens /diagnostics, /_sandloom/rpc, /metrics without a token (never in prod). |
| `DOCS_ENABLED` | `true` | `true` | `false` | `false` | Serves /docs, /redoc and /openapi.json. |
| `OUTBOX_RELAY_ENABLED` | `off` | `off` | `on` (refused) | `on` (refused) | The outbox relay retries undelivered events (at-least-once); off in a prod app process needs the accepted risk relay_disabled. |
| `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` | `false` | `false` | `false` | `false` | A subscription without an operation intent grants entitlements. |
| `SMTP_USE_TLS` | `true` | `true` | `true` | `true` | STARTTLS / TLS to the SMTP server (credentials in clear without it). |

## Accepted risks (`SANDLOOM_ACCEPTED_RISKS`)

The one list of weaker guarantees a deployment accepts, e.g. `SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"]` (JSON list or comma-separated). An unknown id fails startup. Each accepted risk is logged as a WARNING at every start, exported as `sandloom_accepted_risk_active{risk}`, and listed by `/diagnostics/core` (`configuration.accepted_risks`) and `sandloom doctor`.

| Risk id | Weakens | Consulted by |
| --- | --- | --- |
| `fake_psp_in_prod` | BILLING_PROVIDER=fake with ENVIRONMENT=prod: fake checkout sessions, no real payment (demo stacks). | Settings validator (BILLING_PROVIDER) and billing's critical startup check (sandloom_billing.psp.psp_configuration_error) |
| `log_notifier_in_prod` | The log notifier advertises delivery channels in prod: mail and SMS nobody receives. | log notifier driver features (sandloom.core.drivers.log_notifier) |
| `local_storage_in_prod` | Local-filesystem object storage in prod: no replication, lost with the container. | local object-store driver (sandloom.core.drivers.local_objectstore) |
| `volatile_event_bus` | A volatile event bus (memory / Redis Pub/Sub): in prod the driver is admitted and events can be lost; outside prod durable consumers may run on Pub/Sub (lost or duplicated deliveries). | event-bus driver (sandloom.core.drivers.event_bus_driver) and the consumer runtime (sandloom.core.consumers.callbacks) |
| `unversioned_ddl_repair` | sandloom migrate ensure-outbox --force --apply in prod: DDL outside the versioned migration chain, no drift detection. | sandloom migrate ensure-outbox (sandloom.core.cli.migrate) |
| `weak_password_hash` | argon2id parameters below the OWASP minimum in prod: stolen password hashes are cheaper to crack. | Settings validator (PASSWORD_HASH_*) and the startup warning |
| `shared_hs_secret_split` | A split prod deployment keeps a shared HS* JWT secret: every service holding it can mint a token for any user. Refused in prod_strict even when accepted. | startup token-signing role check (sandloom.core.auth.tokens.token_signing_problem) |
| `relay_disabled` | OUTBOX_RELAY_ENABLED=off in a prod app process: no relay retries the outbox, so delivery degrades from at-least-once to the post-commit dispatch alone (a failed dispatch is never retried). | app startup check (sandloom.core.app_factory.outbox_relay_problem) |
| `command_dedupe_below_retry_horizon` | CONTRACT_COMMAND_DEDUPE_TTL_SECONDS below the outbox relay's redelivery horizon in prod: a command retried after its dedupe record expired runs a second time. | app startup check (sandloom.core.guarantee_windows.enforce_guarantee_windows) |
| `saga_horizon_beyond_command_dedupe` | A saga command step can be retried or resolved later than CONTRACT_COMMAND_DEDUPE_TTL_SECONDS after its first attempt in prod: a late retry after the dedupe record expired runs the step twice. | app startup check (sandloom.core.guarantee_windows.enforce_guarantee_windows) |

## Removed settings

No longer settings. Still configured with the inert value they log a warning; with any other value startup fails.

| Setting | Removed in | Inert value | Instead |
| --- | --- | --- | --- |
| `SCHEMA_REQUIRE_ALL_VERSIONED` | 0.8 | `true` | every publish to a versioned channel needs a registered schema in every profile (dev = prod; SANDLOOM-R003) |
| `LEGACY_UNSCOPED_EFFECTS` | 0.8 | `false` | publish_event / record_audit outside a Unit of Work always raise unless the call passes required=False |
| `DB_LAZY_BEGIN` | 0.8 | `false` | SELECTs always run in the UoW transaction; declare ReadOnlyDbSession (DB_READ_ONLY_SESSIONS) or a PlannedRead for optimised reads |
| `SCHEMA_VALIDATION_SOFT` | 0.8 | `false` | an off-schema event is always refused before the outbox (dev = prod) |
| `MODULE_REQUIRE_LISTED` | 0.8 | `true` | an explicitly listed module is required unless MODULE_REQUIRED names the modules that must load |
| `BEAT_LEASE_ENABLED` | 0.8 | `true` | the beat singleton lease is always on (BEAT_LEASE_CACHE_INSTANCE must be a cache with the 'cas' feature) |
| `CONTRACT_READ_SCOPE_MEMO` | 0.8 | `true` | read contracts are memoised per request; opt out per scope with contract_read_scope(memo=False) |
| `BILLING_FAKE_ALLOW_UNSIGNED_WEBHOOKS` | 0.8 | `false` | the fake PSP always rejects unsigned webhooks; sign test payloads with sandloom_billing.psp.sign_stripe_payload |
| `BILLING_ALLOW_FAKE_PSP_IN_PROD` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["fake_psp_in_prod"] |
| `NOTIFIER_ALLOW_LOG_IN_PROD` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["log_notifier_in_prod"] |
| `STORAGE_ALLOW_LOCAL_IN_PROD` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["local_storage_in_prod"] |
| `SANDLOOM_ALLOW_VOLATILE_CONSUMERS` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["volatile_event_bus"] |
| `SANDLOOM_ALLOW_UNVERSIONED_DDL_REPAIR` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["unversioned_ddl_repair"] |
| `PASSWORD_HASH_ALLOW_WEAK` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["weak_password_hash"] |
| `JWT_ALLOW_SHARED_SECRET_SPLIT` | 0.8 | `false` | accept the risk explicitly: SANDLOOM_ACCEPTED_RISKS=["shared_hs_secret_split"] |
| `RPC_AUTH_MODE` | 0.8 | `service_mac` | internal RPC is always pair-MAC authenticated (ADR-0004): give every service RPC_DH_PRIVATE_KEY / RPC_SERVICE_DH_KEYS (and RPC_PRIVATE_KEY to sign commands); INTERNAL_API_TOKEN only guards the ops endpoints |
| `RPC_RESPONSE_BINDING` | 0.8 | `strict` | every RPC response is keyed to its request and pinned provider (the former strict mode) |
| `AUTH_REVOCATION_STRICT_PATH_PREFIXES` | 1.0 | `[]` | every authenticated request reads the subject-state record live (ADR-0009): there is no staleness budget left to override | <!-- stale-ok: removed setting documented for migration -->
| `MODULE_FS_SCAN` | 0.7 | `false` | modules are no longer packages under sandloom.*: list them in SANDLOOM_MODULES (import path or `sandloom.modules` entry-point name) or set MODULE_LOAD_ENTRYPOINTS=true to load every installed module |

## Ops tier

Every other setting in the [configuration reference](configuration.md), every other `INFRA` instance option, and the process environment read by entrypoints: `SECRETS_DIR`, `SANDLOOM_WORKER_TASKQUEUE`, `SANDLOOM_WORKER_COUNT`, `WEB_CONCURRENCY`, `GUNICORN_BIND`, `GUNICORN_ACCESSLOG`, `GUNICORN_TIMEOUT`, `GUNICORN_GRACEFUL_TIMEOUT`, `GUNICORN_KEEPALIVE`, `PROMETHEUS_MULTIPROC_DIR`.

`ACCESS`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `AUTH_REVOCATION_CLOCK_SKEW_SECONDS`, `AUTH_REVOCATION_MARK_TTL_SECONDS`, `AUTH_REVOCATION_RETRY_BASE_SECONDS`, `AUTH_REVOCATION_WRITE_ATTEMPTS`, `BEAT_LEASE_CACHE_INSTANCE`, `BEAT_LEASE_NAME`, `BEAT_LEASE_TTL_SECONDS`, `BILLING_DENY_ON_PAST_DUE`, `BILLING_INTENT_RECONCILE_AFTER_MINUTES`, `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS`, `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES`, `BILLING_PROVIDER`, `BROKER_URL`, `CACHE_HEALTH_CHECK_INTERVAL_SECONDS`, `CACHE_MAX_CONNECTIONS`, `CACHE_POOL_TIMEOUT_SECONDS`, `CACHE_RETRY_ATTEMPTS`, `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS`, `CACHE_SOCKET_TIMEOUT_SECONDS`, `CACHE_URL`, `CONSUMER_RETRY_BASE_SECONDS`, `CONSUMER_RETRY_MAX_SECONDS`, `CONSUMER_STARTUP_TIMEOUT_SECONDS`, `CONTRACT_CACHE_SHADOW`, `CONTRACT_CACHE_SHADOW_MAX_ENTRIES`, `CONTRACT_CACHE_SHADOW_REPORT_DIR`, `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`, `CONTRACT_DB_LOCK_TIMEOUT_MS`, `CONTRACT_DB_MAX_OVERFLOW`, `CONTRACT_DB_POOL_SIZE`, `CONTRACT_DTO_PACKAGES`, `CONTRACT_READ_SCOPE_ENABLED`, `CONTRACT_READ_SCOPE_IDLE_MS`, `CONTRACT_SNAPSHOT_PATHS`, `CORS_ORIGINS`, `DATABASE_SYSTEM_URL`, `DATABASE_URL`, `DB_CONNECT_TIMEOUT_SECONDS`, `DB_DEAD_CONNECTION_RETRY`, `DB_IDLE_IN_TX_TIMEOUT_MS`, `DB_MAX_OVERFLOW`, `DB_POOL_FAIR_BARGE_MS`, `DB_POOL_OVERFLOW_LINGER_SECONDS`, `DB_POOL_PING_IDLE_SECONDS`, `DB_POOL_SIZE`, `DB_POOL_TIMEOUT`, `DB_READ_ONLY_SESSIONS`, `DB_RECONNECT_BASE_SECONDS`, `DB_RECONNECT_MAX_SECONDS`, `DB_RLS_MERGED_BEGIN`, `DB_SESSION_OWNER_CHECKS`, `DB_STATEMENT_TIMEOUT_MS`, `EVENT_BUS_BACKEND`, `EVENT_BUS_REDIS_SEMAPHORE_LIMIT`, `EVENT_CALLBACK_TIME_BUDGET_SECONDS`, `EVENT_HANDLER_TIMEOUT_SECONDS`, `EVENT_PAYLOAD_MAX_BYTES`, `EVENT_SHUTDOWN_DRAIN_SECONDS`, `EVENT_STREAMS_RECONNECT_BASE_SECONDS`, `EVENT_STREAMS_RECONNECT_MAX_SECONDS`, `EVENT_STREAM_MAXLEN`, `EVENT_STREAM_PREFIX`, `EVENT_STREAM_READ_COUNT`, `EVENT_STREAM_RETENTION_SECONDS`, `FRONTEND_URL`, `GC_FREEZE_AFTER_STARTUP`, `GC_METRICS_ENABLED`, `GC_THRESHOLDS`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `HEALTH_CACHE_TTL_SECONDS`, `HEALTH_CHECK_TIMEOUT_SECONDS`, `IDEMPOTENCY_CACHE_INSTANCE`, `INFRA`, `INTERNAL_API_TOKEN`, `JWT_ALGORITHM`, `JWT_AUDIENCE`, `JWT_ISSUER`, `JWT_KID`, `JWT_LEEWAY_SECONDS`, `JWT_PREVIOUS_SECRETS`, `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEYS`, `JWT_SECRET`, `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LOG_FORMAT`, `LOG_LEVEL`, `MAX_REQUEST_BODY_BYTES`, `MAX_SESSION_DAYS`, `MAX_UPLOAD_SIZE_BYTES`, `METRICS_TOKEN`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MIGRATION_LOCK_TIMEOUT_SECONDS`, `MODULE_BLACKLIST`, `MODULE_FAIL_FAST`, `MODULE_LOAD_ENTRYPOINTS`, `MODULE_REQUIRED`, `MODULE_WHITELIST`, `NOTIFIER_BACKEND`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_SERVICE_NAME`, `OUTBOX_RELAY_BACKOFF_BASE_SECONDS`, `OUTBOX_RELAY_BACKOFF_MAX_SECONDS`, `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS`, `OUTBOX_RELAY_CIRCUIT_MAX_SECONDS`, `OUTBOX_RELAY_MAX_ATTEMPTS`, `OUTBOX_RELAY_STALL_SECONDS`, `OUTBOX_RETENTION_HOURS`, `PASSWORD_HASH_CONCURRENCY`, `PASSWORD_HASH_MEMORY_KIB`, `PASSWORD_HASH_PARALLELISM`, `PASSWORD_HASH_PROFILE`, `PASSWORD_HASH_TIME_COST`, `PLANNER_CURSOR_SECRET`, `PLANNER_LIST_MAX_LIMIT`, `PLANNER_OPTIMIZED_EXECUTOR`, `PROJECTION_RYW_WAIT_MS`, `PROJECTION_TOKEN_KEYS`, `RATE_LIMIT_ENABLED`, `RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES`, `RATE_LIMIT_GLOBAL_PERIOD_SECONDS`, `RATE_LIMIT_GLOBAL_REQUESTS`, `REFRESH_TOKEN_EXPIRE_DAYS`, `RPC_ALLOWED_CALLS`, `RPC_AUTH_MAX_SKEW_SECONDS`, `RPC_DH_EXTRA_PRIVATE_KEYS`, `RPC_DH_KEY_ID`, `RPC_DH_PRIVATE_KEY`, `RPC_KEY_ID`, `RPC_MAX_REQUEST_BYTES`, `RPC_MAX_RESPONSE_BYTES`, `RPC_POOL_IDLE_SECONDS`, `RPC_POOL_MAX_CONNECTIONS`, `RPC_PRIVATE_KEY`, `RPC_REPLAY_CACHE_MAX_ENTRIES`, `RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES`, `RPC_SERVICE_DH_CURRENT_KIDS`, `RPC_SERVICE_DH_KEYS`, `RPC_SERVICE_ID`, `RPC_SERVICE_KEYS`, `RPC_TRANSPORT`, `S3_ACCESS_KEY_ID`, `S3_BUCKET`, `S3_REGION`, `S3_SECRET_ACCESS_KEY`, `SAGA_COMPENSATION_DEADLINE_SECONDS`, `SAGA_RETENTION_DAYS`, `SAGA_RETRY_BASE_SECONDS`, `SAGA_RETRY_MAX_SECONDS`, `SAGA_SWEEP_BATCH`, `SAGA_SWEEP_INTERVAL_SECONDS`, `SANDLOOM_COMPOSITION_ROOT`, `SANDLOOM_JSONSCHEMA_BACKEND`, `SANDLOOM_MODULES`, `SANDLOOM_SERVICE_NAME`, `SCHEMA_REGISTRY_PATH`, `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `SERVICE_BINDINGS`, `SERVICE_PROBE_TIMEOUT_SECONDS`, `SERVICE_RPC_TIMEOUT_SECONDS`, `SHUTDOWN_BUDGET_SECONDS`, `SHUTDOWN_TASK_TIMEOUT_SECONDS`, `SINGLE_TENANT_ID`, `SMTP_FROM`, `SMTP_HOST`, `SMTP_PASSWORD`, `SMTP_PORT`, `SMTP_USERNAME`, `SSO_ALLOWED_REDIRECT_URIS`, `STARTUP_TASK_TIMEOUT_SECONDS`, `STORAGE_PATH`, `STORAGE_TYPE`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TENANT_CLAIMS_MAX_ORGANIZATIONS`, `TRACING_URL`, `TRUSTED_PROXY_IPS`, `TRUST_PROXY_HEADERS`, `TWOFA_ENCRYPTION_KEY`
