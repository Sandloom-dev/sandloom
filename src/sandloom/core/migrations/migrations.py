"""Phase I: Module-aware migrations helper.

This is an initial abstraction that prepares for per-module migration execution.
Current capabilities:

* Enumerate loaded module descriptors and collect valid migration directories
  (a directory containing an `versions` sub-folder or any `*.py` files).
* Provide a `MigrationPlan` dataclass summarizing which modules have migrations.
* Provide a `run_all()` helper that (for now) simply logs intended actions and
  optionally could invoke Alembic for the core/global migrations.

Future (Phase I later steps / Phase N):
* Execute each module's migrations in dependency order
* Support a unified version table namespace per module (e.g. `alembic_version_<mod>`)
* Expose CLI entrypoint (e.g. `python -m sandloom.core.migrations.migrations upgrade
--all`)

Rationale: Keep this lightweight so core can evolve without forcing every module
to adopt a heavy scaffolding immediately.
"""

from __future__ import annotations

import heapq
import logging
import os
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    URL,
    Connection,
    Engine,
    Table,
    create_engine,
    inspect,
    make_url,
    pool,
    text,
)
from sqlalchemy.exc import DBAPIError

from sandloom.core.config.settings import settings
from sandloom.core.config.settings import settings as _settings
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.infra.config import load_infra_config
from sandloom.core.migrations import alembic_support
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.orm import Base
from sandloom.core.sql_alchemy.models import (
    ConsumerInboxEntry,
    ContractCommandRecord,
    HttpIdempotencyRecord,
    OutboxMessage,
)
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry

logger = logging.getLogger("sandloom.core.migrations.migrations")


@dataclass(slots=True)
class MigrationEntry:
    module: str
    path: str
    valid: bool
    reason: str | None = None


@dataclass(slots=True)
class MigrationPlan:
    entries: list[MigrationEntry]

    def modules(self) -> list[str]:  # convenience
        return [e.module for e in self.entries if e.valid]


def _is_migration_dir(path: str) -> bool:
    if not os.path.isdir(path):  # fast-fail
        return False
    # Heuristic: directory contains a "versions" subdir OR at least one .py file
    versions = os.path.join(path, "versions")
    if os.path.isdir(versions):
        return True
    for name in os.listdir(path):
        if name.endswith(".py") and not name.startswith("__init__"):
            return True
    return False


def build_plan(descriptors: Iterable[ModuleDescriptor]) -> MigrationPlan:
    entries: list[MigrationEntry] = []
    for d in descriptors:
        if not d.migrations_path:
            entries.append(
                MigrationEntry(module=d.name, path="", valid=False, reason="no_path")
            )
            continue
        path = d.migrations_path
        if _is_migration_dir(path):
            entries.append(MigrationEntry(module=d.name, path=path, valid=True))
        else:
            entries.append(
                MigrationEntry(
                    module=d.name, path=path or "", valid=False, reason="invalid"
                )
            )
    return MigrationPlan(entries=entries)


def run_all(plan: MigrationPlan, dry_run: bool = True) -> None:
    """Execute migrations for all valid entries.

    Currently dry-run by default: logs actions. Real execution will integrate
    Alembic per-module in a later increment once directory conventions stabilize.
    When ``dry_run`` is False only core/global Alembic upgrade is attempted; per
    module execution remains a placeholder.
    """
    for entry in plan.entries:
        if not entry.valid:
            logger.debug(
                "[migrations] skip module=%s reason=%s", entry.module, entry.reason
            )
            continue
        if dry_run:
            logger.info(
                "[migrations] (dry-run) would apply migrations for module=%s path=%s",
                entry.module,
                entry.path,
            )
        else:
            # Per-module execution lives in upgrade_all_modules (descriptor
            # aware). Failing loudly beats logging "(NOT IMPLEMENTED)" and
            # letting CI believe migrations ran.
            raise NotImplementedError(
                "run_all(dry_run=False) is not an executor; "
                "use upgrade_all_modules(descriptors, dry_run=False)"
            )


# -----------------------------
# Multi-database resolution (infra layer)
# -----------------------------

