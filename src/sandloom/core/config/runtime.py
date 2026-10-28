"""Process role, module loading, lifecycle budgets, beat lease, GC and health
probes.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from pydantic_settings import NoDecode

from sandloom.core.config.common import Environment


class RuntimeConfig(BaseModel):
    """Process role, module loading, lifecycle budgets, beat lease, GC and
    health probes.
    """

    # General
    environment: Environment = Field(default=Environment.dev, alias="ENVIRONMENT")
    # Modules list for plugin loader: import paths or `sandloom.modules`
    # entry-point names. The kernel names NO business modules (ADR-0001): the
    # default is empty. A concrete application supplies its module set via the
    # SANDLOOM_MODULES env var, or — the standard composition root
    # (sandloom.main) — every installed `sandloom.modules` entry point when
    # this is left empty.
    sandloom_modules: list[str] = Field(default_factory=list, alias="SANDLOOM_MODULES")
    module_whitelist: list[str] = Field(default_factory=list, alias="MODULE_WHITELIST")
    module_blacklist: list[str] = Field(default_factory=list, alias="MODULE_BLACKLIST")
    module_fail_fast: bool = Field(default=False, alias="MODULE_FAIL_FAST")
    module_load_entrypoints: bool = Field(
        default=False, alias="MODULE_LOAD_ENTRYPOINTS"
    )
    # Modules the process cannot run without: load failure => startup abort.
    # Only enforced for modules actually present in SANDLOOM_MODULES, so a billing-only
    # microservice does not need identity co-deployed. Empty (the default) =
    # EVERY requested module is required, in every profile: a module that fails
    # to import or register aborts startup instead of serving without it
    # (SANDLOOM-B001). Optionality is explicit: define_module(..., optional=True), a
    # composition root's default_optional_modules, or MODULE_REQUIRED naming
    # the subset that must load.
    module_required: list[str] = Field(default_factory=list, alias="MODULE_REQUIRED")
    # Lifecycle
    startup_task_timeout_seconds: int = Field(
        default=15, alias="STARTUP_TASK_TIMEOUT_SECONDS"
    )
    shutdown_task_timeout_seconds: int = Field(
        default=10, alias="SHUTDOWN_TASK_TIMEOUT_SECONDS"
    )
    shutdown_budget_seconds: float = Field(
        default=30.0, gt=0, alias="SHUTDOWN_BUDGET_SECONDS"
    )
    migration_lock_timeout_seconds: float = Field(
        default=300.0, gt=0, alias="MIGRATION_LOCK_TIMEOUT_SECONDS"
    )
    # Celery beat singleton lease (sandloom.core.beat_lease; always on): a second
    # beat for the same broker + module set stands by instead of
    # double-scheduling.
    beat_lease_ttl_seconds: int = Field(
        default=30, ge=3, le=3600, alias="BEAT_LEASE_TTL_SECONDS"
    )
    beat_lease_name: str = Field(
        default="",
        alias="BEAT_LEASE_NAME",
        description=(
            "Lease scope; empty = derived from the broker URL, the taskqueue "
            "instance and the loaded module set (one beat per service)."
        ),
    )
    beat_lease_cache_instance: str = Field(
        default="default", min_length=1, alias="BEAT_LEASE_CACHE_INSTANCE"
    )
    # Cyclic GC of long-running server processes (sandloom.core.gc_tuning; applied
    # by the gunicorn worker hook and the Celery worker, never by create_app
    # itself). FREEZE: after the app's lifespan startup, gc.collect() then
    # gc.freeze() — the startup heap (modules, classes, SQLAlchemy/pydantic
    # metadata, DI graph) is never traversed again, so a full collection
    # costs what the request-era heap costs instead of the whole process.
    # THRESHOLDS: gc.set_threshold(t0, t1, t2) at the same moment ("a,b,c" or
    # a JSON list; an EMPTY value keeps the interpreter's default). The
    # default 2000,10,10 is CPython 3.13+'s own: on 3.12 (700,10,10) every
    # burst of in-flight request objects crossed threshold0 — ~98% fewer
    # young collections on the stand. Measured in
    # docs/design/gc-tuning-2026-10-23.md.
    gc_freeze_after_startup: bool = Field(
        default=True,
        alias="GC_FREEZE_AFTER_STARTUP",
        description=(
            "After lifespan startup (gunicorn worker) / before the pool forks "
            "(Celery): gc.collect() + gc.freeze(), so full collections never "
            "traverse the startup heap again."
        ),
    )
    gc_thresholds: Annotated[tuple[int, int, int] | None, NoDecode] = Field(
        default=(2000, 10, 10),
        alias="GC_THRESHOLDS",
        description=(
            "gc.set_threshold(t0, t1, t2) applied with the freeze, as 'a,b,c' "
            "or a JSON list; empty = interpreter default. Ignored on CPython "
            "3.14.0-3.14.4 (incremental GC)."
        ),
    )
    gc_metrics_enabled: bool = Field(
        default=True,
        alias="GC_METRICS_ENABLED",
        description=(
            "sandloom_gc_pause_seconds / sandloom_gc_collected_objects_total / "
            "sandloom_gc_frozen_objects from gc.callbacks in the configured processes."
        ),
    )
    health_check_timeout_seconds: float = Field(
        default=5.0, alias="HEALTH_CHECK_TIMEOUT_SECONDS"
    )
    # Public /health and /ready reuse probe results for this long (seconds) so
    # anonymous traffic cannot fan out DB/Redis/RPC probes per request. 0 = off.
    health_cache_ttl_seconds: float = Field(
        default=3.0, ge=0, alias="HEALTH_CACHE_TTL_SECONDS"
    )
    consumer_startup_timeout_seconds: float = Field(
        default=15.0, ge=0, alias="CONSUMER_STARTUP_TIMEOUT_SECONDS"
    )
    consumer_retry_base_seconds: float = Field(
        default=1.0, ge=0, alias="CONSUMER_RETRY_BASE_SECONDS"
    )
    consumer_retry_max_seconds: float = Field(
        default=30.0, ge=0, alias="CONSUMER_RETRY_MAX_SECONDS"
    )

    # Composition root (module exposing `app` / `core`) that the worker,
    # sandloom-migrate and the `sandloom` ops CLIs boot; unset = the platform
    # `sandloom.main`.
    # A generated app sets `<app>.asgi`. The process env var wins over .env.
    sandloom_composition_root: str | None = Field(
        default=None, alias="SANDLOOM_COMPOSITION_ROOT"
    )
