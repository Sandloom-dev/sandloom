"""Configuration model for infrastructure instances.

Two layers, merged at startup:

1. **Defaults** synthesized from the flat settings (``DATABASE_URL``,
   ``CACHE_URL``, ``BROKER_URL``, ``STORAGE_*``, ``EVENT_BUS_BACKEND``): with
   no extra configuration, one ``default`` instance per capability.

2. The optional ``INFRA`` setting (JSON) which declares additional named
   instances or overrides driver/options of existing ones. This is how an
   operator adds a second database, a separate analytics cache, an alternate
   queue, etc. — purely by configuration, no code change.

``INFRA`` shape::

    {
      "database": {
        "analytics": {"driver": "sqlalchemy",
                      "url": "postgresql+asyncpg://.../analytics"}
      },
      "cache":   {"sessions": {"driver": "redis", "url": "redis://.../3"}},
      "objectstore": {"exports": {"driver": "local", "path": "./exports"}}
    }

Within an instance object, ``driver`` selects the driver; every other key is
passed verbatim to that driver as an option.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sandloom.core.config.common import Environment
from sandloom.core.config.settings import Settings

#: Capabilities for which a ``default`` instance is synthesized from the flat
#: settings (DATABASE_URL, CACHE_URL, …). This is NOT the set of valid
#: capabilities — INFRA may declare instances for any capability a plugin
#: registers (workflow, graph_store, …). Whether a capability is valid is the
#: registry's job (it owns the specs); config.py only validates SHAPE.
DEFAULTED_CAPABILITIES: tuple[str, ...] = (
    "database",
    "cache",
    "objectstore",
    "taskqueue",
    "eventbus",
    "notification",
)


DEFAULT_INSTANCE = "default"


@dataclass(slots=True)
class InstanceConfig:
    """One configured infrastructure instance."""

    capability: str
    name: str
    driver: str
    options: dict[str, Any] = field(default_factory=dict)


# capability -> {instance_name -> InstanceConfig}
InfraConfig = dict[str, dict[str, InstanceConfig]]


def _synthesize_defaults(settings: Settings) -> InfraConfig:
    """Build the implicit ``default`` instance for each capability."""
    eventbus_driver = settings.event_bus_backend
    if eventbus_driver == "auto":
        # Production defaults to the DURABLE streams transport (at-least-once);
        # dev/test use the in-memory bus. Explicit EVENT_BUS_BACKEND wins.
        eventbus_driver = (
            "streams" if settings.environment == Environment.prod else "memory"
        )

    objectstore_options: dict[str, Any] = {
        "path": settings.storage_path,
        "max_upload_size_bytes": settings.max_upload_size_bytes,
    }
    if settings.storage_type == "s3":
        objectstore_options.update(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )

    return {
        "database": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="database",
                name=DEFAULT_INSTANCE,
                driver="sqlalchemy",
                options={"url": settings.database_url},
            )
        },
        "cache": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="cache",
                name=DEFAULT_INSTANCE,
                driver="redis",
                options={"url": settings.cache_url},
            )
        },
        "objectstore": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="objectstore",
                name=DEFAULT_INSTANCE,
                driver=settings.storage_type,
                options=objectstore_options,
            )
        },
        "taskqueue": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="taskqueue",
                name=DEFAULT_INSTANCE,
                driver="celery",
                options={
                    "broker": settings.broker_url,
                    "backend": settings.cache_url,
                },
            )
        },
        "eventbus": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="eventbus",
                name=DEFAULT_INSTANCE,
                driver=eventbus_driver,
                # No url: the redis/streams drivers then share the bounded
                # default transport client on CACHE_URL (a url would make them
                # build a private client). A named instance may set its own.
                options={},
            )
        },
        "notification": {
            DEFAULT_INSTANCE: InstanceConfig(
                capability="notification",
                name=DEFAULT_INSTANCE,
                driver=settings.notifier_backend,
                options={
                    "host": settings.smtp_host,
                    "port": settings.smtp_port,
                    "username": settings.smtp_username,
                    "password": settings.smtp_password,
                    "from_addr": settings.smtp_from,
                    "use_tls": settings.smtp_use_tls,
                },
            )
        },
    }


class InfraConfigError(ValueError):
    """Raised when the INFRA declaration is structurally invalid."""


def _overlay(base: InfraConfig, raw: Mapping[str, Any]) -> None:
    """Merge the raw ``INFRA`` mapping into ``base`` in place.

    Validates SHAPE only — that the structure is
    ``{capability: {instance: {"driver": ..., ...}}}`` with a driver per
    instance. It does NOT judge whether a capability name is valid: a plugin may
    register ``workflow`` / ``graph_store`` / any capability, and the registry
    (which owns the specs) is the single authority that accepts or rejects it.
    """
    for capability, instances in raw.items():
        if not isinstance(instances, Mapping):
            raise InfraConfigError(
                f"INFRA[{capability!r}] must be an object of named instances"
            )
        bucket = base.setdefault(capability, {})
        for inst_name, spec in instances.items():
            if not isinstance(spec, Mapping):
                raise InfraConfigError(
                    f"INFRA[{capability!r}][{inst_name!r}] must be an object"
                )
            existing = bucket.get(inst_name)
            options = {k: v for k, v in spec.items() if k != "driver"}
            driver = spec.get("driver") or (existing.driver if existing else None)
            if not driver:
                raise InfraConfigError(
                    f"INFRA[{capability!r}][{inst_name!r}] needs a 'driver'"
                )
            if existing is not None:
                # Override: keep prior options not re-specified, layer new ones.
                merged = dict(existing.options)
                merged.update(options)
                options = merged
            bucket[inst_name] = InstanceConfig(
                capability=capability,
                name=inst_name,
                driver=str(driver),
                options=options,
            )


def load_infra_config(settings: Settings) -> InfraConfig:
    """Return the fully merged infrastructure configuration."""
    config = _synthesize_defaults(settings)
    raw = getattr(settings, "infra", None)
    if raw:
        if not isinstance(raw, Mapping):
            raise InfraConfigError("INFRA must be a JSON object")
        _overlay(config, raw)
    return config


__all__ = [
    "DEFAULT_INSTANCE",
    "InstanceConfig",
    "InfraConfig",
    "InfraConfigError",
    "load_infra_config",
]