#: Env var read by alembic/env.py to target a named database instance.
_MIGRATE_URL_ENV = "SANDLOOM_MIGRATE_DATABASE_URL"


def list_database_instances() -> dict[str, str]:
    """Return ``{logical_name: sync_url}`` for every configured database.

    Built from the infrastructure configuration so additional databases
    declared via ``INFRA`` are migrate-able by name, not just the default.
    """
    config = load_infra_config(_settings)
    result: dict[str, str] = {}
    for name, cfg in config.get("database", {}).items():
        raw = cfg.options.get("url")
        if isinstance(raw, str) and raw:
            result[name] = to_sync_url(raw)
    return result


def resolve_database_sync_url(name: str = "default") -> str:
    """Return the sync DSN for a named database instance (raises if unknown)."""
    instances = list_database_instances()
    if name not in instances:
        available = ", ".join(sorted(instances)) or "<none>"
        raise MigrationDependencyError(
            f"Unknown database instance {name!r} (configured: {available})"
        )
    return instances[name]


def ensure_core_infra_tables(database_url: str, dry_run: bool = True) -> None:
    """Create the shared core-infra tables in a target database (repair tool).

    The core chain owns FIVE shared infra tables — ``outbox_messages``,
    ``audit_log``, ``consumer_inbox``, ``contract_command_log`` and
    ``http_idempotency_log`` — and every
    database that hosts a module (or merely publishes events) needs all of
    them: each owns its own
    transactional outbox, its audit trail and its consumer-inbox dedupe rows.
    Creating only the outbox would leave a database that publishes events but
    drops every audit record and every exactly-once consumer claim.

    This is an idempotent (``checkfirst=True``) DEV / REPAIR escape hatch built
    from the canonical ORM models. ``checkfirst`` only checks *existence* — it
    does NOT detect or fix schema drift in an already-present, outdated table.
    The sanctioned production path is the versioned, drift-aware alembic chain
    (``sandloom-migrate upgrade-core``); use this only to repair a database that
    chain cannot reach.
    """
    if dry_run:
        logger.info(
            "[migrations] (dry-run) would ensure core infra tables "
            "(outbox_messages, audit_log, consumer_inbox, contract_command_log, "
            "http_idempotency_log) on %s",
            _mask_url(database_url),
        )
        return

    # The ORM models are the canonical schema and declare the same columns AND
    # indexes as the alembic chain, so tables created here match migration-built
    # ones. checkfirst only checks existence — parity is from the shared model.
    engine = create_engine(database_url)
    try:
        for model in (
            OutboxMessage,
            AuditLogEntry,
            ConsumerInboxEntry,
            ContractCommandRecord,
            HttpIdempotencyRecord,
        ):
            table = cast(Table, model.__table__)
            table.create(bind=engine, checkfirst=True)
        # Use the shared metadata create_all too so any index/constraint the
        # loop missed is still emitted (idempotent under checkfirst).
        Base.metadata.create_all(
            bind=engine,
            tables=[
                cast(Table, OutboxMessage.__table__),
                cast(Table, AuditLogEntry.__table__),
                cast(Table, ConsumerInboxEntry.__table__),
                cast(Table, ContractCommandRecord.__table__),
            ],
            checkfirst=True,
        )
        logger.info(
            "[migrations] ensured core infra tables on %s", _mask_url(database_url)
        )
    finally:
        engine.dispose()


