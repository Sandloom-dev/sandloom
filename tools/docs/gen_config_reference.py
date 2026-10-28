"""Generate the configuration reference page from ``sandloom.core.config.settings.Settings``.

Used by ``tools/docs/docs_reference.py`` before the Zensical build.
It introspects the pydantic-settings model so the env-var reference can never
drift from the code: every ``Field(alias=...)`` becomes a row in the table.
It also writes ``reference/configuration-tiers.md`` — the settings by tier and
the semantic tier's per-profile table — from ``sandloom.core.config.tiers``.

Importing the framework instantiates the module-level ``settings`` singleton,
which validates a handful of mandatory env vars (DATABASE_URL, CACHE_URL,
BROKER_URL, JWT_SECRET). We seed harmless dummy values *before* importing so a
docs build needs no real infrastructure config. Real env vars (e.g. in CI)
always win because we only set a default when the variable is absent.
"""

from __future__ import annotations

import os

# Seed the mandatory settings with build-only placeholders so importing the
# framework during the docs build does not require a real deployment config.
# Reference generation imports configuration without requiring running services.
_DOCS_BUILD_ENV = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://docs:docs@localhost:5432/docs",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "docs-build-placeholder-secret-not-used-at-runtime",
}
for _key, _value in _DOCS_BUILD_ENV.items():
    os.environ.setdefault(_key, _value)

from pydantic_core import PydanticUndefined  # noqa: E402

from sandloom.core.config import tiers  # noqa: E402
from sandloom.core.config.settings import Settings  # noqa: E402

