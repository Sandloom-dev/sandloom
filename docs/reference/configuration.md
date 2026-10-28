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
| `ENVIRONMENT` | `<enum 'Environment'>` | `'dev'` |  |
| `SANDLOOM_MODULES` | `list[str]` | `[]` |  |
| `MODULE_WHITELIST` | `list[str]` | `[]` |  |
| `MODULE_BLACKLIST` | `list[str]` | `[]` |  |
| `MODULE_FAIL_FAST` | `bool` | `False` |  |
| `MODULE_LOAD_ENTRYPOINTS` | `bool` | `False` |  |
| `MODULE_REQUIRED` | `list[str]` | `[]` |  |
| `SANDLOOM_COMPOSITION_ROOT` | `str | None` | `None` |  |

## Infrastructure

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `INFRA` | `dict[str, dict[str, dict[str, object]]]` | `{}` |  |
| `DATABASE_URL` | `str` | **required** |  |
| `DB_POOL_SIZE` | `int` | `10` |  |
| `DB_MAX_OVERFLOW` | `int` | `20` |  |
| `DB_POOL_TIMEOUT` | `int` | `30` |  |
| `CACHE_URL` | `str` | **required** |  |
| `BROKER_URL` | `str` | **required** |  |

## Eventing & Outbox

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `CONSUMER_STARTUP_TIMEOUT_SECONDS` | `float` | `15.0` |  |
| `CONSUMER_RETRY_BASE_SECONDS` | `float` | `1.0` |  |
| `CONSUMER_RETRY_MAX_SECONDS` | `float` | `30.0` |  |
| `EVENT_BUS_BACKEND` | `Literal['auto', 'memory', 'redis', 'streams']` | `'auto'` |  |
| `SANDLOOM_SERVICE_NAME` | `str | None` | `None` |  |
| `EVENT_STREAM_RETENTION_SECONDS` | `int` | `604800` |  |
| `EVENT_STREAM_MAXLEN` | `int` | `0` |  |
| `EVENT_STREAM_PREFIX` | `str` | `""` |  |
| `OUTBOX_RELAY_ENABLED` | `Literal['auto', 'on', 'off', 'delegated']` | `'auto'` |  |
| `OUTBOX_RELAY_MAX_ATTEMPTS` | `int` | `10` |  |
| `OUTBOX_RELAY_BACKOFF_BASE_SECONDS` | `float` | `2.0` |  |
| `OUTBOX_RELAY_BACKOFF_MAX_SECONDS` | `float` | `300.0` |  |
| `OUTBOX_RELAY_CIRCUIT_BASE_SECONDS` | `float` | `1.0` |  |
| `OUTBOX_RELAY_CIRCUIT_MAX_SECONDS` | `float` | `30.0` |  |
| `EVENT_CALLBACK_TIME_BUDGET_SECONDS` | `float` | `0.0` | If > 0, each event bus callback execution time is measured and a warning is logged when exceeded (non-fatal). |
| `EVENT_PAYLOAD_MAX_BYTES` | `int` | `0` | If > 0, serialized payload size is checked before publish; ValueError raised if exceeded. |
| `EVENT_SHUTDOWN_DRAIN_SECONDS` | `float` | `5.0` | Max seconds to wait for in-flight event callbacks to finish on bus shutdown before cancelling the stragglers. Bounds shutdown so a hung callback cannot hang the process. <= 0 waits unbounded. |
| `EVENT_BUS_REDIS_SEMAPHORE_LIMIT` | `int` | `100` | Max concurrent callbacks for RedisEventBus to prevent overload. Adjust based on CPU/network capacity. |
| `SCHEMA_REGISTRY_PATH` | `str` | `'./data/schema_registry.json'` | Path to JSON file used to persist in-memory event schema registry. If empty, persistence is disabled. File will be created atomically on updates. |