def _mask_url(url: str) -> str:
    """Redact credentials in a DSN for logging."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


#: Advisory-lock key serialising every ``sandloom-migrate`` run against one database
#: ("SANDLOOMM"). Session-level, so it spans the core chain AND every module chain.
MIGRATION_LOCK_KEY = 0x414A414D


#: ``application_name`` of the connections holding the migration lock, so a
#: waiting runner's diagnostics (and ``pg_stat_activity``) name them.
MIGRATION_LOCK_APPLICATION_NAME = "sandloom-migrate"
#: How often a waiting runner logs who holds the lock (seconds).
MIGRATION_LOCK_NOTICE_SECONDS = 5.0

#: ``pg_advisory_lock(bigint)`` splits the key into classid (high 32 bits) and
#: objid (low 32 bits) with objsubid = 1; covers the xact-level lock too.
_LOCK_HOLDERS_SQL = (
    "SELECT a.pid, a.application_name, a.state, a.backend_start, a.query_start, "
    "a.xact_start, a.usename, a.client_addr "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
    "AND l.classid = {classid} AND l.objid = {objid} "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


def migration_lock_holders(conn: Any) -> list[dict[str, Any]]:
    """Sessions holding the migration advisory lock on ``conn``'s database.

    Read from ``pg_locks`` joined with ``pg_stat_activity`` (no query text: it
    may carry data). Empty when the lock is free or the role may not see the
    holder (``pg_stat_activity`` hides other roles' rows without
    ``pg_read_all_stats``). Never raises: diagnostics must not fail the wait.
    """
    key = MIGRATION_LOCK_KEY
    sql = _LOCK_HOLDERS_SQL.format(
        classid=(key >> 32) & 0xFFFFFFFF, objid=key & 0xFFFFFFFF
    )
    try:
        rows = conn.exec_driver_sql(sql).mappings().all()
        conn.commit()
    except Exception:  # noqa: BLE001 - best-effort diagnostics
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001, S110 - diagnostics must not fail the wait
            pass
        return []
    return [
        {
            k: (str(v) if v is not None and k == "client_addr" else v)
            for k, v in row.items()
        }
        for row in rows
    ]


def describe_lock_holders(holders: Sequence[dict[str, Any]]) -> str:
    """One-line human description of :func:`migration_lock_holders` rows."""
    if not holders:
        return "holder not visible (grant pg_read_all_stats to see it)"
    parts = []
    for h in holders:
        since = h.get("xact_start") or h.get("query_start") or h.get("backend_start")
        parts.append(
            f"pid {h.get('pid')} (application_name={h.get('application_name')!r}, "
            f"user={h.get('usename')!r}, client={h.get('client_addr') or 'local'}, "
            f"state={h.get('state')!r}, query_start={h.get('query_start')}, "
            f"since={since})"
        )
    return "; ".join(parts)


@contextmanager
def migration_lock(
    database_urls: Iterable[str], *, timeout: float | None = None
) -> Iterator[None]:
    """Hold a PostgreSQL advisory lock on every target database for the block.

    Two concurrent ``sandloom-migrate`` processes (two replicas' migrate jobs, a
    retried CI step) would otherwise race DDL and the ``alembic_version_*``
    rows. Each database gets its own dedicated connection holding
    ``pg_advisory_lock(MIGRATION_LOCK_KEY)`` for the WHOLE run; a second runner
    waits (``pg_try_advisory_lock`` polling) until the first finishes, then
    sees an up-to-date schema and no-ops. The wait is bounded by ``timeout``
    (``sandloom-migrate --lock-timeout``) or ``MIGRATION_LOCK_TIMEOUT_SECONDS``;
    every :data:`MIGRATION_LOCK_NOTICE_SECONDS` the waiting runner logs WHO
    holds the lock (pid, application_name, query start — see
    :func:`migration_lock_holders`) and the timeout error names them too.
    Databases are locked in sorted order so two runners never deadlock.
    Non-PostgreSQL URLs are skipped. The per-chain runner
    (:func:`sandloom.core.migrations.alembic_support.run_migrations`) also takes a
    transaction-scoped lock when invoked outside this block (plain
    ``alembic upgrade``) and skips it for URLs held here.
    """
    wait_budget = (
        settings.migration_lock_timeout_seconds if timeout is None else float(timeout)
    )
    if not wait_budget > 0:
        raise ValueError("migration lock timeout must be > 0 seconds")
    urls = sorted({u for u in database_urls if u.startswith("postgresql")})
    held: list[tuple[str, Engine, Connection]] = []
    try:
        for url in urls:
            engine = create_engine(
                url,
                poolclass=pool.NullPool,
                connect_args={"application_name": MIGRATION_LOCK_APPLICATION_NAME},
            )
            conn = engine.connect()
            held.append((url, engine, conn))
            logger.info("[migrations] acquiring migration lock on %s", _mask_url(url))
            started = time.monotonic()
            deadline = started + wait_budget
            next_notice = started
            while not conn.exec_driver_sql(
                f"SELECT pg_try_advisory_lock({MIGRATION_LOCK_KEY})"
            ).scalar_one():
                conn.commit()
                now = time.monotonic()
                if now >= deadline:
                    holders = describe_lock_holders(migration_lock_holders(conn))
                    raise MigrationDependencyError(
                        f"Timed out after {wait_budget:g}s waiting for the "
                        f"migration advisory lock on {_mask_url(url)}, held by "
                        f"{holders}; let that runner finish (or terminate it), "
                        "or raise --lock-timeout / MIGRATION_LOCK_TIMEOUT_SECONDS"
                    )
                if now >= next_notice:
                    logger.warning(
                        "[migrations] waiting for migration lock on %s held by %s "
                        "(%.0fs of %gs)",
                        _mask_url(url),
                        describe_lock_holders(migration_lock_holders(conn)),
                        now - started,
                        wait_budget,
                    )
                    next_notice = now + MIGRATION_LOCK_NOTICE_SECONDS
                time.sleep(min(0.1, deadline - now))
            conn.commit()  # session-level lock survives the commit
            alembic_support.LOCKED_URLS.add(url)
        yield
    finally:
        for url, engine, conn in reversed(held):
            alembic_support.LOCKED_URLS.discard(url)
            try:
                conn.exec_driver_sql(f"SELECT pg_advisory_unlock({MIGRATION_LOCK_KEY})")
                conn.commit()
            except Exception:  # noqa: BLE001 - closing the session frees it anyway
                logger.warning("[migrations] could not release lock on %s", url)
            finally:
                conn.close()
                engine.dispose()


#: Extensions a service database gets (as
#: ``deploy/postgres/init-databases.sh`` creates them): ``citext`` backs
#: case-insensitive emails, ``pg_trgm`` the admin user search index.
NEW_DATABASE_EXTENSIONS = ("citext", "pg_trgm")

#: Advisory-lock class ("SANDLOOMP") serialising :func:`ensure_database` per
#: target name, on the maintenance database; the second key is
#: ``hashtext(<name>)``.
PROVISION_LOCK_CLASS = 0x414A4150


def ensure_database(
    database_url: str, *, maintenance_database: str = "postgres"
) -> bool:
    """Create the PostgreSQL database ``database_url`` names when it is missing.

    ``sandloom migrate upgrade-all --create-databases`` (the microservices
    profile's ``migrate-<service>`` one-shots) runs it before migrating: a
    service added to a running stack has a database name, but the Postgres
    image's init scripts ran only once, on the then-empty volume. The role in
    ``database_url`` connects to ``maintenance_database`` on the same server
    and needs ``CREATEDB``. Non-PostgreSQL URLs: no-op (``False``). Returns
    whether it created the database.

    Race-safe (C-DEPLOY-09): the whole provisioning (exists check, ``CREATE
    DATABASE``, :data:`NEW_DATABASE_EXTENSIONS`) runs under an advisory lock
    on ``maintenance_database`` keyed by the target name, and the extensions
    are ensured (``IF NOT EXISTS``) whether or not this caller created it. A
    concurrent caller therefore waits for the creator and never returns
    (and starts migrating) while the database still lacks an extension; a
    creator that died in between leaves the extensions to the next caller.
    """
    url = make_url(to_sync_url(database_url))
    name = url.database
    if not url.drivername.startswith("postgresql") or not name:
        return False
    admin = create_engine(
        url.set(database=maintenance_database),
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT",
        connect_args={"application_name": MIGRATION_LOCK_APPLICATION_NAME},
    )
    key = {"cls": PROVISION_LOCK_CLASS, "name": name}
    try:
        with admin.connect() as conn:
            # session level: CREATE DATABASE cannot run in a transaction
            conn.execute(text("SELECT pg_advisory_lock(:cls, hashtext(:name))"), key)
            try:
                created = _create_database(conn, name)
                if created:
                    logger.info("[migrations] created database %s", _mask_url(str(url)))
                _ensure_extensions(url)
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:cls, hashtext(:name))"), key
                )
    finally:
        admin.dispose()
    return created


def _create_database(conn: Connection, name: str) -> bool:
    exists = conn.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
    ).scalar()
    if exists:
        return False
    quoted = conn.dialect.identifier_preparer.quote_identifier(name)
    try:
        conn.exec_driver_sql(f"CREATE DATABASE {quoted}")
    except DBAPIError as exc:
        # duplicate_database: a creator that does not take the lock (an
        # init script, an operator)
        if getattr(exc.orig, "pgcode", None) == "42P04":
            return False
        raise
    return True


def _ensure_extensions(url: URL) -> None:
    target = create_engine(url, poolclass=pool.NullPool, isolation_level="AUTOCOMMIT")
    try:
        with target.connect() as conn:
            for extension in NEW_DATABASE_EXTENSIONS:
                conn.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    finally:
        target.dispose()


def run_core_upgrade(
    head: str = "head", dry_run: bool = True, database_url: str | None = None
) -> None:
    """Run Alembic upgrade for the core (shared) migrations chain.

    Parameters
    ----------
    head: Target revision (default ``head``)
    dry_run: If True just logs intended action
    database_url: Sync DSN of the target database. ``None`` = the default
        database. Supplying a named instance's URL applies the chain there.
    """
    target = database_url or to_sync_url(_default_database_url())
    if dry_run:
        logger.info(
            "[migrations] (dry-run) alembic upgrade %s on %s",
            head,
            _mask_url(target),
        )
        return
    try:  # pragma: no cover - side-effect heavy
        prev = os.environ.get(_MIGRATE_URL_ENV)
        os.environ[_MIGRATE_URL_ENV] = target
        try:
            cfg = Config()
            cfg.set_main_option("script_location", str(core_chain_dir()))
            command.upgrade(cfg, head)
        finally:
            if prev is None:
                os.environ.pop(_MIGRATE_URL_ENV, None)
            else:
                os.environ[_MIGRATE_URL_ENV] = prev
        logger.info(
            "[migrations] core upgrade %s complete on %s", head, _mask_url(target)
        )
    except Exception:  # pragma: no cover
        # A failed migration must fail the deploy — never swallow it.
        logger.exception("[migrations] core upgrade failed")
        raise


#: Where the core (shared-infra) chain ships inside the installed package. The
#: wheel build force-includes migrations/core here, so an application that
#: depends on the framework wheel (``sandloom new``) migrates the SAME core chain.
_PACKAGED_CORE_CHAIN = "core_chain"


def core_chain_dir() -> Path:
    """Locate the core Alembic chain (outbox / audit / inbox / command log).

    Order: the copy packaged inside ``sandloom.core.migrations`` (wheel / image
    install) → the framework source tree (``migrations/core`` for an editable
    install). Never an application's own project-level chain or the CWD: the
    core chain is the framework's, so its revisions stay identical everywhere.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / _PACKAGED_CORE_CHAIN,
        here.parents[3]
        / "migrations"
        / "core",  # source checkout: repository migrations
    ]
    for candidate in candidates:
        if (candidate / "env.py").is_file() and (candidate / "versions").is_dir():
            return candidate
    raise RuntimeError(
        "core migration chain not found (looked in: "
        + ", ".join(str(c) for c in candidates)
        + "); reinstall the sandloom package"
    )


