"""Generate the configuration reference page from ``sandloom.core.config.settings.Settings``.

Used by ``tools/docs/docs_reference.py`` before the Zensical build.
It introspects the pydantic-settings model so the env-var reference can never
drift from the code: every ``Field(alias=...)`` becomes a row in the table.
It also writes ``reference/configuration-tiers.md`` — the settings by tier and
the semantic tier's per-profile table — from ``sandloom.core.config.tiers``.

Importing the framework instantiates the module-level ``settings`` singleton,
which validates a handful of mandatory env vars (DATABASE_URL, JWT_SECRET). We
seed harmless dummy values *before* importing so a
docs build needs no real infrastructure config. Real env vars (e.g. in CI)
always win because we only set a default when the variable is absent.
"""

from __future__ import annotations

import enum
import os
import sys
from pathlib import Path

# Seed the mandatory settings with build-only placeholders so importing the
# framework during the docs build does not require a real deployment config.
# Reference generation imports configuration without requiring running services.
_DOCS_BUILD_ENV = {
    "ENVIRONMENT": "dev",
    "DATABASE_URL": "postgresql+asyncpg://docs:docs@localhost:5432/docs",
    "JWT_SECRET": "docs-build-placeholder-secret-not-used-at-runtime",
    # No composition is booted here: name the in-process bus, so the revocation
    # applier's service-name check (a shared bus needs one) has nothing to ask.
    "EVENT_BUS_BACKEND": "memory",
}
for _key, _value in _DOCS_BUILD_ENV.items():
    os.environ.setdefault(_key, _value)

from pydantic import BaseModel  # noqa: E402
from pydantic.fields import FieldInfo  # noqa: E402
from pydantic_core import PydanticUndefined  # noqa: E402

from sandloom.core import worker  # noqa: E402
from sandloom.core.config import module_settings, tiers  # noqa: E402
from sandloom.core.config.settings import Settings  # noqa: E402
from sandloom.core.descriptors.module_names import installed_modules  # noqa: E402


def _module_packages() -> list[str]:
    """Import paths of the modules whose settings the page lists: every
    installed ``sandloom.modules`` entry point, plus the framework repository's
    own module packages (``src/sandloom_*``) that are not installed with it
    (the admin console ships separately). The page names no module."""
    src = Path(__file__).resolve().parents[2] / "src"
    packages = [module.path for module in installed_modules()]
    for directory in sorted(src.glob("sandloom_*")):
        if directory.is_dir() and directory.name not in packages:
            if str(src) not in sys.path:
                sys.path.append(str(src))  # after the installed packages
            packages.append(directory.name)
    return packages


def _module_settings_models() -> list[type[BaseModel]]:
    """The settings models those packages declare (``define_module(settings=)``)."""
    models = []
    for package in _module_packages():
        model = module_settings.declared_model(package)
        if model is not None:
            models.append(model)
    return models


_MODULE_SETTINGS = _module_settings_models()

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
    "PGHOST": "Database",
    "PGHOSTADDR": "Database",
    "PGSERVICE": "Database",
    # Eventing
    "EVENT_BUS_BACKEND": "Eventing & Outbox",
    "OUTBOX_RELAY_ENABLED": "Eventing & Outbox",
    "OUTBOX_RELAY_MAX_ATTEMPTS": "Eventing & Outbox",
    "OUTBOX_RELAY_BACKOFF_BASE_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_BACKOFF_MAX_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_CIRCUIT_BASE_SECONDS": "Eventing & Outbox",
    "OUTBOX_RELAY_CIRCUIT_MAX_SECONDS": "Eventing & Outbox",
    "SANDLOOM_SERVICE_NAME": "Eventing & Outbox",
    "SANDLOOM_COMPOSITION_ROOT": "Core & Modules",
    "EVENT_CALLBACK_TIME_BUDGET_SECONDS": "Eventing & Outbox",
    "EVENT_PAYLOAD_MAX_BYTES": "Eventing & Outbox",
    "EVENT_SHUTDOWN_DRAIN_SECONDS": "Eventing & Outbox",
    "EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT": "Eventing & Outbox",
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
    # Notifications
    "NOTIFIER_BACKEND": "Notifications",
    # Security / JWT
    "JWT_SECRET": "Security & JWT",
    "PRODUCTION_PROFILE": "Security & JWT",
    "SANDLOOM_ACCEPTED_RISKS": "Security & JWT",
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

# A setting the table above does not name is placed by the longest prefix of its
# name here. A setting no entry places stays under "Other", which the test of
# this generator (``tests/architecture/test_config_reference_page.py``) keeps empty.
_PREFIX_CATEGORIES: dict[str, str] = {
    "SANDLOOM_": "Core & Modules",
    "SHUTDOWN_": "Lifecycle",
    "BEAT_": "Lifecycle",
    "HEALTH_": "Lifecycle",
    "MIGRATION_": "Migrations",
    "SAGA_": "Sagas",
    "EVENT_": "Eventing & Outbox",
    "OUTBOX_": "Eventing & Outbox",
    "PROJECTION_": "Eventing & Outbox",
    "RPC_": "Service Registry & RPC",
    "SERVICE_": "Service Registry & RPC",
    "CONTRACT_DB_": "Database",
    "CONTRACT_": "Service Registry & RPC",
    "DATABASE_": "Database",
    "DB_": "Database",
    "PLANNER_": "Database",
    "NOTIFIER_": "Notifications",
    "JWT_": "Security & JWT",
    "AUTH_": "Security & JWT",
    "PASSWORD_HASH_": "Security & JWT",
    "INTERNAL_": "Security & JWT",
    "METRICS_TOKEN": "Security & JWT",
    "RATE_LIMIT_": "Security & JWT",
    "MAX_SESSION_DAYS": "Security & JWT",
    "TENANT_": "Multi-Tenancy",
    "OTEL_": "Observability",
    "BILLING_": "Billing",
    "ADMIN_CONSOLE_": "Admin Console",
}