# Curated grouping: alias -> category. Aliases not listed fall into "Other".
_CATEGORIES: dict[str, str] = {
    # Core / modules
    "ENVIRONMENT": "Core & Modules",
    "SANDLOOM_MODULES": "Core & Modules",
    "MODULE_WHITELIST": "Core & Modules",
    "MODULE_BLACKLIST": "Core & Modules",
    "MODULE_FAIL_FAST": "Core & Modules",
    "MODULE_LOAD_ENTRYPOINTS": "Core & Modules",
    "MODULE_REQUIRED": "Core & Modules",
    # Lifecycle
    "STARTUP_TASK_TIMEOUT_SECONDS": "Lifecycle",
    "SHUTDOWN_TASK_TIMEOUT_SECONDS": "Lifecycle",
    "HEALTH_CHECK_TIMEOUT_SECONDS": "Lifecycle",
    "GC_FREEZE_AFTER_STARTUP": "Lifecycle",
    "GC_THRESHOLDS": "Lifecycle",
    "GC_METRICS_ENABLED": "Lifecycle",
    "CONSUMER_STARTUP_TIMEOUT_SECONDS": "Eventing & Outbox",
    "CONSUMER_RETRY_BASE_SECONDS": "Eventing & Outbox",
    "CONSUMER_RETRY_MAX_SECONDS": "Eventing & Outbox",
    # Infrastructure
    "INFRA": "Infrastructure",
    "DATABASE_URL": "Infrastructure",
    "DB_POOL_SIZE": "Infrastructure",
    "DB_MAX_OVERFLOW": "Infrastructure",
    "DB_POOL_TIMEOUT": "Infrastructure",
    "CACHE_URL": "Infrastructure",
    "BROKER_URL": "Infrastructure",
    # Eventing
    "EVENT_BUS_BACKEND": "Eventing & Outbox",
    "OUTBOX_RELAY_ENABLED": "Eventing & Outbox",
    "OUTBOX_RELAY_MAX_ATTEMPTS": "Eventing & Outbox",
    "OUTBOX_RELAY_BACKOFF_BASE_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_BACKOFF_MAX_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_CIRCUIT_BASE_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_CIRCUIT_MAX_SECONDS": "Eventing & Outbox",
    "SANDLOOM_SERVICE_NAME": "Eventing & Outbox",
    "EVENT_STREAM_RETENTION_SECONDS": "Eventing & Outbox",
    "EVENT_STREAM_MAXLEN": "Eventing & Outbox",
    "EVENT_STREAM_PREFIX": "Eventing & Outbox",
    "SANDLOOM_COMPOSITION_ROOT": "Core & Modules",
    "EVENT_CALLBACK_TIME_BUDGET_SECONDS": "Eventing & Outbox",
    "EVENT_PAYLOAD_MAX_BYTES": "Eventing & Outbox",
    "EVENT_SHUTDOWN_DRAIN_SECONDS": "Eventing & Outbox",
    "EVENT_BUS_REDIS_SEMAPHORE_LIMIT": "Eventing & Outbox",
    "SCHEMA_REGISTRY_PATH": "Eventing & Outbox",
    # Service registry / RPC
    "SERVICE_BINDINGS": "Service Registry & RPC",
    "SERVICE_RPC_TIMEOUT_SECONDS": "Service Registry & RPC",
    "CONTRACT_DTO_PACKAGES": "Service Registry & RPC",
    "CONTRACT_COMPAT_CHECK": "Service Registry & RPC",
    "CONTRACT_SNAPSHOT_PATHS": "Service Registry & RPC",
    # Authorization
    "ACCESS": "Authorization",
    # Storage
    "STORAGE_TYPE": "Storage",
    "STORAGE_PATH": "Storage",
    "MAX_UPLOAD_SIZE_BYTES": "Storage",
    "MAX_REQUEST_BODY_BYTES": "Storage",
    "S3_BUCKET": "Storage",
    "S3_REGION": "Storage",
    "S3_ACCESS_KEY_ID": "Storage",
    "S3_SECRET_ACCESS_KEY": "Storage",
    # Notifications
    "NOTIFIER_BACKEND": "Notifications",
    "SMTP_HOST": "Notifications",
    "SMTP_PORT": "Notifications",
    "SMTP_USERNAME": "Notifications",
    "SMTP_PASSWORD": "Notifications",
    "SMTP_FROM": "Notifications",
    "SMTP_USE_TLS": "Notifications",
    # Security / JWT
    "JWT_SECRET": "Security & JWT",
    "PRODUCTION_PROFILE": "Security & JWT",
    "SANDLOOM_ACCEPTED_RISKS": "Security & JWT",
    "SECURITY_CACHE_INSTANCE": "Security & JWT",
    "JWT_ALGORITHM": "Security & JWT",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "Security & JWT",
    "REFRESH_TOKEN_EXPIRE_DAYS": "Security & JWT",
    "JWT_ISSUER": "Security & JWT",
    "JWT_AUDIENCE": "Security & JWT",
    "JWT_LEEWAY_SECONDS": "Security & JWT",
    "INTERNAL_API_TOKEN": "Security & JWT",
    "TRUST_PROXY_HEADERS": "Security & JWT",
    "TRUSTED_PROXY_IPS": "Security & JWT",
    "RATE_LIMIT_ENABLED": "Security & JWT",
    "TWOFA_ENCRYPTION_KEY": "Security & JWT",
    "TENANCY_MODE": "Security & JWT",
    "SINGLE_TENANT_ID": "Security & JWT",
    # Billing
    "BILLING_PROVIDER": "Billing",
    "STRIPE_SECRET_KEY": "Billing",
    "STRIPE_WEBHOOK_SECRET": "Billing",
    "BILLING_DENY_ON_PAST_DUE": "Billing",
    # SSO
    "GOOGLE_CLIENT_ID": "SSO",
    "GOOGLE_CLIENT_SECRET": "SSO",
    "GITHUB_CLIENT_ID": "SSO",
    "GITHUB_CLIENT_SECRET": "SSO",
    "MICROSOFT_CLIENT_ID": "SSO",
    "MICROSOFT_CLIENT_SECRET": "SSO",
    "LINKEDIN_CLIENT_ID": "SSO",
    "LINKEDIN_CLIENT_SECRET": "SSO",
    "TELEGRAM_BOT_TOKEN": "SSO",
    "SSO_ALLOWED_REDIRECT_URIS": "SSO",
    # Frontend / CORS
    "FRONTEND_URL": "Frontend & CORS",
    "CORS_ORIGINS": "Frontend & CORS",
    "DOCS_ENABLED": "Frontend & CORS",
    # Observability
    "SENTRY_DSN": "Observability",
    "SENTRY_TRACES_SAMPLE_RATE": "Observability",
    "TRACING_URL": "Observability",
    "OTEL_SERVICE_NAME": "Observability",
    "LOG_LEVEL": "Observability",
    "LOG_FORMAT": "Observability",
}