def _default_database_url() -> str:
    return _settings.database_url


# -----------------------------
# Per-module Alembic execution
# -----------------------------


def _sanitize_module_name(name: str) -> str:
    # use last segment only (e.g. sandloom_identity -> identity)
    return _module_name(name).lower().replace("-", "_")


def version_table_for(descriptor: ModuleDescriptor) -> str:
    """Per-chain alembic version table (``alembic_version_<chain key>``)."""
    return f"alembic_version_{descriptor.migration_chain_key()}"


def check_chain_collisions(descriptors: Iterable[ModuleDescriptor]) -> None:
    """Fail if two modules with migrations would share one chain key.

    The key (last dotted segment unless ``migrations_chain`` is set) names the
    version table and, by scaffold convention, the ``<key>_`` table prefix — so
    ``sandloom_billing`` and ``myapp.modules.billing`` would silently share
    ``alembic_version_billing`` and each other's tables.
    """
    owners: dict[str, str] = {}
    for d in descriptors:
        if not d.migrations_path:
            continue
        key = d.migration_chain_key()
        other = owners.get(key)
        if other is not None and other != d.name:
            raise MigrationDependencyError(
                f"modules {other!r} and {d.name!r} both map to migration chain "
                f"{key!r} (version table alembic_version_{key}, table prefix "
                f"'{key}_'). Rename one module package or set a distinct "
                "ModuleDescriptor.migrations_chain (and table prefix) on it."
            )
        owners[key] = d.name