## Service Registry & RPC

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SERVICE_BINDINGS` | `dict[str, dict[str, object]]` | `{}` |  |
| `SERVICE_RPC_TIMEOUT_SECONDS` | `float` | `1.0` |  |
| `CONTRACT_DTO_PACKAGES` | `list[str]` | `[]` |  |
| `CONTRACT_COMPAT_CHECK` | `Literal['off', 'ready', 'strict']` | `'ready'` |  |
| `CONTRACT_SNAPSHOT_PATHS` | `list[str]` | `[]` |  |

## Authorization

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `ACCESS` | `dict[str, str]` | `{}` |  |

## Security & JWT

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `PRODUCTION_PROFILE` | `Literal['standard', 'multi_tenant_strict']` | `'standard'` |  |
| `SANDLOOM_ACCEPTED_RISKS` | `list[str]` | `[]` |  |
| `SECURITY_CACHE_INSTANCE` | `str` | `'default'` |  |
| `TENANCY_MODE` | `Literal['multi', 'single']` | `'multi'` |  |
| `SINGLE_TENANT_ID` | `uuid.UUID | None` | `None` |  |
| `JWT_SECRET` | `str | None` | `None` |  |
| `JWT_ALGORITHM` | `Literal['HS256', 'HS384', 'HS512', 'EdDSA', 'RS256', 'ES256']` | `'HS256'` |  |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `int` | `15` |  |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `int` | `14` |  |
| `JWT_ISSUER` | `str | None` | `None` |  |
| `JWT_AUDIENCE` | `str | None` | `None` |  |
| `JWT_LEEWAY_SECONDS` | `int` | `10` |  |
| `INTERNAL_API_TOKEN` | `str | None` | `None` |  |
| `TRUST_PROXY_HEADERS` | `bool` | `False` |  |
| `TRUSTED_PROXY_IPS` | `list[str]` | `[]` |  |
| `RATE_LIMIT_ENABLED` | `bool` | `False` |  |
| `TWOFA_ENCRYPTION_KEY` | `str | None` | `None` |  |

## Billing

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `BILLING_PROVIDER` | `Literal['fake', 'stripe']` | `'fake'` |  |
| `STRIPE_SECRET_KEY` | `str | None` | `None` |  |
| `STRIPE_WEBHOOK_SECRET` | `str | None` | `None` |  |
| `BILLING_DENY_ON_PAST_DUE` | `bool` | `False` |  |

## Storage

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `STORAGE_TYPE` | `Literal['local', 's3', 'gcs', 'yc']` | `'local'` |  |
| `STORAGE_PATH` | `str` | `'./data'` |  |
| `MAX_UPLOAD_SIZE_BYTES` | `int` | `52428800` |  |
| `MAX_REQUEST_BODY_BYTES` | `int` | `0` |  |
| `S3_BUCKET` | `str | None` | `None` |  |
| `S3_REGION` | `str | None` | `None` |  |
| `S3_ACCESS_KEY_ID` | `str | None` | `None` |  |
| `S3_SECRET_ACCESS_KEY` | `str | None` | `None` |  |

## Notifications

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `NOTIFIER_BACKEND` | `Literal['log', 'smtp']` | `'log'` |  |
| `SMTP_HOST` | `str | None` | `None` |  |
| `SMTP_PORT` | `int` | `25` |  |
| `SMTP_USERNAME` | `str | None` | `None` |  |
| `SMTP_PASSWORD` | `str | None` | `None` |  |
| `SMTP_FROM` | `str | None` | `None` |  |
| `SMTP_USE_TLS` | `bool` | `True` |  |

## SSO

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `GOOGLE_CLIENT_ID` | `str | None` | `None` |  |
| `GOOGLE_CLIENT_SECRET` | `str | None` | `None` |  |
| `GITHUB_CLIENT_ID` | `str | None` | `None` |  |
| `GITHUB_CLIENT_SECRET` | `str | None` | `None` |  |
| `MICROSOFT_CLIENT_ID` | `str | None` | `None` |  |
| `MICROSOFT_CLIENT_SECRET` | `str | None` | `None` |  |
| `LINKEDIN_CLIENT_ID` | `str | None` | `None` |  |
| `LINKEDIN_CLIENT_SECRET` | `str | None` | `None` |  |
| `TELEGRAM_BOT_TOKEN` | `str | None` | `None` |  |
| `SSO_ALLOWED_REDIRECT_URIS` | `str` | `'http://localhost:3000,http://localhost:3000/auth/callback,https://app.example.com/auth/callback'` |  |

## Frontend & CORS

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `DOCS_ENABLED` | `bool | None` | `None` |  |
| `FRONTEND_URL` | `str` | `'http://localhost:3000'` |  |
| `CORS_ORIGINS` | `list[str]` | `[]` |  |

## Observability

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SENTRY_DSN` | `str` | `""` |  |
| `SENTRY_TRACES_SAMPLE_RATE` | `float` | `0.0` |  |
| `TRACING_URL` | `str` | `""` |  |
| `OTEL_SERVICE_NAME` | `str` | `'sandloom-backend'` |  |
| `LOG_LEVEL` | `str` | `'INFO'` |  |
| `LOG_FORMAT` | `Literal['auto', 'json', 'text']` | `'auto'` |  |