_CATEGORY_ORDER = [
    "Core & Modules",
    "Infrastructure",
    "Eventing & Outbox",
    "Service Registry & RPC",
    "Authorization",
    "Security & JWT",
    "Multi-Tenancy",
    "Billing",
    "Storage",
    "Notifications",
    "SSO",
    "Frontend & CORS",
    "Observability",
    "Lifecycle",
    "Other",
]


def _type_label(annotation: object) -> str:
    """Best-effort, human-readable type label for a model field."""
    text = str(annotation)
    text = text.replace("typing.", "").replace("sandloom.core.config.settings.", "")
    # Collapse the common Optional spelling.
    text = text.replace(" | None", " | None")
    if text.startswith("<class '") and text.endswith("'>"):
        text = text[len("<class '") : -len("'>")]
    return f"`{text}`"


def _default_label(field: object) -> str:
    if getattr(field, "is_required", lambda: False)():
        return "**required**"
    default = getattr(field, "default", PydanticUndefined)
    if default is PydanticUndefined or default is None:
        factory = getattr(field, "default_factory", None)
        if factory is not None:
            try:
                produced = factory()
            except Exception:  # pragma: no cover - defensive
                return "`<computed>`"
            return f"`{produced!r}`"
        if default is None:
            return "`None`"
        return "—"
    if isinstance(default, str) and default == "":
        return '`""`'
    # Enum value
    value = getattr(default, "value", default)
    return f"`{value!r}`"


def _description(field: object) -> str:
    desc = getattr(field, "description", None)
    if desc:
        return " ".join(desc.split())
    return ""


def render_pages() -> dict[str, str]:
    # Bucket fields by category, preserving declaration order within a category.
    buckets: dict[str, list[tuple[str, object]]] = {cat: [] for cat in _CATEGORY_ORDER}
    for name, field in Settings.model_fields.items():
        alias = field.alias or name.upper()
        category = _CATEGORIES.get(alias, "Other")
        buckets.setdefault(category, []).append((alias, field))


    lines: list[str] = []
    lines.append("---")
    lines.append("title: Configuration Reference")
    lines.append("---")
    lines.append("")
    lines.append("# Configuration Reference")
    lines.append("")
    lines.append(
        "Every runtime setting Sandloom reads from the environment, generated directly "
        "from `sandloom.core.config.settings.Settings` so this table can never drift from the "
        "code. Settings may be supplied as environment variables, in a `.env` file, "
        "or — when `SECRETS_DIR` is set — as files named after each field "
        "(Docker/Kubernetes secrets)."
    )
    lines.append("")
    lines.append(
        '!!! info "Required settings"\n'
        "    `DATABASE_URL`, `CACHE_URL`, `BROKER_URL`, and `JWT_SECRET` are "
        "mandatory; the process aborts at startup if any is missing or invalid. "
        "In production, `JWT_SECRET` must be at least 32 random characters."
    )
    lines.append("")
    lines.append(
        '!!! note "Configuration tiers"\n'
        "    Each setting is **semantic** (changes a guarantee; its value is chosen "
        "per profile), **ops** (capacity / operations) or **migration** "
        "(transitional, with a removal version). The per-profile table of the "
        "semantic settings and the active-override reporting are in "
        "[Configuration Tiers](configuration-tiers.md)."
    )
    lines.append("")

    for category in _CATEGORY_ORDER:
        rows = buckets.get(category) or []
        if not rows:
            continue
        lines.append(f"## {category}")
        lines.append("")
        lines.append("| Env var | Type | Default | Description |")
        lines.append("| --- | --- | --- | --- |")
        for alias, field in rows:
            lines.append(
                f"| `{alias}` | {_type_label(field.annotation)} "
                f"| {_default_label(field)} | {_description(field)} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_This page is generated from the settings model. To change "
        "a description, edit the `Field(description=...)` (or the curated grouping "
        "in `tools/docs/gen_config_reference.py`)._"
    )
    lines.append("")

    tier_page = tiers.render_markdown()
    # Removed names are migration data, deliberately shown in this catalogue.
    tier_page = "\n".join(
        line + " <!-- stale-ok: removed setting documented for migration -->"
        if line.startswith("| `AUTH_REVOCATION_STRICT_PATH_PREFIXES`") else line
        for line in tier_page.split("\n")
    )
    return {
        "reference/configuration.md": "\n".join(lines),
        "reference/configuration-tiers.md": tier_page,
    }