def _chain_heads(script_location: str) -> tuple[str, ...]:
    return tuple(sorted(ScriptDirectory(script_location).get_heads()))


def _chain_revisions(script_location: str) -> set[str]:
    return {rev.revision for rev in ScriptDirectory(script_location).walk_revisions()}


def migration_status(  # noqa: C901
    descriptors: Iterable[ModuleDescriptor],
    hosting_databases: Iterable[str],
    *,
    connect_timeout: int = 5,
) -> list[dict[str, Any]]:
    """Applied vs code heads of every chain on its database (READ-ONLY).

    One row per (chain, database): the core chain on every hosting database,
    each module chain on the database hosting it. ``status`` is ``ok`` (at the
    code's heads), ``behind`` (an older revision of this chain), ``unknown``
    (a revision this code does not know — the database was migrated by a
    NEWER release), ``missing`` (no version table: never migrated) or
    ``error`` (unreachable; ``error`` holds the message, credentials masked).
    Connections are read-only (``default_transaction_read_only``) with a
    connect and statement timeout; nothing is written.
    """
    chains: list[tuple[str, str, str, str]] = []  # (chain, location, table, db)
    core_location = str(core_chain_dir())
    for db_name in sorted(set(hosting_databases)):
        chains.append((("core"), core_location, "alembic_version", db_name))  # noqa: PERF401 - cold path (startup / diagnostics)
    for d in descriptors:
        if not d.migrations_path or not _is_migration_dir(d.migrations_path):
            continue
        for db_name in sorted(d.database_instances()):
            chains.append((d.name, d.migrations_path, version_table_for(d), db_name))  # noqa: PERF401 - cold path (startup / diagnostics)

    urls = list_database_instances()
    rows: list[dict[str, Any]] = []
    engines: dict[str, Any] = {}
    try:
        for chain, location, table, db_name in chains:
            row: dict[str, Any] = {
                "chain": chain,
                "database": db_name,
                "version_table": table,
            }
            rows.append(row)
            try:
                heads = _chain_heads(location)
                row["code_heads"] = list(heads)
                url = urls.get(db_name)
                if url is None:
                    raise MigrationDependencyError(
                        f"database instance {db_name!r} is not configured"
                    )
                engine = engines.get(url)
                if engine is None:
                    connect_args: dict[str, Any] = {}
                    if url.startswith("postgresql"):
                        connect_args = {
                            "connect_timeout": connect_timeout,
                            "application_name": "sandloom-doctor",
                            "options": "-c default_transaction_read_only=on "
                            f"-c statement_timeout={connect_timeout * 1000}",
                        }
                    engine = create_engine(
                        url, poolclass=pool.NullPool, connect_args=connect_args
                    )
                    engines[url] = engine
                with engine.connect() as conn:
                    if not inspect(conn).has_table(table):
                        row["applied"] = []
                        row["status"] = "missing"
                        continue
                    applied = sorted(
                        str(v)
                        for v in conn.exec_driver_sql(
                            f'SELECT version_num FROM "{table}"'  # noqa: S608 - configured version-table name
                        ).scalars()
                    )
                row["applied"] = applied
                if tuple(applied) == heads:
                    row["status"] = "ok"
                elif set(applied) - _chain_revisions(location):
                    row["status"] = "unknown"
                else:
                    row["status"] = "behind"
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                row["status"] = "error"
                row["error"] = _mask_dsns(str(exc) or type(exc).__name__)
    finally:
        for engine in engines.values():
            engine.dispose()
    return rows


