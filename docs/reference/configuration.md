---
title: Configuration Reference
---

# Configuration Reference

Every runtime setting Sandloom reads from the environment, generated directly from `sandloom.core.config.settings.Settings` so this table can never drift from the code. Settings may be supplied as environment variables, in a `.env` file, or — when `SECRETS_DIR` is set — as files named after each field (Docker/Kubernetes secrets).

!!! info "Required settings"
    `DATABASE_URL`, `CACHE_URL`, `BROKER_URL`, and `JWT_SECRET` are mandatory; the process aborts at startup if any is missing or invalid. In production, `JWT_SECRET` must be at least 32 random characters.

!!! note "Configuration tiers"
    Each setting is **semantic** (changes a guarantee; its value is chosen per profile), **ops** (capacity / operations) or **migration** (transitional, with a removal version). The per-profile table of the semantic settings and the active-override reporting are in [Configuration Tiers](configuration-tiers.md).

## Core & Modules

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `ENVIRONMENT` | `<enum 'Environment'>` | `'dev'` | Runtime environment: dev, test or prod. Production activates stricter startup requirements and production defaults. |
| `SANDLOOM_MODULES` | `list[str]` | `[]` | JSON list of module import paths or installed entry-point names loaded by this process; an application composition root may supply defaults. |
| `MODULE_WHITELIST` | `list[str]` | `[]` | Optional module allowlist applied by the loader before registration. |
| `MODULE_BLACKLIST` | `list[str]` | `[]` | Modules excluded by the loader before registration. |
| `MODULE_FAIL_FAST` | `bool` | `False` | Stop module loading on the first registration/import failure rather than collecting later failures. |
| `MODULE_LOAD_ENTRYPOINTS` | `bool` | `False` | Also load installed sandloom.modules entry points beyond the explicitly configured module list. |
| `MODULE_REQUIRED` | `list[str]` | `[]` | Required module names among the requested modules; empty normally makes every requested non-optional module required. |
| `SANDLOOM_COMPOSITION_ROOT` | `str | None` | `None` | Import path of the module exposing the application app/core for workers, migrations and operator commands; unset uses sandloom.main. |

## Infrastructure

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `INFRA` | `dict[str, dict[str, dict[str, object]]]` | `{}` | JSON map of capabilities to named driver instances and options; merges with default instances synthesized from flat settings. |
| `DATABASE_URL` | `str` | **required** | Connection URL of the default application database; production relational deployments use PostgreSQL. |
| `DB_POOL_SIZE` | `int` | `10` | Persistent connections per request/worker database pool; multiply the pool budget by the number of worker processes. |
| `DB_MAX_OVERFLOW` | `int` | `20` | Additional temporary connections permitted above DB_POOL_SIZE. |
| `DB_POOL_TIMEOUT` | `int` | `30` | Maximum seconds a database checkout waits for an available pooled connection. |
| `CACHE_URL` | `str` | **required** | Connection URL of the default cache instance, normally Redis; named instances and overrides are declared through INFRA. |
| `BROKER_URL` | `str` | **required** | Connection URL used to configure the default task queue broker and Redis-backed event transport. |