def _category(alias: str) -> str:
    """The section of ``alias``: the curated table, else its longest prefix."""
    if alias in _CATEGORIES:
        return _CATEGORIES[alias]
    best = max(
        (prefix for prefix in _PREFIX_CATEGORIES if alias.startswith(prefix)),
        key=len,
        default=None,
    )
    return _PREFIX_CATEGORIES[best] if best is not None else "Other"


_CATEGORY_ORDER = [
    "Core & Modules",
    "Infrastructure",
    "Database",
    "Migrations",
    "Eventing & Outbox",
    "Sagas",
    "Service Registry & RPC",
    "Authorization",
    "Security & JWT",
    "Multi-Tenancy",
    "Billing",
    "Storage",
    "Notifications",
    "SSO",
    "Frontend & CORS",
    "Admin Console",
    "Observability",
    "Lifecycle",
    "Other",
]

#: Variables the worker process reads from its own environment, outside
#: ``Settings`` (so no model field lists them): ``(name, default, description)``.
_PROCESS_VARIABLES: tuple[tuple[str, str, str], ...] = (
    (
        worker.WORKER_CONCURRENCY_ENV,
        f"the CPU quota, at most {worker.MAX_DEFAULT_WORKER_CONCURRENCY}",
        "Worker processes of `sandloom worker` (the `--concurrency` option "
        "overrides it). A positive integer; anything else refuses to start.",
    ),
    (
        worker.TASKQUEUE_INSTANCE_ENV,
        "`default`",
        "The task queue instance of INFRA the worker runs.",
    ),
)


def _type_label(annotation: object) -> str:
    """Best-effort, human-readable type label for a model field."""
    text = str(annotation)
    text = text.replace("typing.", "").replace("sandloom.core.config.settings.", "")
    text = text.replace("pydantic.types.", "")
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        # ``<enum 'Environment'>`` says nothing: list the accepted values.
        return "`" + " | ".join(repr(member.value) for member in annotation) + "`"
    if text.startswith("Optional[") and text.endswith("]"):
        text = f"{text[len('Optional[') : -1]} | None"
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
    buckets: dict[str, list[tuple[str, FieldInfo]]] = {
        cat: [] for cat in _CATEGORY_ORDER
    }
    for name, field in Settings.model_fields.items():
        alias = field.alias or name.upper()
        buckets.setdefault(_category(alias), []).append((alias, field))
    # The settings the bundled modules declare (ADR-0015) are read from their
    # models, not from ``Settings``.
    for model in _MODULE_SETTINGS:
        for name, field in model.model_fields.items():
            alias = field.alias or name.upper()
            buckets.setdefault(_category(alias), []).append((alias, field))

    lines: list[str] = []
    lines.append("---")
    lines.append("title: Configuration Reference")
    lines.append("---")
    lines.append("")
    lines.append("# Configuration Reference")
    lines.append("")
    lines.append(
        "Every runtime setting Sandloom reads from the environment, generated directly "
        "from `sandloom.core.config.settings.Settings` and the settings the bundled "
        "modules declare, so this table can never drift from the code. "
        "Settings may be supplied as environment variables, in a `.env` file, "
        "or — when `SECRETS_DIR` is set — as files named after each field "
        "(Docker/Kubernetes secrets)."
    )
    lines.append("")
    lines.append(
        '!!! info "Required settings"\n'
        "    `DATABASE_URL` and `JWT_SECRET` are mandatory; the process aborts at "
        "startup if either is missing or invalid. An app that selects a Redis or "
        "Celery driver gives each instance its own url in `INFRA` (no flat "
        "`CACHE_URL` / `BROKER_URL` setting exists), and startup refuses an "
        "instance that has none. "
        "In production, `JWT_SECRET` must be at least 32 random characters."
    )
    lines.append("")
    lines.append(
        '!!! note "Configuration tiers"\n'
        "    Each setting is **semantic** (changes a guarantee; its value is chosen "
        "per profile) or **ops** (capacity / operations); there is no transitional "
        "tier. The per-profile table of the "
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

    lines.append("## Worker process variables")
    lines.append("")
    lines.append(
        "Read by the worker process from its own environment, not by `Settings`: "
        "`sandloom config dump` does not show them."
    )
    lines.append("")
    lines.append("| Env var | Default | Description |")
    lines.append("| --- | --- | --- |")
    for name, default, description in _PROCESS_VARIABLES:
        lines.append(f"| `{name}` | {default} | {description} |")
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
        if line.startswith("| `AUTH_REVOCATION_STRICT_PATH_PREFIXES`")
        else line
        for line in tier_page.split("\n")
    )
    return {
        "reference/configuration.md": "\n".join(lines),
        "reference/configuration-tiers.md": tier_page,
    }