def _mask_dsns(text: str) -> str:
    """Mask ``scheme://user:password@`` credentials anywhere in ``text``."""
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@", r"\1***@", text)


def _has_env_script(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "env.py"))


def upgrade_module(
    descriptor: ModuleDescriptor,
    revision: str = "head",
    dry_run: bool = True,
) -> None:
    """Upgrade a single module's migrations (expects Alembic layout in migrations_path).

    Requirements for execution (non-dry-run):
    * migrations_path is set and valid
    * env.py exists inside migrations_path
    * versions/ directory present (typical Alembic structure)
    """
    if not descriptor.migrations_path:
        logger.debug(
            "[migrations] module=%s no migrations_path (skip)", descriptor.name
        )
        return
    path = descriptor.migrations_path
    if not _is_migration_dir(path):  # already validated earlier but double-check
        logger.warning(
            "[migrations] module=%s path=%s invalid structure", descriptor.name, path
        )
        return
    targets = sorted(descriptor.database_instances())
    if dry_run:
        logger.info(
            "[migrations] (dry-run) module=%s upgrade %s using path=%s on databases=%s",
            descriptor.name,
            revision,
            path,
            ",".join(targets),
        )
        return
    if not _has_env_script(path):  # pragma: no cover - defensive
        raise MigrationDependencyError(
            f"module={descriptor.name} path={path} missing env.py (cannot run)"
        )
    try:  # pragma: no cover - side effects heavy
        # Apply the module's chain to the ONE database that hosts its tables
        # (``ModuleDescriptor.database_instances()``: explicit
        # ``migrations_database``, else its single required database, else
        # ``default``). An unknown instance raises (resolve_database_sync_url).
        version_table = version_table_for(descriptor)
        for db_name in targets:
            sync_url = resolve_database_sync_url(db_name)
            cfg = Config()
            cfg.set_main_option("script_location", path)
            cfg.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
            cfg.set_main_option("version_table", version_table)
            command.upgrade(cfg, revision)
            logger.info(
                "[migrations] module=%s upgraded to %s on database=%s",
                descriptor.name,
                revision,
                db_name,
            )
    except Exception:  # pragma: no cover
        # A failed migration must fail the deploy — never swallow it.
        logger.exception("[migrations] module=%s upgrade failed", descriptor.name)
        raise