## Eventing & Outbox

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `CONSUMER_STARTUP_TIMEOUT_SECONDS` | `float` | `15.0` | Seconds allowed for required event consumers to establish their initial subscriptions. |
| `CONSUMER_RETRY_BASE_SECONDS` | `float` | `1.0` | Initial backoff delay in seconds when supervised event consumers retry a failed subscription. |
| `CONSUMER_RETRY_MAX_SECONDS` | `float` | `30.0` | Maximum backoff delay in seconds for supervised event-consumer subscription retries. |
| `EVENT_BUS_BACKEND` | `Literal['auto', 'memory', 'redis', 'streams']` | `'auto'` | Event transport selector: auto chooses durable Streams in production and in-memory delivery elsewhere; volatile production transports require accepted risk. |
| `SANDLOOM_SERVICE_NAME` | `str | None` | `None` | Logical service label attached to Streams consumer inventory and ownership diagnostics; does not rename a module consumer group. |
| `EVENT_STREAM_RETENTION_SECONDS` | `int` | `604800` | Approximate maximum Streams entry age in seconds, trimmed during publish; zero disables age trimming. |
| `EVENT_STREAM_MAXLEN` | `int` | `0` | Approximate maximum entries retained per channel stream; zero disables length trimming. |
| `EVENT_STREAM_PREFIX` | `str` | `""` | Namespace prepended to Streams, dead-letter and consumer-inventory keys; changing it starts a separate stream namespace. |
| `OUTBOX_RELAY_ENABLED` | `Literal['auto', 'on', 'off', 'delegated']` | `'auto'` | Outbox relay policy: auto enables production relaying, on forces it, off disables it, and delegated is reserved for framework-elected worker leadership. |
| `OUTBOX_RELAY_MAX_ATTEMPTS` | `int` | `10` | Failed publish attempts before an outbox row is dead-lettered; broker-outage circuit pauses do not consume attempts. |
| `OUTBOX_RELAY_BACKOFF_BASE_SECONDS` | `float` | `2.0` | Base per-row outbox retry delay in seconds, increased exponentially with jitter. |
| `OUTBOX_RELAY_BACKOFF_MAX_SECONDS` | `float` | `300.0` | Maximum per-row outbox retry backoff in seconds. |
| `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` | `float` | `1.0` | Initial relay cooldown in seconds when the broker is unavailable. |
| `OUTBOX_RELAY_CIRCUIT_MAX_SECONDS` | `float` | `30.0` | Maximum broker-outage relay cooldown in seconds. |
| `EVENT_CALLBACK_TIME_BUDGET_SECONDS` | `float` | `0.0` | If > 0, each event bus callback execution time is measured and a warning is logged when exceeded (non-fatal). |
| `EVENT_PAYLOAD_MAX_BYTES` | `int` | `0` | If > 0, serialized payload size is checked before publish; ValueError raised if exceeded. |
| `EVENT_SHUTDOWN_DRAIN_SECONDS` | `float` | `5.0` | Max seconds to wait for in-flight event callbacks to finish on bus shutdown before cancelling the stragglers. Bounds shutdown so a hung callback cannot hang the process. <= 0 waits unbounded. |
| `EVENT_BUS_REDIS_SEMAPHORE_LIMIT` | `int` | `100` | Max concurrent callbacks for RedisEventBus to prevent overload. Adjust based on CPU/network capacity. |
| `SCHEMA_REGISTRY_PATH` | `str` | `'./data/schema_registry.json'` | Path to JSON file used to persist in-memory event schema registry. If empty, persistence is disabled. File will be created atomically on updates. |

