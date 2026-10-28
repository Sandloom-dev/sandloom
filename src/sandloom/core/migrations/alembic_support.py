"""Shared Alembic ``env.py`` runner for scoped, per-chain migrations.

The framework no longer uses a single linear migration chain. Instead:

* the **core** chain owns the shared infrastructure tables (``outbox_messages``,
  ``audit_log``, ``consumer_inbox``, ``contract_command_log``,
  ``http_idempotency_log``) and is applied to
  EVERY database that hosts a module (every database needs its own
  transactional outbox);
* every **business module** owns its OWN chain (``src/sandloom/<module>/migrations``)
  covering only that module's tables, applied to the database instance(s) the
  module declares via ``CapabilityRequirement("database", instance=...)``.

All models share one ``Base.metadata``, so each chain targets a SUBSET of it.
A chain's ``env.py`` is a thin wrapper that imports its own models (so they
register on the shared metadata) and calls :func:`run_migrations` with the set
of table names it owns. ``include_object`` then scopes autogenerate to exactly
those tables, so a chain never emits DDL for a table that belongs elsewhere.

Target resolution (in priority order):

#. ``SANDLOOM_MIGRATE_DATABASE_URL`` — a sync DSN injected by ``sandloom-migrate`` when
it
   targets a named database instance.
#. the alembic ``Config``'s ``sqlalchemy.url`` (set programmatically by
   ``upgrade_module`` / autogenerate, or by ``alembic.ini`` for the core chain).
#. the default ``DATABASE_URL`` from settings (converted to the sync driver).

The per-chain version table is read from the Config main option
``version_table`` (``upgrade_module`` sets it to ``alembic_version_<module>``),
so independent chains coexisting in one database (the monolith) never collide on
a shared ``alembic_version`` table.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterable, Sequence
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from sandloom.core.config.settings import settings
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.infra.config import load_infra_config
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.orm import Base

#: Sync DSN injected by ``sandloom-migrate`` to target a named database instance.
MIGRATE_URL_ENV = "SANDLOOM_MIGRATE_DATABASE_URL"

#: Advisory-lock key shared with ``sandloom.core.migrations.migrations``.
MIGRATION_LOCK_KEY = 0x414A414D

#: URLs whose migration lock this process already holds (set by
#: ``migrations.migration_lock``) — the chain runner must not re-take it on a
#: second connection (that would self-deadlock).
LOCKED_URLS: set[str] = set()

#: Placeholder URL alembic writes into a fileless ``Config``; never a real target.
_PLACEHOLDER_URLS = frozenset(
    {
        "driver://user:pass@localhost/dbname",
        "postgresql://career:assistant@db:5432/career_assistant",
        "postgresql+psycopg2://career:assistant@db:5432/career_assistant",
    }
)


def _resolve_sync_url(configured: str | None) -> str:
    injected = os.getenv(MIGRATE_URL_ENV)
    if injected:
        return to_sync_url(injected)
    if configured and configured not in _PLACEHOLDER_URLS:
        return to_sync_url(configured)

    return to_sync_url(settings.database_url)


def run_migrations(  # noqa: C901
    table_names: Iterable[str] = (),
    model_modules: Sequence[str] = (),
    *,
    table_prefix: str | None = None,
) -> None:
    """Run the active alembic chain scoped to the chain's own tables.

    Parameters
    ----------
    table_names:
        Explicit set of tables this chain owns (used by the framework's
        hand-written chains, whose tables do not share a single prefix).
    model_modules:
        Importable module paths whose import registers the chain's models on the
        shared ``Base.metadata`` (e.g. ``"sandloom_identity.models"``).
    table_prefix:
        Alternative scoping for scaffolded modules, which follow the
        ``<module>_`` table-name convention: every table starting with this
        prefix is owned by the chain. Combined with ``table_names`` (union).

    Autogenerate is restricted to the owned tables (and the indexes/constraints
    attached to them) via ``include_object``; at upgrade time the filter is a
    no-op (the migration ops run as written).
    """
    for mod in model_modules:
        importlib.import_module(mod)

    owned = frozenset(table_names)
    config = context.config
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    target_metadata = Base.metadata
    version_table = config.get_main_option("version_table") or "alembic_version"

    def _owns(name: str | None) -> bool:
        if name is None:
            return False
        if name in owned:
            return True
        return table_prefix is not None and name.startswith(table_prefix)

    def include_object(
        obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        if type_ == "table":
            return _owns(name)
        parent = getattr(obj, "table", None)
        if parent is not None:
            return _owns(parent.name)
        return True

    url = _resolve_sync_url(config.get_main_option("sqlalchemy.url"))
    # Keep generated table policies consistent with the runtime tenant hook.
    # Match the effective URL, including named databases and CLI URL overrides.
    config.attributes["sandloom_tenant_rls"] = any(
        instance.options.get("tenant_rls") is True
        and to_sync_url(str(instance.options.get("url") or settings.database_url))
        == url
        for instance in load_infra_config(settings).get("database", {}).values()
    )

    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            include_object=include_object,
            version_table=version_table,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    section = dict(config.get_section(config.config_ini_section, {}) or {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
                version_table=version_table,
            )
            with context.begin_transaction():
                if url not in LOCKED_URLS and connection.dialect.name == "postgresql":
                    # Direct ``alembic upgrade`` (outside sandloom-migrate's
                    # run-wide lock): serialise concurrent runners for the
                    # duration of this chain's transaction.
                    connection.exec_driver_sql(
                        f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})"
                    )
                context.run_migrations()
    finally:
        connectable.dispose()


def module_package_of(env_file: str | os.PathLike[str]) -> str:
    """Dotted package of the module whose ``migrations/env.py`` is ``env_file``.

    ``.../myapp/modules/links/migrations/env.py`` -> ``myapp.modules.links``:
    walks up from the module directory while each directory is a regular
    package (has ``__init__.py``).
    """
    env = Path(env_file).resolve()
    module_dir = env.parent.parent
    if not (module_dir / "__init__.py").is_file():
        raise RuntimeError(
            f"cannot derive the module package from {env}: {module_dir} is not "
            "a Python package (no __init__.py); pass package=... explicitly"
        )
    parts: list[str] = []
    current = module_dir
    while (current / "__init__.py").is_file():
        parts.append(current.name)
        current = current.parent
    return ".".join(reversed(parts))


def run_module_migrations(
    env_file: str | os.PathLike[str],
    *,
    package: str | None = None,
    model_modules: Sequence[str] | None = None,
    table_prefix: str | None = None,
    table_names: Iterable[str] = (),
) -> None:
    """Run a business module's own chain from its ``migrations/env.py``.

    Everything is derived from the env.py location by convention — the module
    package (``<pkg>``, from ``__file__``), its models (``<pkg>.models``) and
    the table prefix (``<module>_``) — so a module's env.py is two lines::

        from sandloom.core.migrations import run_module_migrations

        run_module_migrations(__file__)

    Any keyword overrides the convention (``package=``, ``model_modules=`` for
    models split across modules, ``table_prefix=``, ``table_names=`` for
    tables outside the prefix).
    """
    pkg = package or module_package_of(env_file)
    module_name = _module_name(pkg)
    run_migrations(
        table_names,
        list(model_modules) if model_modules is not None else [f"{pkg}.models"],
        table_prefix=table_prefix if table_prefix is not None else f"{module_name}_",
    )