## Lifecycle

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `STARTUP_TASK_TIMEOUT_SECONDS` | `int` | `15` |  |
| `SHUTDOWN_TASK_TIMEOUT_SECONDS` | `int` | `10` |  |
| `GC_FREEZE_AFTER_STARTUP` | `bool` | `True` | After lifespan startup (gunicorn worker) / before the pool forks (Celery): gc.collect() + gc.freeze(), so full collections never traverse the startup heap again. |
| `GC_THRESHOLDS` | `tuple[int, int, int] | None` | `(2000, 10, 10)` | gc.set_threshold(t0, t1, t2) applied with the freeze, as 'a,b,c' or a JSON list; empty = interpreter default. Ignored on CPython 3.14.0-3.14.4 (incremental GC). |
| `GC_METRICS_ENABLED` | `bool` | `True` | sandloom_gc_pause_seconds / sandloom_gc_collected_objects_total / sandloom_gc_frozen_objects from gc.callbacks in the configured processes. |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | `float` | `5.0` |  |

## Other

| Env var | Type | Default | Description |
| --- | --- | --- | --- |
| `SHUTDOWN_BUDGET_SECONDS` | `float` | `30.0` |  |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | `float` | `300.0` |  |
| `BEAT_LEASE_TTL_SECONDS` | `int` | `30` |  |
| `BEAT_LEASE_NAME` | `str` | `""` | Lease scope; empty = derived from the broker URL, the taskqueue instance and the loaded module set (one beat per service). |
| `BEAT_LEASE_CACHE_INSTANCE` | `str` | `'default'` |  |
| `HEALTH_CACHE_TTL_SECONDS` | `float` | `3.0` |  |
| `SAGA_SWEEP_INTERVAL_SECONDS` | `float` | `5.0` |  |
| `SAGA_SWEEP_BATCH` | `int` | `200` |  |
| `SAGA_COMPENSATION_DEADLINE_SECONDS` | `int` | `3600` |  |
| `SAGA_RETRY_BASE_SECONDS` | `float` | `1.0` |  |
| `SAGA_RETRY_MAX_SECONDS` | `float` | `300.0` |  |
| `SAGA_RETENTION_DAYS` | `int` | `30` |  |
| `EVENT_HANDLER_TIMEOUT_SECONDS` | `float` | `30.0` |  |
| `EVENT_STREAMS_RECONNECT_BASE_SECONDS` | `float` | `0.5` |  |
| `EVENT_STREAMS_RECONNECT_MAX_SECONDS` | `float` | `30.0` |  |
| `EVENT_STREAM_READ_COUNT` | `int` | `64` |  |
| `OUTBOX_RELAY_STALL_SECONDS` | `float` | `60.0` |  |
| `OUTBOX_RETENTION_HOURS` | `float` | `168.0` |  |
| `SANDLOOM_JSONSCHEMA_BACKEND` | `Literal['auto', 'python']` | `'auto'` |  |
| `PROJECTION_TOKEN_KEYS` | `dict[str, str]` | `{}` |  |
| `PROJECTION_RYW_WAIT_MS` | `int` | `300` |  |
| `RPC_TRANSPORT` | `Literal['aiohttp', 'httpx']` | `'aiohttp'` |  |
| `RPC_POOL_MAX_CONNECTIONS` | `int` | `100` |  |
| `RPC_POOL_IDLE_SECONDS` | `float` | `4.0` |  |
| `RPC_MAX_RESPONSE_BYTES` | `int` | `33554432` |  |
| `RPC_MAX_REQUEST_BYTES` | `int` | `4194304` |  |
| `SERVICE_PROBE_TIMEOUT_SECONDS` | `float` | `1.0` |  |
| `CONTRACT_DB_POOL_SIZE` | `int` | `5` |  |
| `CONTRACT_DB_MAX_OVERFLOW` | `int` | `5` |  |
| `CONTRACT_DB_LOCK_TIMEOUT_MS` | `int` | `5000` |  |
| `CONTRACT_READ_SCOPE_ENABLED` | `bool` | `True` |  |
| `CONTRACT_READ_SCOPE_IDLE_MS` | `int` | `0` |  |
| `CONTRACT_CACHE_SHADOW` | `dict[str, dict[str, Any]]` | `{}` |  |
| `CONTRACT_CACHE_SHADOW_MAX_ENTRIES` | `int` | `100000` |  |
| `CONTRACT_CACHE_SHADOW_REPORT_DIR` | `str | None` | `None` |  |
| `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` | `int` | `86400` |  |
| `IDEMPOTENCY_CACHE_INSTANCE` | `str` | `""` |  |
| `DATABASE_SYSTEM_URL` | `str | None` | `None` |  |
| `DB_CONNECT_TIMEOUT_SECONDS` | `float` | `5.0` |  |
| `DB_RECONNECT_BASE_SECONDS` | `float` | `0.05` |  |
| `DB_RECONNECT_MAX_SECONDS` | `float` | `2.0` |  |
| `DB_POOL_OVERFLOW_LINGER_SECONDS` | `float` | `30.0` |  |
| `DB_POOL_FAIR_BARGE_MS` | `float` | `25.0` |  |
| `DB_POOL_PING_IDLE_SECONDS` | `float` | `5.0` |  |
| `DB_DEAD_CONNECTION_RETRY` | `bool` | `True` |  |
| `DB_READ_ONLY_SESSIONS` | `bool` | `False` |  |
| `DB_RLS_MERGED_BEGIN` | `bool` | `True` |  |
| `DB_SESSION_OWNER_CHECKS` | `bool` | `False` |  |
| `PLANNER_OPTIMIZED_EXECUTOR` | `bool` | `True` |  |
| `PLANNER_LIST_MAX_LIMIT` | `int` | `500` |  |
| `DB_STATEMENT_TIMEOUT_MS` | `int` | `30000` |  |
| `DB_IDLE_IN_TX_TIMEOUT_MS` | `int` | `60000` |  |
| `CACHE_SOCKET_TIMEOUT_SECONDS` | `float` | `0.5` |  |
| `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS` | `float` | `0.5` |  |
| `CACHE_HEALTH_CHECK_INTERVAL_SECONDS` | `int` | `30` |  |
| `CACHE_MAX_CONNECTIONS` | `int` | `64` |  |
| `CACHE_POOL_TIMEOUT_SECONDS` | `float` | `1.0` |  |
| `CACHE_RETRY_ATTEMPTS` | `int` | `1` |  |
| `JWT_PRIVATE_KEY` | `str | None` | `None` |  |
| `JWT_PUBLIC_KEYS` | `dict[str, str]` | `{}` |  |
| `MAX_SESSION_DAYS` | `int` | `30` |  |
| `TENANT_CLAIMS_MAX_ORGANIZATIONS` | `int` | `128` |  |
| `AUTH_REVOCATION_MARK_TTL_SECONDS` | `int | None` | `None` |  |
| `JWT_PREVIOUS_SECRETS` | `list[str]` | `[]` |  |
| `JWT_KID` | `str | None` | `None` |  |
| `AUTH_REVOCATION_FAIL_CLOSED` | `bool | None` | `None` |  |
| `AUTH_REVOCATION_CLOCK_SKEW_SECONDS` | `float` | `2.0` |  |
| `AUTH_REVOCATION_WRITE_ATTEMPTS` | `int` | `3` |  |
| `AUTH_REVOCATION_RETRY_BASE_SECONDS` | `float` | `0.05` |  |
| `PASSWORD_HASH_CONCURRENCY` | `int | None` | `None` |  |
| `PASSWORD_HASH_PROFILE` | `Literal['strong', 'owasp_min', 'custom']` | `'strong'` |  |
| `PASSWORD_HASH_TIME_COST` | `int | None` | `None` |  |
| `PASSWORD_HASH_MEMORY_KIB` | `int | None` | `None` |  |
| `PASSWORD_HASH_PARALLELISM` | `int | None` | `None` |  |
| `RPC_AUTH_MAX_SKEW_SECONDS` | `int` | `60` |  |
| `RPC_REPLAY_GUARD` | `Literal['local', 'commands', 'all']` | `'commands'` |  |
| `RPC_REPLAY_CACHE_MAX_ENTRIES` | `int` | `200000` |  |
| `RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES` | `int` | `100000` |  |
| `RPC_COMMANDS_REQUIRE_SIGNATURE` | `Literal['auto', 'on', 'all', 'off']` | `'auto'` |  |
| `RPC_SERVICE_ID` | `str | None` | `None` |  |
| `RPC_PRIVATE_KEY` | `str | None` | `None` |  |
| `RPC_KEY_ID` | `str | None` | `None` |  |
| `RPC_SERVICE_KEYS` | `dict[str, dict[str, str]]` | `{}` |  |
| `RPC_DH_PRIVATE_KEY` | `str | None` | `None` |  |
| `RPC_DH_KEY_ID` | `str | None` | `None` |  |
| `RPC_DH_EXTRA_PRIVATE_KEYS` | `dict[str, str]` | `{}` |  |
| `RPC_SERVICE_DH_KEYS` | `dict[str, dict[str, str]]` | `{}` |  |
| `RPC_SERVICE_DH_CURRENT_KIDS` | `dict[str, str]` | `{}` |  |
| `RPC_ALLOWED_CALLS` | `dict[str, Any]` | `{}` |  |
| `INTERNAL_ENDPOINTS_OPEN_IN_DEV` | `bool` | `False` |  |
| `PLANNER_CURSOR_SECRET` | `str | None` | `None` |  |
| `METRICS_TOKEN` | `str | None` | `None` |  |
| `RATE_LIMIT_GLOBAL_REQUESTS` | `int` | `0` |  |
| `RATE_LIMIT_GLOBAL_PERIOD_SECONDS` | `int` | `60` |  |
| `RATE_LIMIT_GLOBAL_EXEMPT_PATH_PREFIXES` | `list[str]` | `['/health', '/ready', '/metrics', '/_sandloom/']` |  |
| `BILLING_GRANT_INTENTLESS_SUBSCRIPTIONS` | `bool` | `False` |  |
| `BILLING_INTENT_RECONCILE_AFTER_MINUTES` | `int` | `30` |  |
| `BILLING_INTENT_RECONCILE_MAX_ATTEMPTS` | `int` | `5` |  |
| `BILLING_PORTAL_ATTRIBUTION_WINDOW_MINUTES` | `int` | `60` |  |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `Literal['grpc', 'http/protobuf']` | `'grpc'` |  |

---

_This page is generated from the settings model. To change a description, edit the `Field(description=...)` (or the curated grouping in `tools/docs/gen_config_reference.py`)._