## Service Registry & RPC

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SERVICE_BINDINGS` | `dict[str, dict[str, object]]` | `{}` | JSON map of remote contract IDs to provider bindings, including base_url and service_id. Optional required bindings affect readiness; binding keys are startup-validated. |
| `SERVICE_RPC_TIMEOUT_SECONDS` | `float` | `1.0` | Default timeout in seconds for a remote contract call unless its binding overrides it. |
| `CONTRACT_DTO_PACKAGES` | `list[str]` | `[]` | Additional packages allowed to provide contract wire DTOs, beyond sandloom.shared; the same packages must be importable by every caller. |
| `CONTRACT_COMPAT_CHECK` | `Literal['off', 'ready', 'strict']` | `'ready'` | Remote contract compatibility policy: ready degrades readiness, strict also aborts startup for reachable incompatible providers, and off skips the check. |
| `CONTRACT_SNAPSHOT_PATHS` | `list[str]` | `[]` | Additional contract snapshot files or directories; these override expected snapshots shipped in provider packages. |

## Authorization

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `ACCESS` | `dict[str, str]` | `{}` | JSON map overriding access-fact resolution modes (claim, projection, live or auto); each fact freshness floor is still enforced. |

## Security & JWT

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `PRODUCTION_PROFILE` | `Literal['standard', 'multi_tenant_strict']` | `'standard'` | Production trust profile: standard or multi_tenant_strict. The strict profile enforces tenant RLS, isolated security infrastructure and other startup guarantees. |
| `SANDLOOM_ACCEPTED_RISKS` | `list[str]` | `[]` | Explicit production guarantee exceptions as a JSON list or comma-separated risk IDs; unknown IDs fail startup and accepted risks are reported. |
| `SECURITY_CACHE_INSTANCE` | `str` | `'default'` | Named cache instance for security-critical revocation and block state, and by default idempotency records; use durable non-evicting Redis in production. |
| `TENANCY_MODE` | `Literal['multi', 'single']` | `'multi'` | Tenant-binding mode: multi derives the current tenant; single consistently binds the canonical SINGLE_TENANT_ID without disabling tenant filters. |
| `SINGLE_TENANT_ID` | `uuid.UUID | None` | `None` | Canonical tenant UUID required when TENANCY_MODE is single. |
| `JWT_SECRET` | `str | None` | `None` | Shared secret for symmetric JWT signing and verification. In asymmetric mode only the token issuer uses it for emailed tokens; verifier-only services must leave it unset. |
| `JWT_ALGORITHM` | `Literal['HS256', 'HS384', 'HS512', 'EdDSA', 'RS256', 'ES256']` | `'HS256'` | Accepted and issued JWT algorithm. Symmetric algorithms use JWT_SECRET; asymmetric algorithms use issuer private keys and published public keys. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `int` | `15` | Lifetime, in minutes, of newly issued access tokens. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `int` | `14` | Lifetime, in days, of newly issued refresh tokens, bounded by the absolute session lifetime. |
| `JWT_ISSUER` | `str | None` | `None` | Expected JWT issuer claim; when configured it is included in issued tokens and checked during verification. |
| `JWT_AUDIENCE` | `str | None` | `None` | Expected JWT audience claim; when configured it is included in issued tokens and checked during verification. |
| `JWT_LEEWAY_SECONDS` | `int` | `10` | Clock-skew tolerance in seconds when validating JWT time claims. |
| `INTERNAL_API_TOKEN` | `str | None` | `None` | Credential for operational diagnostics, contract metadata, health details and metrics through X-Internal-Token; never authenticates internal RPC. |
| `TRUST_PROXY_HEADERS` | `bool` | `False` | Honor forwarded client-IP headers only under the configured trusted-proxy policy. |
| `TRUSTED_PROXY_IPS` | `list[str]` | `[]` | JSON list of trusted proxy CIDRs; forwarded chains are walked from the immediate peer toward the first untrusted hop. |
| `RATE_LIMIT_ENABLED` | `bool` | `False` | Enable route rate limiting outside production; production always enforces configured route limits. |
| `TWOFA_ENCRYPTION_KEY` | `str | None` | `None` | Comma-separated TOTP encryption keys, newest first, for rotation; production identity requires explicit keys, while development can derive a fallback from JWT_SECRET. |

## Billing

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `BILLING_PROVIDER` | `Literal['fake', 'stripe']` | `'fake'` | Payment provider selector: fake for local/testing flows or stripe for real Stripe integration. |
| `STRIPE_SECRET_KEY` | `str | None` | `None` | Stripe API secret used by the Stripe payment-provider adapter. |
| `STRIPE_WEBHOOK_SECRET` | `str | None` | `None` | Stripe webhook signing secret used to authenticate incoming webhook payloads. |
| `BILLING_DENY_ON_PAST_DUE` | `bool` | `False` | Deny subscription entitlements while payment is past due; disabled preserves the configured past-due grace behavior. |

## Storage

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `STORAGE_TYPE` | `Literal['local', 's3', 'gcs', 'yc']` | `'local'` | Default storage driver selector; local files ship ready to use, while other selections require a matching installed and configured driver. |
| `STORAGE_PATH` | `str` | `'./data'` | Filesystem root used by local object storage. |
| `MAX_UPLOAD_SIZE_BYTES` | `int` | `52428800` | Maximum bytes accepted by Storage.save; zero disables this upload cap. |
| `MAX_REQUEST_BODY_BYTES` | `int` | `0` | Global Content-Length request-body cap in bytes; zero falls back to MAX_UPLOAD_SIZE_BYTES. |
| `S3_BUCKET` | `str | None` | `None` | Bucket name supplied to the S3 object-storage driver. |
| `S3_REGION` | `str | None` | `None` | Region supplied to the S3 object-storage driver. |
| `S3_ACCESS_KEY_ID` | `str | None` | `None` | Access-key ID supplied to the S3 object-storage driver. |
| `S3_SECRET_ACCESS_KEY` | `str | None` | `None` | Secret access key supplied to the S3 object-storage driver. |

## Notifications

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `NOTIFIER_BACKEND` | `Literal['log', 'smtp']` | `'log'` | Default notifier driver: log records delivery metadata without message contents; smtp sends email using SMTP settings. |
| `SMTP_HOST` | `str | None` | `None` | SMTP server hostname used by the email notifier. |
| `SMTP_PORT` | `int` | `25` | SMTP server TCP port used by the email notifier. |
| `SMTP_USERNAME` | `str | None` | `None` | Optional SMTP authentication username. |
| `SMTP_PASSWORD` | `str | None` | `None` | Optional SMTP authentication password. |
| `SMTP_FROM` | `str | None` | `None` | Sender email address used by the SMTP notifier. |
| `SMTP_USE_TLS` | `bool` | `True` | Upgrade SMTP with STARTTLS and certificate verification; disable only for a trusted local development inbox. |

## SSO

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `GOOGLE_CLIENT_ID` | `str | None` | `None` | OAuth client ID for Google sign-in. |
| `GOOGLE_CLIENT_SECRET` | `str | None` | `None` | OAuth client secret for Google sign-in. |
| `GITHUB_CLIENT_ID` | `str | None` | `None` | OAuth client ID for GitHub sign-in. |
| `GITHUB_CLIENT_SECRET` | `str | None` | `None` | OAuth client secret for GitHub sign-in. |
| `MICROSOFT_CLIENT_ID` | `str | None` | `None` | OAuth client ID for Microsoft sign-in. |
| `MICROSOFT_CLIENT_SECRET` | `str | None` | `None` | OAuth client secret for Microsoft sign-in. |
| `LINKEDIN_CLIENT_ID` | `str | None` | `None` | OAuth client ID for LinkedIn sign-in. |
| `LINKEDIN_CLIENT_SECRET` | `str | None` | `None` | OAuth client secret for LinkedIn sign-in. |
| `TELEGRAM_BOT_TOKEN` | `str | None` | `None` | Telegram bot credential used by the Telegram sign-in integration. |
| `SSO_ALLOWED_REDIRECT_URIS` | `str` | `'http://localhost:3000,http://localhost:3000/auth/callback,https://app.example.com/auth/callback'` | Comma-separated exact redirect URI allowlist for SSO callbacks and frontend redirects. |

## Frontend & CORS

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `DOCS_ENABLED` | `bool | None` | `None` | Expose /docs, /redoc and /openapi.json; unset enables them outside production and disables them in production. |
| `FRONTEND_URL` | `str` | `'http://localhost:3000'` | Frontend base URL used by generated links and as the default CORS origin. |
| `CORS_ORIGINS` | `list[str]` | `[]` | JSON list of allowed CORS origins; an empty list uses FRONTEND_URL. |

## Observability

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SENTRY_DSN` | `str` | `""` | Sentry project DSN; empty disables Sentry reporting. |
| `SENTRY_TRACES_SAMPLE_RATE` | `float` | `0.0` | Fraction of transactions sampled for Sentry tracing when Sentry is configured. |
| `TRACING_URL` | `str` | `""` | OpenTelemetry collector endpoint; empty disables OTLP export, which also requires the tracing SDK. |
| `OTEL_SERVICE_NAME` | `str` | `'sandloom-backend'` | Service name attached to exported OpenTelemetry telemetry. |
| `LOG_LEVEL` | `str` | `'INFO'` | Minimum process log level, using standard Python logging levels. |
| `LOG_FORMAT` | `Literal['auto', 'json', 'text']` | `'auto'` | Log output format: auto selects JSON in production and human-readable text elsewhere. |