def revision_module(
    descriptor: ModuleDescriptor,
    message: str,
    *,
    autogenerate: bool = False,
    database: str | None = None,
    rev_id: str | None = None,
    url: str | None = None,
) -> list[str]:
    """Create a new revision in ``descriptor``'s OWN migration chain.

    Runs ``alembic revision`` with exactly the configuration ``upgrade_module``
    uses — ``script_location`` = the module's ``migrations/`` directory,
    ``version_table`` = ``alembic_version_<chain key>``, target database = the
    one hosting the module's tables (``database_instances()``; pass
    ``database`` when a module declares several). With ``autogenerate`` the
    chain's ``env.py`` compares ONLY the module's tables against that database
    (which must be at the chain's head); ``url`` compares against that
    database instead (``sandloom migrate new``: a scratch database at head).
    Returns the new revision file paths.
    """
    if not descriptor.migrations_path or not _is_migration_dir(
        descriptor.migrations_path
    ):
        raise MigrationDependencyError(
            f"module={descriptor.name} has no migrations directory (expected "
            "<module>/migrations/ with env.py and versions/; `sandloom add module` "
            "generates one)"
        )
    path = descriptor.migrations_path
    if not _has_env_script(path):
        raise MigrationDependencyError(
            f"module={descriptor.name} path={path} missing env.py (cannot run)"
        )
    targets = sorted(descriptor.database_instances())
    if database is not None:
        if database not in targets:
            raise MigrationDependencyError(
                f"module={descriptor.name} is not hosted in database {database!r} "
                f"(hosts: {targets})"
            )
        db_name = database
    elif len(targets) == 1:
        db_name = targets[0]
    else:
        raise MigrationDependencyError(
            f"module={descriptor.name} is hosted in several databases {targets}; "
            "pass --database to choose the one autogenerate compares against"
        )

    cfg = Config()
    cfg.set_main_option("script_location", path)
    target = url if url is not None else resolve_database_sync_url(db_name)
    cfg.set_main_option("sqlalchemy.url", target.replace("%", "%%"))
    cfg.set_main_option("version_table", version_table_for(descriptor))
    result = command.revision(
        cfg, message=message, autogenerate=autogenerate, rev_id=rev_id
    )
    scripts = result if isinstance(result, list) else [result]
    paths = [str(s.path) for s in scripts if s is not None]
    logger.info(
        "[migrations] module=%s new revision(s) %s (database=%s, autogenerate=%s)",
        descriptor.name,
        ", ".join(paths),
        db_name,
        autogenerate,
    )
    return paths