## Lifecycle

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `STARTUP_TASK_TIMEOUT_SECONDS` | `int` | `15` | Default timeout in seconds for each registered application startup task. |
| `SHUTDOWN_TASK_TIMEOUT_SECONDS` | `int` | `10` | Default timeout in seconds for each registered application shutdown task. |
| `GC_FREEZE_AFTER_STARTUP` | `bool` | `True` | After lifespan startup (gunicorn worker) / before the pool forks (Celery): gc.collect() + gc.freeze(), so full collections never traverse the startup heap again. |
| `GC_THRESHOLDS` | `tuple[int, int, int] | None` | `(2000, 10, 10)` | gc.set_threshold(t0, t1, t2) applied with the freeze, as 'a,b,c' or a JSON list; empty = interpreter default. Ignored on CPython 3.14.0-3.14.4 (incremental GC). |
| `GC_METRICS_ENABLED` | `bool` | `True` | sandloom_gc_pause_seconds / sandloom_gc_collected_objects_total / sandloom_gc_frozen_objects from gc.callbacks in the configured processes. |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | `float` | `5.0` | Timeout in seconds applied to health-check execution. |

## Other

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SHUTDOWN_BUDGET_SECONDS` | `float` | `30.0` | Overall graceful-shutdown budget in seconds for application lifecycle cleanup. |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | `float` | `300.0` | Maximum seconds migration commands wait to acquire the database migration lock. |
| `BEAT_LEASE_TTL_SECONDS` | `int` | `30` | Lifetime in seconds of the renewable singleton lease used to prevent duplicate Celery beat scheduling. |
| `BEAT_LEASE_NAME` | `str` | `""` | Lease scope; empty = derived from the broker URL, the taskqueue instance and the loaded module set (one beat per service). |
| `BEAT_LEASE_CACHE_INSTANCE` | `str` | `'default'` | Named cache instance holding the Celery beat singleton lease. |
| `HEALTH_CACHE_TTL_SECONDS` | `float` | `3.0` | Seconds public health and readiness reuse probe results; zero disables this cache. |
| `SAGA_SWEEP_INTERVAL_SECONDS` | `float` | `5.0` | Interval in seconds between lease-guarded sweeps that nudge due saga instances on each database. |
| `SAGA_SWEEP_BATCH` | `int` | `200` | Maximum due saga instances nudged per database sweep tick. |
| `SAGA_COMPENSATION_DEADLINE_SECONDS` | `int` | `3600` | Default window in seconds for compensation retries before a saga is parked; a saga definition can override it. |
| `SAGA_RETRY_BASE_SECONDS` | `float` | `1.0` | Base delay in seconds for exponential saga-step retry backoff with full jitter. |
| `SAGA_RETRY_MAX_SECONDS` | `float` | `300.0` | Maximum saga-step retry backoff in seconds. |
| `SAGA_RETENTION_DAYS` | `int` | `30` | Retention in days for completed or compensated saga instances; zero keeps them forever and parked instances are never purged. |
| `EVENT_HANDLER_TIMEOUT_SECONDS` | `float` | `30.0` | Timeout in seconds for each Redis Streams event callback. |
| `EVENT_STREAMS_RECONNECT_BASE_SECONDS` | `float` | `0.5` | Initial delay in seconds between Redis Streams reconnect attempts. |
| `EVENT_STREAMS_RECONNECT_MAX_SECONDS` | `float` | `30.0` | Maximum Redis Streams reconnect backoff delay in seconds. |
| `EVENT_STREAM_READ_COUNT` | `int` | `64` | Maximum entries read in one Streams XREADGROUP batch. |
| `OUTBOX_RELAY_STALL_SECONDS` | `float` | `60.0` | Seconds without local relay progress before relay health reports a stall; idle polling is counted as progress. |
| `OUTBOX_RETENTION_HOURS` | `float` | `168.0` | Retention of delivered outbox history in hours; zero keeps it forever. Pending and dead-lettered rows are not purged. |
| `SANDLOOM_JSONSCHEMA_BACKEND` | `Literal['auto', 'python']` | `'auto'` | Payload schema validator: auto uses jsonschema-rs when installed, otherwise Python jsonschema; python forces the Python implementation. |
| `PROJECTION_TOKEN_KEYS` | `dict[str, str]` | `{}` | JSON map of key IDs to HMAC secrets for projection consistency tokens; empty disables issuing and honoring these tokens. |
| `PROJECTION_RYW_WAIT_MS` | `int` | `300` | Maximum milliseconds a token-bearing projection read waits to catch up before returning its configured stale result behavior. |
| `RPC_TRANSPORT` | `Literal['aiohttp', 'httpx']` | `'aiohttp'` | Internal RPC HTTP client implementation. aiohttp is the default; httpx is available when its transport behavior is needed. |
| `RPC_POOL_MAX_CONNECTIONS` | `int` | `100` | Maximum aiohttp RPC connections per peer; additional callers wait within their call timeout. |
| `RPC_POOL_IDLE_SECONDS` | `float` | `4.0` | Seconds an idle aiohttp RPC connection remains in the keep-alive pool before eviction. |
| `RPC_MAX_RESPONSE_BYTES` | `int` | `33554432` | Largest accepted internal RPC response body in bytes; larger responses are rejected. |
| `RPC_MAX_REQUEST_BYTES` | `int` | `4194304` | Largest accepted internal RPC request body in bytes. |
| `SERVICE_PROBE_TIMEOUT_SECONDS` | `float` | `1.0` | Maximum seconds for each concurrent readiness probe of a required remote service binding. |
| `CONTRACT_DB_POOL_SIZE` | `int` | `5` | Persistent connections per database in the dedicated local-contract pool, separate from the request pool. |
| `CONTRACT_DB_MAX_OVERFLOW` | `int` | `5` | Additional temporary connections allowed above CONTRACT_DB_POOL_SIZE. |
| `CONTRACT_DB_LOCK_TIMEOUT_MS` | `int` | `5000` | PostgreSQL lock timeout in milliseconds on contract-pool connections; zero uses the server default. |
| `CONTRACT_READ_SCOPE_ENABLED` | `bool` | `True` | Reuse one separate local read-contract transaction and memoize identical local read calls within a request; never joins the caller transaction. |
| `CONTRACT_READ_SCOPE_IDLE_MS` | `int` | `0` | Delay in milliseconds before releasing an idle local-contract read scope; zero releases it when the request awaits unrelated work. |
| `CONTRACT_CACHE_SHADOW` | `dict[str, dict[str, Any]]` | `{}` | JSON specification of hypothetical contract-read caches and invalidations. Records measurements only; cached results are never served. |
| `CONTRACT_CACHE_SHADOW_MAX_ENTRIES` | `int` | `100000` | Maximum entries retained by contract-cache shadow measurement. |
| `CONTRACT_CACHE_SHADOW_REPORT_DIR` | `str | None` | `None` | Directory for contract-cache shadow counter snapshots at process exit; unset writes no report file. |
| `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` | `int` | `86400` | Seconds a completed command-contract result remains replayable for its idempotency key; must cover configured saga and retry horizons. |
| `IDEMPOTENCY_CACHE_INSTANCE` | `str` | `""` | Separate named cache for idempotency and task-enqueue records; empty uses SECURITY_CACHE_INSTANCE. Revocation and block state stays in the security cache. |
| `DATABASE_SYSTEM_URL` | `str | None` | `None` | Separate privileged database login used only by framework cross-tenant work on the default database; never supplied to business-module sessions. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `float` | `5.0` | Maximum seconds to establish a database connection. |
| `DB_RECONNECT_BASE_SECONDS` | `float` | `0.05` | Initial pool-wide backoff delay in seconds for new connections after transport failures; never retries statements. |
| `DB_RECONNECT_MAX_SECONDS` | `float` | `2.0` | Maximum database reconnect backoff delay in seconds. |
| `DB_POOL_OVERFLOW_LINGER_SECONDS` | `float` | `30.0` | Seconds to retain returned overflow connections after recent pool pressure; zero restores immediate overflow eviction. |
| `DB_POOL_FAIR_BARGE_MS` | `float` | `25.0` | Maximum waiter age in milliseconds before connection handoff takes priority over new arrivals; zero requests strict FIFO handoff. |
| `DB_POOL_PING_IDLE_SECONDS` | `float` | `5.0` | Ping only connections idle for this many seconds; zero pings every checkout and a negative value disables idle pings. |
| `DB_DEAD_CONNECTION_RETRY` | `bool` | `True` | Replace and retry a connection only when the first transaction message was refused before sending any bytes; never retries an ambiguous statement or commit. |
| `DB_READ_ONLY_SESSIONS` | `bool` | `False` | Enable server-enforced read-only transactions and early write refusals for read-only session dependencies; disabled uses normal session semantics. |
| `DB_RLS_MERGED_BEGIN` | `bool` | `True` | Combine transaction BEGIN and tenant binding in one asyncpg message on tenant-RLS engines; incompatible driver internals fall back to separate messages. |
| `DB_SESSION_OWNER_CHECKS` | `bool` | `False` | Extra session/connection ownership checks for accidental use from another task; db() always enforces its unit owner independently. |
| `PLANNER_OPTIMIZED_EXECUTOR` | `bool` | `True` | Allow eligible declared read operations to use the optimized database executor with the same tenant and transaction guarantees. |
| `PLANNER_LIST_MAX_LIMIT` | `int` | `500` | Framework maximum page size for declared list operations; a declaration may impose a smaller limit. |
| `DB_STATEMENT_TIMEOUT_MS` | `int` | `30000` | PostgreSQL statement timeout in milliseconds on runtime pools; zero uses the server default. Does not configure migration connections. |
| `DB_IDLE_IN_TX_TIMEOUT_MS` | `int` | `60000` | PostgreSQL idle-in-transaction timeout in milliseconds on runtime pools; zero uses the server default. |
| `CACHE_SOCKET_TIMEOUT_SECONDS` | `float` | `0.5` | Maximum seconds to wait for a Redis cache command response; timeout failures are not retried. |
| `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` | `float` | `0.5` | Maximum seconds to establish a Redis cache connection. |
| `CACHE_HEALTH_CHECK_INTERVAL_SECONDS` | `int` | `30` | Redis cache connection health-check interval in seconds; zero disables periodic connection checks. |
| `CACHE_MAX_CONNECTIONS` | `int` | `64` | Maximum connections in the default Redis cache pool; callers wait within CACHE_POOL_TIMEOUT_SECONDS when the pool is busy. |
| `CACHE_POOL_TIMEOUT_SECONDS` | `float` | `1.0` | Maximum seconds a Redis cache operation waits for a pooled connection before failing. |
| `CACHE_RETRY_ATTEMPTS` | `int` | `1` | Retries of Redis cache commands after connection errors. Command timeouts are never retried. |
| `JWT_PRIVATE_KEY` | `str | None` | `None` | Issuer-only PEM private key for asymmetric access-token signing; its public key must be published under JWT_KID. |
| `JWT_PUBLIC_KEYS` | `dict[str, str]` | `{}` | JSON map of key IDs to PEM public keys used to verify asymmetric access tokens in every service. |
| `MAX_SESSION_DAYS` | `int` | `30` | Absolute session lifetime in days; refreshing a token does not extend this deadline. |
| `TENANT_CLAIMS_MAX_ORGANIZATIONS` | `int` | `128` | Maximum organization memberships embedded in an access token; excessive claims are refused rather than silently truncated. |
| `AUTH_REVOCATION_MARK_TTL_SECONDS` | `int | None` | `None` | Lifetime of token-revocation marks in seconds. Unset uses at least 24 hours and the local access-token lifetime plus skew; explicit values must cover that access-token window. |
| `JWT_PREVIOUS_SECRETS` | `list[str]` | `[]` | JSON list of retired symmetric JWT secrets accepted for verification only during rotation; asymmetric deployments leave it empty. |
| `JWT_KID` | `str | None` | `None` | Key ID placed in issued JWT headers. Required for asymmetric signing and must identify the published signing public key. |
| `AUTH_REVOCATION_FAIL_CLOSED` | `bool | None` | `None` | Whether unavailable revocation or emergency-block checks reject access with 503. Unset fails closed in production and open elsewhere. |
| `AUTH_REVOCATION_CLOCK_SKEW_SECONDS` | `float` | `2.0` | Margin added to revocation timestamps to reject tokens minted by a clock running ahead. Keep it consistent across services; zero disables the margin. |
| `AUTH_REVOCATION_WRITE_ATTEMPTS` | `int` | `3` | Attempts to write an after-commit revocation mark before deferring it to the durable revocation consumer. |
| `AUTH_REVOCATION_RETRY_BASE_SECONDS` | `float` | `0.05` | Initial exponential-backoff delay in seconds between after-commit revocation-mark write attempts. |
| `PASSWORD_HASH_CONCURRENCY` | `int | None` | `None` | Maximum concurrent Argon2 hash and verification operations per process; unset uses at most four available CPUs. |
| `PASSWORD_HASH_PROFILE` | `Literal['strong', 'owasp_min', 'custom']` | `'strong'` | Argon2id work-factor preset: strong, owasp_min, or custom. Explicit time, memory and parallelism settings override preset values. |
| `PASSWORD_HASH_TIME_COST` | `int | None` | `None` | Argon2id iteration count overriding the selected profile; custom profiles require an explicit value. |
| `PASSWORD_HASH_MEMORY_KIB` | `int | None` | `None` | Argon2id memory cost per operation in KiB, overriding the selected profile; account for concurrent operations when sizing memory. |
| `PASSWORD_HASH_PARALLELISM` | `int | None` | `None` | Argon2id parallelism overriding the selected profile; custom profiles require an explicit value. |
| `RPC_AUTH_MAX_SKEW_SECONDS` | `int` | `60` | Maximum accepted timestamp difference in seconds for internal RPC authentication; also bounds nonce lifetime. |
| `RPC_REPLAY_GUARD` | `Literal['local', 'commands', 'all']` | `'commands'` | Shared RPC replay protection: local keeps only per-process protection, commands also guards commands in the security cache, and all guards every call there. |
| `RPC_REPLAY_CACHE_MAX_ENTRIES` | `int` | `200000` | Maximum live nonces in each process replay table; a full table fails closed. |
| `RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES` | `int` | `100000` | Maximum live nonces one caller may occupy in the process replay table. |
| `RPC_COMMANDS_REQUIRE_SIGNATURE` | `Literal['auto', 'on', 'all', 'off']` | `'auto'` | Ed25519 RPC signature policy: auto enables commands in production, on signs commands, all also signs reads, and off disables the extra signature requirement. |
| `RPC_SERVICE_ID` | `str | None` | `None` | This process service identity used for internal RPC authentication and authorization. |
| `RPC_PRIVATE_KEY` | `str | None` | `None` | This service's Ed25519 PEM private key for signing internal RPC calls; separate from its X25519 key-agreement key. |
| `RPC_KEY_ID` | `str | None` | `None` | Key ID of RPC_PRIVATE_KEY in this service published Ed25519 keys. |
| `RPC_SERVICE_KEYS` | `dict[str, dict[str, str]]` | `{}` | JSON map of calling service IDs to key-ID/Ed25519-public-key maps; retain old keys during independent service rotations. |
| `RPC_DH_PRIVATE_KEY` | `str | None` | `None` | This service's dedicated X25519 PEM private key for pair-MAC authentication and response binding. |
| `RPC_DH_KEY_ID` | `str | None` | `None` | Key ID of RPC_DH_PRIVATE_KEY in this service published X25519 keys. |
| `RPC_DH_EXTRA_PRIVATE_KEYS` | `dict[str, str]` | `{}` | JSON map of additional X25519 private-key IDs to PEM keys accepted during this service key rotation. |
| `RPC_SERVICE_DH_KEYS` | `dict[str, dict[str, str]]` | `{}` | JSON map of service IDs to key-ID/X25519-public-key maps, shared consistently across services. |
| `RPC_SERVICE_DH_CURRENT_KIDS` | `dict[str, str]` | `{}` | JSON map selecting the current published X25519 key ID addressed for each service; absent entries use the greatest published key ID. |
| `RPC_ALLOWED_CALLS` | `dict[str, Any]` | `{}` | Per-caller JSON allowlist of contract IDs and optional methods; omitted calls are denied and bindings must be covered. |
| `INTERNAL_ENDPOINTS_OPEN_IN_DEV` | `bool` | `False` | Open operational endpoints without INTERNAL_API_TOKEN outside production only; has no effect in production or on internal RPC. |
| `PLANNER_CURSOR_SECRET` | `str | None` | `None` | HMAC secret for opaque planner pagination cursors; unset falls back to INTERNAL_API_TOKEN, then JWT_SECRET. |
| `METRICS_TOKEN` | `str | None` | `None` | Optional scrape-only X-Internal-Token credential for /metrics, without access to other operational endpoints. |
| `RATE_LIMIT_GLOBAL_REQUESTS` | `int` | `0` | Global per-client-IP request allowance per configured period; zero disables this additional fail-open floor. |
| `RATE_LIMIT_GLOBAL_PERIOD_SECONDS` | `int` | `60` | Window length in seconds for the global per-client-IP request allowance. |
| `RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES` | `list[str]` | `['/health', '/ready', '/metrics', '/_sandloom/']` | Request path prefixes excluded from the additional global rate limiter. |
| `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` | `bool` | `False` | Allow catalog-backed subscriptions created without a framework operation intent to grant access; default denies those new intentless grants. |
| `BILLING_INTENT_RECONCILE_AFTER_MINUTES` | `int` | `30` | Minimum pending-intent age in minutes before periodic reconciliation looks up or expires its payment-provider operation. |
| `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` | `int` | `5` | Failed reconciliation attempts per operation intent before leaving it for operator follow-up. |
| `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` | `int` | `60` | Minutes during which a preceding portal intent attributes subscription changes to that portal visit; changes outside it are audited as unattributed. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `Literal['grpc', 'http/protobuf']` | `'grpc'` | OTLP exporter protocol used with TRACING_URL: grpc or http/protobuf. |

---

_This page is generated from the settings model. To change a description, edit the `Field(description=...)` (or the curated grouping in `tools/docs/gen_config_reference.py`)._