def upgrade_all_modules(
    descriptors: Sequence[ModuleDescriptor],
    revision: str = "head",
    dry_run: bool = True,
) -> None:
    """Upgrade all modules that have valid migration dirs."""
    check_chain_collisions(descriptors)
    plan = build_plan(descriptors)
    # Map name -> descriptor for quick lookup
    descriptor_map: dict[str, ModuleDescriptor] = {d.name: d for d in descriptors}
    # Filter only valid migration entries
    valid_names = {e.module for e in plan.entries if e.valid}

    ordered = _order_by_dependencies(descriptor_map, valid_names)
    for name in ordered:
        d = descriptor_map.get(name)
        if d is None:
            continue
        upgrade_module(d, revision=revision, dry_run=dry_run)


# -----------------------------
# Dependency ordering & conflict detection (Phase I)
# -----------------------------


class MigrationDependencyError(RuntimeError):
    pass


def _order_by_dependencies(
    descriptor_map: dict[str, ModuleDescriptor], candidates: set[str]
) -> list[str]:
    """Return a DETERMINISTIC topological order of the candidate modules.

    The plan must not depend on set iteration order — two runs over the same
    descriptors always produce the same sequence (lexicographic tie-break).

    * A missing dependency of a ``required`` module is fatal (raises) — a
      required module must not migrate before a dependency that never ran.
      A missing dependency of an optional module is logged and the edge dropped.
    * A dependency cycle is fatal (raises) rather than silently skipped — a
      cyclic migration graph is a deploy error, not a best-effort case.
    """
    ordered_candidates = sorted(candidates)
    adjacency: dict[str, set[str]] = {name: set() for name in ordered_candidates}
    indegree: dict[str, int] = dict.fromkeys(ordered_candidates, 0)

    for name in ordered_candidates:
        d = descriptor_map.get(name)
        if not d:
            continue
        for dep in sorted(getattr(d, "depends_on", []) or []):
            if dep not in candidates:
                if getattr(d, "required", False):
                    raise MigrationDependencyError(
                        f"required module {name!r} depends on {dep!r}, which has "
                        "no migrations to run (not loaded or absent)"
                    )
                logger.warning(
                    "[migrations] module=%s optional dependency missing/ignored=%s",
                    name,
                    dep,
                )
                continue
            adjacency[dep].add(name)
            indegree[name] += 1

    # Kahn's algorithm with a min-heap: lexicographically smallest ready node
    # first, so the order is fully deterministic.
    heap = [n for n, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    ordered: list[str] = []
    while heap:
        current = heapq.heappop(heap)
        ordered.append(current)
        for nxt in sorted(adjacency[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, nxt)

    if len(ordered) != len(candidates):
        cyclic = sorted(n for n, deg in indegree.items() if deg > 0)
        raise MigrationDependencyError(
            f"cyclic migration dependency detected among: {cyclic}"
        )
    return ordered
