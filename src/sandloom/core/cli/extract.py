"""Move ONE module's existing data out of a shared database (offline mode).

``sandloom migrate extract-module <module> --from <db> --to <db>`` is the data half
of "monolith -> microservices by configuration": the code/topology half is a
config change (``SANDLOOM_MODULES`` + ``SERVICE_BINDINGS``), but a running SaaS also
has rows in the monolith database that the new service must own. This module
moves them in a **maintenance window** (the module's writers are stopped);
online / zero-downtime extraction is NOT covered (see deploy/README.md).

What is moved, and the ownership rules:

* **Module tables** -- every table mapped by a model under the module's package
  (``<pkg>.models``) plus every table carrying the chain's ``<key>_`` prefix.
  Copied in foreign-key order with ``COPY`` (text format, explicit column
  lists), one target transaction per table, then verified by row count + an
  order-independent content checksum computed by PostgreSQL on both sides.
* **Sequences** owned by those tables (``serial`` / identity columns) are set
  on the target to the source's ``last_value`` / ``is_called``.
* **Outbox** (``outbox_messages``) -- rows of the module's channels
  (``<short>.`` prefix, the ``<module>.<event>.vN`` convention) that are NOT
  delivered yet (pending and dead-lettered) are *handed off*: inserted into the
  target with the same id (stable envelope id), then marked delivered in the
  source with an ``extracted-to`` marker so the source relay never re-sends
  them. Delivered rows are history and stay in the source (retention purges).
* **Consumer inbox** (``consumer_inbox``) -- exactly-once claims of the
  module's consumers (``<module>:<name>`` groups, explicit ``group=`` ids of its
  consumers, and ``<short>@...`` durable-inbox names) are copied, so a
  redelivery after the move is still deduplicated. Removed from the source only
  by ``--drop-source``.
* **Audit** (``audit_log``) -- rows whose action is in the module's namespace
  (``<short>.``) are copied; the source keeps its copy (audit is append-only
  and is never deleted by this tool).
* **Command idempotency log** (``contract_command_log``) -- the durable dedupe
  records of the module's command contracts (``contract`` = ``<contract>.<method>``
  for every contract the module registered, plus the ``<short>.`` naming
  convention) are copied, so a caller retrying a command that completed before
  the move gets the stored result from the new service instead of running it
  twice. Removed from the source only by ``--drop-source``.
* **HTTP idempotency log** (``http_idempotency_log``) -- the durable
  ``idempotency_guard(durable=True)`` records of the module's routes (guard
  ``scope`` = the ``<short>.`` naming convention) are copied, so a client
  retrying a request that committed before the move gets its stored response
  from the new service. Removed from the source only by ``--drop-source``.
* **Saga instances** (``saga_instances`` / ``saga_steps``) -- the instances
  of the module's sagas (``saga_type`` = ``<short>.<name>``) and their step
  rows are copied, so the new service continues every in-flight saga from
  where it stopped (its command steps replay through the copied command log).
  Removed from the source only by ``--drop-source``.

Safety model:

* dry-run by default (:meth:`ModuleExtractor.plan` performs no writes);
* a persistent **write fence** on the source: statement-level triggers make
  every module table read-only and refuse new outbox rows on the module's
  channels (SQLSTATE 25006). Installing it takes the table locks under
  ``lock_timeout``, so live writers make the fence -- and the run -- fail
  instead of racing the copy. The fence stays after the run (writes to the
  stale source copy would be lost); ``--unfence`` removes it to roll back;
* **resumable**: every copied table is committed together with a manifest row
  (``sandloom_module_extractions`` in the target: rows + checksum). A re-run skips
  tables whose manifest matches the (fenced, unchanged) source; a non-empty
  target table without a manifest is a refusal, never an overwrite;
* ``--drop-source`` is a separate, explicit step that requires the completed
  manifest and re-checks that the source still matches it.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg2.extras import (  # type: ignore[import-untyped]
    execute_values,
)
from sqlalchemy import create_engine, pool

import sandloom.core.migrations.migrations as _migrations
from sandloom.core.consumers.runtime import normalize_database_url
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.migrations.alembic_support import MIGRATE_URL_ENV
from sandloom.core.migrations.migrations import _mask_url
from sandloom.core.migrations.urls import to_sync_url
from sandloom.core.orm import Base

logger = logging.getLogger("sandloom.core.migrations.extract")

#: Trigger function + trigger name of the source write fence.
FENCE_FUNCTION = "sandloom_extract_fence"
FENCE_TRIGGER = "sandloom_extract_fence"
#: Manifest table in the TARGET database (resume + drop-source evidence).
MANIFEST_TABLE = "sandloom_module_extractions"
#: Manifest row marking a fully verified extraction.
COMPLETE_MARKER = "*complete*"
#: Outbox ``last_error`` marker of a row handed off to another database.
HANDOFF_MARKER = "sandloom migrate extract-module: handed off to "
#: COPY payloads above this size spill from memory to a temporary file.
_SPOOL_BYTES = 64 * 1024 * 1024

_SESSION_SETUP = (
    "SET TIME ZONE 'UTC'",
    "SET extra_float_digits = 3",
    "SET DateStyle = 'ISO, YMD'",
    "SET IntervalStyle = 'postgres'",
    "SET bytea_output = 'hex'",
)


class ExtractionError(RuntimeError):
    """The extraction cannot proceed (refused or failed verification)."""


# ---------------------------------------------------------------------------
# what belongs to the module
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModuleDataSpec:
    """The module's data footprint in a shared database."""

    module: str
    short: str
    chain_key: str
    migrations_path: str
    tables: tuple[str, ...]
    channel_prefixes: tuple[str, ...]
    inbox_consumers: tuple[str, ...] = ()
    inbox_prefixes: tuple[str, ...] = ()
    audit_prefixes: tuple[str, ...] = ()
    #: ``contract_command_log.contract`` prefixes of the module's commands.
    command_prefixes: tuple[str, ...] = ()
    #: ``http_idempotency_log.scope`` prefixes of the module's durable guards.
    http_scope_prefixes: tuple[str, ...] = ()
    #: ``saga_instances.saga_type`` prefixes of the module's sagas.
    saga_prefixes: tuple[str, ...] = ()

    @property
    def version_table(self) -> str:
        return f"alembic_version_{self.chain_key}"


def module_tables(descriptor: ModuleDescriptor) -> tuple[str, ...]:
    """Tables owned by ``descriptor``: its mapped models + its ``<key>_`` prefix."""
    pkg = descriptor.name
    try:
        importlib.import_module(f"{pkg}.models")
    except ModuleNotFoundError as exc:
        if exc.name not in (f"{pkg}.models", pkg):
            raise
    owned: set[str] = set()
    for mapper in Base.registry.mappers:
        cls_module = getattr(mapper.class_, "__module__", "") or ""
        if cls_module == pkg or cls_module.startswith(f"{pkg}."):
            for table in mapper.tables:
                owned.add(str(table.name))
    prefix = f"{descriptor.migration_chain_key()}_"
    owned.update(name for name in Base.metadata.tables if name.startswith(prefix))
    return tuple(sorted(owned))


def module_data_spec(
    descriptor: ModuleDescriptor,
    consumer_groups: Iterable[str] = (),
    contracts: Iterable[str] = (),
) -> ModuleDataSpec:
    """Build the ownership rules of ``descriptor`` (see the module docstring).

    ``consumer_groups`` are the durable group ids of the module's registered
    event / projection consumers (their exactly-once inbox keys);
    ``contracts`` the service contracts it registered (their command records).
    """
    if not descriptor.migrations_path:
        raise ExtractionError(
            f"module {descriptor.name!r} has no migrations chain: nothing to extract"
        )
    short = _module_name(descriptor.name)
    tables = module_tables(descriptor)
    if not tables:
        raise ExtractionError(
            f"module {descriptor.name!r} maps no tables (no models under "
            f"{descriptor.name}.models and no '{descriptor.migration_chain_key()}_' "
            "tables)"
        )
    return ModuleDataSpec(
        module=descriptor.name,
        short=short,
        chain_key=descriptor.migration_chain_key(),
        migrations_path=descriptor.migrations_path,
        tables=tables,
        channel_prefixes=(f"{short}.",),
        inbox_consumers=tuple(sorted(set(consumer_groups))),
        inbox_prefixes=(f"{descriptor.name}:", f"{short}@"),
        audit_prefixes=(f"{short}.",),
        command_prefixes=tuple(
            sorted({f"{short}.", *(f"{name}." for name in contracts)})
        ),
        http_scope_prefixes=(f"{short}.",),
        saga_prefixes=(f"{short}.",),
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TableState:
    name: str
    source_rows: int
    source_checksum: str
    target_rows: int | None = None
    target_checksum: str | None = None
    manifest_checksum: str | None = None
    status: str = "pending"  # pending | done | conflict | copied


@dataclass(slots=True)
class ExtractionReport:
    module: str
    source: str
    target: str
    mode: str
    applied: bool = False
    fenced: bool = False
    tables: list[TableState] = field(default_factory=list)
    sequences: list[tuple[str, int | None, bool]] = field(default_factory=list)
    outbox_pending: int = 0
    outbox_dead: int = 0
    outbox_handed_off: int = 0
    inbox_rows: int = 0
    audit_rows: int = 0
    command_rows: int = 0
    http_rows: int = 0
    saga_rows: int = 0
    complete: bool = False
    problems: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = "APPLIED" if self.applied else "DRY-RUN (pass --apply to execute)"
        lines = [
            f"extract-module {self.module} [{self.mode}] {head}",
            f"  source: {self.source}",
            f"  target: {self.target}",
            f"  source fenced (read-only): {'yes' if self.fenced else 'no'}",
            f"  target manifest complete: {'yes' if self.complete else 'no'}",
            "  tables (FK order):",
        ]
        for t in self.tables:
            tgt = "-" if t.target_rows is None else str(t.target_rows)
            lines.append(
                f"    {t.name:<34} source={t.source_rows:<8} target={tgt:<8} {t.status}"
            )
        for name, value, called in self.sequences:
            lines.append(f"  sequence {name}: last_value={value} is_called={called}")
        lines.append(
            f"  outbox: pending={self.outbox_pending} dead={self.outbox_dead} "
            f"(module channels, to hand off); already handed off="
            f"{self.outbox_handed_off}"
        )
        lines.append(f"  consumer_inbox claims to copy: {self.inbox_rows}")
        lines.append(f"  audit_log rows to copy: {self.audit_rows}")
        lines.append(f"  contract_command_log records to copy: {self.command_rows}")
        lines.append(f"  saga instances to copy: {self.saga_rows}")
        lines.append(f"  http_idempotency_log records to copy: {self.http_rows}")
        lines.extend(f"  action: {a}" for a in self.actions)
        lines.extend(f"  PROBLEM: {p}" for p in self.problems)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def mask_url(url: str) -> str:
    return _mask_url(url)


def database_identity_of_url(url: str) -> str:
    """The group-ownership identity a service configured with ``url`` claims."""
    digest = hashlib.sha256(normalize_database_url(url).encode()).hexdigest()
    return f"url:{digest[:16]}"


class _Conn:
    """A thin psycopg2 connection wrapper (explicit transactions, UTC session)."""

    def __init__(self, url: str) -> None:
        self.url = to_sync_url(url)
        self._engine = create_engine(self.url, poolclass=pool.NullPool)
        self.raw: Any = self._engine.raw_connection()
        cur = self.raw.cursor()
        for stmt in _SESSION_SETUP:
            cur.execute(stmt)
        self.raw.commit()

    def cursor(self) -> Any:
        return self.raw.cursor()

    def one(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        cur = self.raw.cursor()
        cur.execute(sql, params)
        return cur.fetchone()

    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[Any]:
        cur = self.raw.cursor()
        cur.execute(sql, params)
        return list(cur.fetchall())

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            self._engine.dispose()


@contextmanager
def _connect(url: str) -> Iterator[_Conn]:
    conn = _Conn(url)
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: _Conn, table: str) -> bool:
    return conn.one("SELECT to_regclass(%s) IS NOT NULL", (_q(table),))[0] is True


def _columns(conn: _Conn, table: str) -> list[str]:
    rows = conn.all(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped AND attgenerated = '' "
        "ORDER BY attnum",
        (_q(table),),
    )
    return [str(r[0]) for r in rows]


def _checksum(conn: _Conn, table: str, columns: Sequence[str]) -> tuple[int, str]:
    """Row count + an order-independent content checksum (two 60-bit sums)."""
    row = "md5(ROW(" + ", ".join(_q(c) for c in columns) + ")::text)"
    sql = (
        "SELECT count(*), "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
        f"coalesce(sum(('x' || substr({row}, 1, 15))::bit(60)::bigint), 0)::text"
        " || ':' || "
        f"coalesce(sum(('x' || substr({row}, 16, 15))::bit(60)::bigint), 0)::text "
        f"FROM {_q(table)}"
    )
    count, digest = conn.one(sql)
    return int(count), f"{int(count)}:{digest}"


def _chain_version(conn: _Conn, version_table: str) -> str | None:
    if not _table_exists(conn, version_table):
        return None
    row = conn.one(f"SELECT version_num FROM {_q(version_table)} LIMIT 1")  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
    return None if row is None else str(row[0])


def _fk_order(conn: _Conn, tables: Sequence[str]) -> tuple[list[str], list[str]]:
    """Tables in FK-safe (parents first) order + FKs leaving the module."""
    owned = set(tables)
    rows = conn.all(
        "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text "
        "FROM pg_constraint c WHERE c.contype = 'f'"
    )
    deps: dict[str, set[str]] = {t: set() for t in tables}
    outside: list[str] = []
    for child, parent in rows:
        child, parent = str(child).strip('"'), str(parent).strip('"')
        if child not in owned or child == parent:
            continue
        if parent not in owned:
            outside.append(f"{child} -> {parent}")
            continue
        deps[child].add(parent)
    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(t for t, d in remaining.items() if not (d - set(ordered)))
        if not ready:  # a cycle: keep a deterministic order, COPY still works
            ready = sorted(remaining)[:1]
        for t in ready:
            ordered.append(t)
            remaining.pop(t)
    return ordered, outside


def _owned_sequences(conn: _Conn, tables: Sequence[str]) -> list[str]:
    rows = conn.all(
        "SELECT DISTINCT s.relname FROM pg_class s "
        "JOIN pg_depend d ON d.objid = s.oid "
        "AND d.classid = 'pg_class'::regclass "
        "AND d.refclassid = 'pg_class'::regclass "
        "JOIN pg_class t ON t.oid = d.refobjid "
        "WHERE s.relkind = 'S' AND d.deptype IN ('a', 'i') "
        "AND t.relname = ANY(%s) ORDER BY 1",
        (list(tables),),
    )
    return [str(r[0]) for r in rows]


def _required_extensions(conn: _Conn, tables: Sequence[str]) -> list[str]:
    """Extensions whose types the module's columns use (e.g. ``citext``)."""
    rows = conn.all(
        "SELECT DISTINCT e.extname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "JOIN pg_depend d ON d.objid = t.oid AND d.deptype = 'e' "
        "JOIN pg_extension e ON e.oid = d.refobjid "
        "WHERE c.relname = ANY(%s) AND a.attnum > 0 ORDER BY 1",
        (list(tables),),
    )
    return [str(r[0]) for r in rows]


def _rls_problem(conn: _Conn, tables: Sequence[str], side: str) -> str | None:
    """RLS would silently filter COPY / checksums for a non-bypassing role."""
    rows = conn.all(
        "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity",
        (list(tables),),
    )
    if not rows:
        return None
    bypass = conn.one(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if bypass and bypass[0]:
        return None
    names = ", ".join(sorted(str(r[0]) for r in rows))
    return (
        f"{side}: row-level security is enabled on {names} and the connecting "
        "role is neither superuser nor BYPASSRLS — the copy would see only one "
        "tenant's rows. Run the extraction as a superuser / BYPASSRLS role."
    )


def _target_rls_problem(
    source: _Conn, target: _Conn, tables: Sequence[str]
) -> str | None:
    """Check the target role before its migrations create FORCE-RLS tables."""
    protected = source.all(
        "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity",
        (list(tables),),
    )
    existing = _rls_problem(target, tables, "target")
    if existing or not protected:
        return existing
    bypass = target.one(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if not bypass or not bypass[0]:
        return (
            "target: source module has RLS-protected tables but the target role "
            "is neither superuser nor BYPASSRLS; migrations would create FORCE "
            "RLS tables that this role cannot copy across tenants"
        )
    return None


# ---------------------------------------------------------------------------
# the extractor
# ---------------------------------------------------------------------------


class ModuleExtractor:
    """Plan / apply / verify / drop-source / unfence one module's extraction.

    ``on_table_copied`` is a test seam called after each table commits (a
    raising hook simulates a crash mid-run, to prove resume).
    """

    def __init__(
        self,
        spec: ModuleDataSpec,
        source_url: str,
        target_url: str,
        *,
        lock_timeout_ms: int = 5000,
        on_table_copied: Callable[[str], None] | None = None,
    ) -> None:
        self.spec = spec
        self.source_url = to_sync_url(source_url)
        self.target_url = to_sync_url(target_url)
        if normalize_database_url(self.source_url) == normalize_database_url(
            self.target_url
        ):
            raise ExtractionError("--from and --to name the same database")
        for url in (self.source_url, self.target_url):
            if not url.startswith("postgresql"):
                raise ExtractionError(
                    f"only PostgreSQL is supported (got {mask_url(url)})"
                )
        self.lock_timeout_ms = max(1, int(lock_timeout_ms))
        self.on_table_copied = on_table_copied

    # -- helpers -----------------------------------------------------------
    def _report(self, mode: str) -> ExtractionReport:
        return ExtractionReport(
            module=self.spec.module,
            source=mask_url(self.source_url),
            target=mask_url(self.target_url),
            mode=mode,
        )

    def _head(self) -> str | None:
        cfg = Config()
        cfg.set_main_option("script_location", self.spec.migrations_path)
        return ScriptDirectory.from_config(cfg).get_current_head()

    def _outbox_filter(self) -> tuple[str, list[str]]:
        clauses = " OR ".join("channel LIKE %s" for _ in self.spec.channel_prefixes)
        return f"({clauses})", [_like_prefix(p) for p in self.spec.channel_prefixes]

    def _inbox_filter(self) -> tuple[str, list[Any]]:
        parts = ["consumer = ANY(%s)"]
        params: list[Any] = [list(self.spec.inbox_consumers)]
        for prefix in self.spec.inbox_prefixes:
            parts.append("consumer LIKE %s")
            params.append(_like_prefix(prefix))
        return "(" + " OR ".join(parts) + ")", params

    def _audit_filter(self) -> tuple[str, list[str]]:
        clauses = " OR ".join("action LIKE %s" for _ in self.spec.audit_prefixes)
        return f"({clauses})", [_like_prefix(p) for p in self.spec.audit_prefixes]

    def _command_filter(self) -> tuple[str, list[Any]]:
        prefixes = self.spec.command_prefixes or (f"{self.spec.short}.",)
        clauses = " OR ".join("contract LIKE %s" for _ in prefixes)
        return f"({clauses})", [_like_prefix(p) for p in prefixes]

    def _http_filter(self) -> tuple[str, list[Any]]:
        prefixes = self.spec.http_scope_prefixes or (f"{self.spec.short}.",)
        clauses = " OR ".join("scope LIKE %s" for _ in prefixes)
        return f"({clauses})", [_like_prefix(p) for p in prefixes]

    def _saga_filter(self) -> tuple[str, list[Any]]:
        prefixes = self.spec.saga_prefixes or (f"{self.spec.short}.",)
        clauses = " OR ".join("saga_type LIKE %s" for _ in prefixes)
        return f"({clauses})", [_like_prefix(p) for p in prefixes]

    #: Saga state of the module (steps first: they reference instances).
    def _saga_tables(self) -> tuple[tuple[str, tuple[str, list[Any]]], ...]:
        where = self._saga_filter()
        return (("saga_steps", where), ("saga_instances", where))

    #: Dedupe-record tables keyed by the module's names: table -> filter.
    def _dedupe_tables(self) -> tuple[tuple[str, tuple[str, list[Any]]], ...]:
        return (
            ("contract_command_log", self._command_filter()),
            ("http_idempotency_log", self._http_filter()),
        )

    def _fenced_tables(self, conn: _Conn) -> set[str]:
        rows = conn.all(
            "SELECT tgrelid::regclass::text FROM pg_trigger WHERE tgname = %s",
            (FENCE_TRIGGER,),
        )
        return {str(r[0]).strip('"') for r in rows}

    def _outbox_fence_name(self) -> str:
        return f"{FENCE_TRIGGER}_{self.spec.chain_key}"

    def _is_fenced(self, conn: _Conn) -> bool:
        return set(self.spec.tables) <= self._fenced_tables(conn)

    def _manifest(self, conn: _Conn) -> dict[str, tuple[int, str]]:
        if not _table_exists(conn, MANIFEST_TABLE):
            return {}
        rows = conn.all(
            f"SELECT table_name, row_count, checksum FROM {MANIFEST_TABLE} "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            "WHERE module = %s",
            (self.spec.module,),
        )
        return {str(r[0]): (int(r[1]), str(r[2])) for r in rows}

    def _ensure_manifest(self, conn: _Conn) -> None:
        conn.cursor().execute(
            f"CREATE TABLE IF NOT EXISTS {MANIFEST_TABLE} ("
            "module varchar(200) NOT NULL, table_name varchar(200) NOT NULL, "
            "row_count bigint NOT NULL, checksum text NOT NULL, "
            "source_identity text NOT NULL, "
            "copied_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'utc'), "
            "PRIMARY KEY (module, table_name))"
        )
        conn.commit()

    def _counts(self, src: _Conn, report: ExtractionReport) -> None:
        where, params = self._outbox_filter()
        if _table_exists(src, "outbox_messages"):
            pending, dead, handed = src.one(
                "SELECT"  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                " count(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),"
                " count(*) FILTER"
                " (WHERE delivered_at IS NULL AND dead_at IS NOT NULL),"
                " count(*) FILTER"
                " (WHERE delivered_at IS NOT NULL AND last_error LIKE %s)"
                f" FROM outbox_messages WHERE {where}",
                [_like_prefix(HANDOFF_MARKER), *params],
            )
            report.outbox_pending, report.outbox_dead = int(pending), int(dead)
            report.outbox_handed_off = int(handed)
        if _table_exists(src, "consumer_inbox"):
            where, iparams = self._inbox_filter()
            report.inbox_rows = int(
                src.one(f"SELECT count(*) FROM consumer_inbox WHERE {where}", iparams)[  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                    0
                ]
            )
        if _table_exists(src, "audit_log"):
            where, aparams = self._audit_filter()
            report.audit_rows = int(
                src.one(f"SELECT count(*) FROM audit_log WHERE {where}", aparams)[0]  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            )
        counts = {}
        counted = (*self._dedupe_tables(), ("saga_instances", self._saga_filter()))
        for table, (where, params) in counted:
            if _table_exists(src, table):
                counts[table] = int(
                    src.one(
                        f"SELECT count(*) FROM {_q(table)} WHERE {where}",  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                        params,
                    )[0]
                )
        report.command_rows = counts.get("contract_command_log", 0)
        report.http_rows = counts.get("http_idempotency_log", 0)
        report.saga_rows = counts.get("saga_instances", 0)

    def _inspect(  # noqa: C901
        self, src: _Conn, tgt: _Conn | None, report: ExtractionReport
    ) -> list[str]:
        """Fill the report; return the FK-ordered table list."""
        # The chain version first: a source behind the code head also lacks
        # the newer tables, and "upgrade the source" is the actionable fix.
        head = self._head()
        src_version = _chain_version(src, self.spec.version_table)
        if src_version != head:
            report.problems.append(
                f"source chain {self.spec.version_table} is at {src_version!r}, "
                f"code head is {head!r}: run 'sandloom migrate upgrade-module "
                f"{self.spec.short} --apply' on the source first"
            )
        missing = [t for t in self.spec.tables if not _table_exists(src, t)]
        if missing:
            report.problems.append(f"source lacks module tables: {missing}")
            return []
        ordered, outside = _fk_order(src, self.spec.tables)
        if outside:
            report.problems.append(
                "foreign keys leave the module (a module may not reference "
                f"another module's tables): {outside}"
            )
        rls = _rls_problem(src, self.spec.tables, "source")
        if rls:
            report.problems.append(rls)
        if tgt is not None:
            target_rls = _target_rls_problem(src, tgt, self.spec.tables)
            if target_rls:
                report.problems.append(target_rls)
        report.fenced = self._is_fenced(src)
        manifest = self._manifest(tgt) if tgt is not None else {}
        report.complete = COMPLETE_MARKER in manifest
        for table in ordered:
            cols = _columns(src, table)
            rows, digest = _checksum(src, table, cols)
            state = TableState(table, rows, digest)
            if table in manifest:
                state.manifest_checksum = manifest[table][1]
            if tgt is not None and _table_exists(tgt, table):
                tgt_cols = _columns(tgt, table)
                if set(tgt_cols) != set(cols):
                    report.problems.append(
                        f"column mismatch on {table}: source={cols} "
                        f"target={tgt_cols} (target chain at another revision?)"
                    )
                    state.status = "conflict"
                else:
                    state.target_rows, state.target_checksum = _checksum(
                        tgt, table, cols
                    )
            if state.status != "conflict":
                if state.manifest_checksum is not None:
                    if state.manifest_checksum == digest:
                        state.status = "done"
                    else:
                        state.status = "conflict"
                        report.problems.append(
                            f"{table}: the source changed after it was copied "
                            "(manifest checksum differs) — was the fence removed?"
                        )
                elif state.target_rows:
                    state.status = "conflict"
                    report.problems.append(
                        f"{table}: target already holds {state.target_rows} rows "
                        "not written by this tool — refusing to overwrite"
                    )
            report.tables.append(state)
        for seq in _owned_sequences(src, self.spec.tables):
            value, called = src.one(f"SELECT last_value, is_called FROM {_q(seq)}")  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            report.sequences.append((seq, int(value), bool(called)))
        self._counts(src, report)
        src.commit()
        if tgt is not None:
            tgt.commit()
        return ordered

    # -- public operations -------------------------------------------------
    def plan(self) -> ExtractionReport:
        """Read-only: what ``apply`` would do (no writes anywhere)."""
        report = self._report("extract")
        with _connect(self.source_url) as src, _connect(self.target_url) as tgt:
            self._inspect(src, tgt, report)
            if not report.fenced:
                report.actions.append(
                    "fence the source (module tables read-only, module outbox "
                    "channels refuse inserts)"
                )
            report.actions.append(
                "apply the core chain + the module chain to the target"
            )
            todo = [t.name for t in report.tables if t.status == "pending"]
            report.actions.append(f"copy + verify tables: {todo or 'none'}")
            report.actions.append(
                "set sequences, copy consumer_inbox/audit/command-log rows, hand off "
                "undelivered outbox rows, write the completion manifest"
            )
        return report

    def apply(self, *, writes_stopped: bool) -> ExtractionReport:
        if not writes_stopped:
            raise ExtractionError(
                "refusing to extract without --i-have-stopped-writes: stop every "
                "process that writes the module (app, workers, beat, consumers) "
                "first; the fence then makes the source read-only"
            )
        report = self._report("extract")
        with _connect(self.source_url) as src:
            preflight = self._report("extract")
            with _connect(self.target_url) as target:
                ordered = self._inspect(src, target, preflight)
            if preflight.problems:
                raise ExtractionError("; ".join(preflight.problems))
            self._fence(src)
            report.actions.append("source fenced")
            extensions = _required_extensions(src, self.spec.tables)
            src.commit()
            self._migrate_target(extensions)
            report.actions.append("target migrated (core + module chain at head)")
            with _connect(self.target_url) as tgt:
                self._ensure_manifest(tgt)
                ordered = self._inspect(src, tgt, report)
                if report.problems:
                    raise ExtractionError("; ".join(report.problems))
                source_identity = database_identity_of_url(self.source_url)
                for state in report.tables:
                    if state.status == "done":
                        continue
                    self._copy_table(src, tgt, state, source_identity)
                    report.actions.append(f"copied {state.name} ({state.source_rows})")
                    if self.on_table_copied is not None:
                        self.on_table_copied(state.name)
                self._copy_sequences(tgt, report)
                self._copy_inbox(src, tgt)
                self._copy_audit(src, tgt)
                self._copy_command_log(src, tgt)
                self._copy_http_log(src, tgt)
                self._copy_sagas(src, tgt)
                handed = self._handoff_outbox(src, tgt)
                report.actions.append(f"handed off {handed} outbox row(s)")
                self._verify(src, tgt, ordered)
                tgt.cursor().execute(
                    f"INSERT INTO {MANIFEST_TABLE} (module, table_name, row_count, "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                    "checksum, source_identity) VALUES (%s, %s, 0, 'ok', %s) "
                    "ON CONFLICT (module, table_name) DO NOTHING",
                    (self.spec.module, COMPLETE_MARKER, source_identity),
                )
                tgt.commit()
                final = self._report("extract")
                self._inspect(src, tgt, final)
                final.applied = True
                final.actions = report.actions
                return final

    def verify(self) -> ExtractionReport:
        report = self._report("verify")
        with _connect(self.source_url) as src, _connect(self.target_url) as tgt:
            ordered = self._inspect(src, tgt, report)
            self._verify(src, tgt, ordered)
        return report

    def drop_source(self, *, writes_stopped: bool, apply: bool) -> ExtractionReport:
        """Drop the module's tables (and inbox claims) from the source."""
        report = self._report("drop-source")
        with _connect(self.source_url) as src, _connect(self.target_url) as tgt:
            ordered = self._inspect(src, tgt, report)
            if not report.complete:
                report.problems.append(
                    "the target has no completed extraction manifest for this "
                    "module: run the extraction (--apply) first"
                )
            if not report.fenced:
                report.problems.append("the source is not fenced")
            bad = [t.name for t in report.tables if t.status != "done"]
            if bad:
                report.problems.append(
                    f"source tables do not match the copied manifest: {bad}"
                )
            if report.outbox_pending or report.outbox_dead:
                report.problems.append(
                    "the source still holds undelivered outbox rows of the "
                    "module: re-run the extraction to hand them off"
                )
            report.actions.append(
                f"DROP TABLE {list(reversed(ordered))} + {self.spec.version_table}; "
                "delete the module's consumer_inbox claims, command records and "
                "saga instances; "
                "keep audit_log rows, "
                "handed-off outbox rows and the outbox fence"
            )
            if not apply or report.problems:
                return report
            if not writes_stopped:
                raise ExtractionError(
                    "refusing --drop-source without --i-have-stopped-writes"
                )
            cur = src.cursor()
            cur.execute(f"SET lock_timeout = '{self.lock_timeout_ms}ms'")
            for table in reversed(ordered):
                cur.execute(f"DROP TABLE IF EXISTS {_q(table)}")
            cur.execute(f"DROP TABLE IF EXISTS {_q(self.spec.version_table)}")
            claims = (
                ("consumer_inbox", self._inbox_filter()),
                *self._dedupe_tables(),
                *self._saga_tables(),
            )
            for table, (where, params) in claims:
                if _table_exists(src, table):
                    cur.execute(f"DELETE FROM {_q(table)} WHERE {where}", params)  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            src.commit()
            report.applied = True
        return report

    def unfence(self, *, apply: bool, force: bool = False) -> ExtractionReport:
        """Remove the source fence (roll back before the cutover)."""
        report = self._report("unfence")
        with _connect(self.source_url) as src, _connect(self.target_url) as tgt:
            report.fenced = self._is_fenced(src)
            manifest = self._manifest(tgt)
            report.complete = COMPLETE_MARKER in manifest
            if report.complete and not force:
                report.problems.append(
                    "the extraction completed: the new service may already have "
                    "taken writes that the source does not have. Unfencing makes "
                    "the stale source writable again — pass --force only if the "
                    "target never served traffic"
                )
            report.actions.append(
                "drop the fence triggers on the module tables and the module's "
                "outbox fence"
            )
            if not apply or report.problems:
                return report
            cur = src.cursor()
            cur.execute(f"SET lock_timeout = '{self.lock_timeout_ms}ms'")
            for table in self.spec.tables:
                if _table_exists(src, table):
                    cur.execute(
                        f"DROP TRIGGER IF EXISTS {FENCE_TRIGGER} ON {_q(table)}"
                    )
            if _table_exists(src, "outbox_messages"):
                cur.execute(
                    f"DROP TRIGGER IF EXISTS {self._outbox_fence_name()} "
                    "ON outbox_messages"
                )
            src.commit()
            report.applied = True
            report.fenced = False
        return report

    # -- steps -------------------------------------------------------------
    def _fence(self, src: _Conn) -> None:
        cur = src.cursor()
        try:
            cur.execute(f"SET LOCAL lock_timeout = '{self.lock_timeout_ms}ms'")
            cur.execute(
                f"CREATE OR REPLACE FUNCTION {FENCE_FUNCTION}() RETURNS trigger "
                "LANGUAGE plpgsql AS $fence$ BEGIN RAISE EXCEPTION "
                "'sandloom: % is read-only: module % is fenced for extraction to "
                "another database (sandloom migrate extract-module)', TG_TABLE_NAME, "
                "TG_ARGV[0] USING ERRCODE = 'read_only_sql_transaction'; "
                "END $fence$"
            )
            module = _lit(self.spec.module)
            for table in self.spec.tables:
                cur.execute(f"DROP TRIGGER IF EXISTS {FENCE_TRIGGER} ON {_q(table)}")
                cur.execute(
                    f"CREATE TRIGGER {FENCE_TRIGGER} BEFORE INSERT OR UPDATE OR "
                    f"DELETE OR TRUNCATE ON {_q(table)} FOR EACH STATEMENT "
                    f"EXECUTE FUNCTION {FENCE_FUNCTION}({module})"
                )
            if _table_exists(src, "outbox_messages"):
                name = self._outbox_fence_name()
                cond = " OR ".join(
                    f"NEW.channel LIKE {_lit(_like_prefix(p))}"
                    for p in self.spec.channel_prefixes
                )
                cur.execute(f"DROP TRIGGER IF EXISTS {name} ON outbox_messages")
                cur.execute(
                    f"CREATE TRIGGER {name} BEFORE INSERT ON outbox_messages "
                    f"FOR EACH ROW WHEN ({cond}) "
                    f"EXECUTE FUNCTION {FENCE_FUNCTION}({module})"
                )
            src.commit()
        except Exception as exc:
            src.rollback()
            if getattr(exc, "pgcode", None) == "55P03":  # lock_not_available
                raise ExtractionError(
                    "could not fence the source within "
                    f"{self.lock_timeout_ms} ms: live transactions hold locks on "
                    "the module's tables — the module still has writers. Stop "
                    "them and re-run."
                ) from exc
            raise

    def _migrate_target(self, extensions: Sequence[str] = ()) -> None:
        prev = os.environ.pop(MIGRATE_URL_ENV, None)
        try:
            with _migrations.migration_lock([self.target_url]):
                if extensions:
                    # The module chain may rely on an extension another
                    # module's chain created in the shared database (tenancy's
                    # citext comes from identity's chain): the new database
                    # needs it before the chain runs.
                    with _connect(self.target_url) as tgt:
                        for ext in extensions:
                            tgt.cursor().execute(
                                f"CREATE EXTENSION IF NOT EXISTS {_q(ext)}"
                            )
                        tgt.commit()
                _migrations.run_core_upgrade(
                    dry_run=False, database_url=self.target_url
                )
                cfg = Config()
                cfg.set_main_option("script_location", self.spec.migrations_path)
                cfg.set_main_option(
                    "sqlalchemy.url", self.target_url.replace("%", "%%")
                )
                cfg.set_main_option("version_table", self.spec.version_table)
                command.upgrade(cfg, "head")
        finally:
            if prev is not None:
                os.environ[MIGRATE_URL_ENV] = prev

    def _copy_table(
        self, src: _Conn, tgt: _Conn, state: TableState, source_identity: str
    ) -> None:
        cols = _columns(src, state.name)
        collist = ", ".join(_q(c) for c in cols)
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as buf:
            src.cursor().copy_expert(
                f"COPY (SELECT {collist} FROM {_q(state.name)}) TO STDOUT",  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                buf,
            )
            src.commit()
            buf.seek(0)
            try:
                tgt.cursor().copy_expert(
                    f"COPY {_q(state.name)} ({collist}) FROM STDIN", buf
                )
                rows, digest = _checksum(tgt, state.name, cols)
                if (rows, digest) != (state.source_rows, state.source_checksum):
                    raise ExtractionError(
                        f"{state.name}: verification failed after copy "
                        f"(source {state.source_checksum}, target {digest})"
                    )
                tgt.cursor().execute(
                    f"INSERT INTO {MANIFEST_TABLE} (module, table_name, row_count, "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                    "checksum, source_identity) VALUES (%s, %s, %s, %s, %s)",
                    (self.spec.module, state.name, rows, digest, source_identity),
                )
                tgt.commit()
            except Exception:
                tgt.rollback()
                raise
        state.target_rows, state.target_checksum = state.source_rows, digest
        state.status = "copied"

    def _copy_sequences(self, tgt: _Conn, report: ExtractionReport) -> None:
        cur = tgt.cursor()
        for seq, value, called in report.sequences:
            if not _table_exists(tgt, seq):
                raise ExtractionError(f"sequence {seq} is missing in the target")
            cur.execute("SELECT setval(%s, %s, %s)", (_q(seq), value, called))
        tgt.commit()

    def _copy_rows(
        self,
        src: _Conn,
        tgt: _Conn,
        table: str,
        columns: Sequence[str],
        where: str,
        params: Sequence[Any],
        key: Sequence[str],
    ) -> None:
        """Copy the rows matching ``where`` (idempotent: ON CONFLICT DO NOTHING).

        Streams like :meth:`_copy_table`: ``COPY (SELECT …) TO STDOUT`` into a
        spooled file, ``COPY`` into a temporary table on the target, then one
        set-based ``INSERT … ON CONFLICT DO NOTHING`` and one set-based check
        that every copied key is present (and matches ``where``) in the
        target. Memory stays bounded by the spool, whatever the row count.
        """
        if not _table_exists(src, table):
            return
        collist = ", ".join(_q(c) for c in columns)
        select = (
            src.cursor()
            .mogrify(f"SELECT {collist} FROM {_q(table)} WHERE {where}", params)  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            .decode()
        )
        staging = _q(f"_sandloom_extract_{table}")
        match = " AND ".join(f"t.{_q(k)} = s.{_q(k)}" for k in key)
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as buf:
            src.cursor().copy_expert(f"COPY ({select}) TO STDOUT", buf)
            src.commit()
            buf.seek(0)
            cur = tgt.cursor()
            try:
                cur.execute(
                    f"CREATE TEMP TABLE {staging} "
                    f"(LIKE {_q(table)} INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                cur.copy_expert(f"COPY {staging} ({collist}) FROM STDIN", buf)
                cur.execute(
                    f"INSERT INTO {_q(table)} ({collist}) "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                    f"SELECT {collist} FROM {staging} ON CONFLICT DO NOTHING"
                )
                where_t = cur.mogrify(where, params).decode()
                cur.execute(
                    f"SELECT count(*) FROM {staging} s WHERE NOT EXISTS "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                    f"(SELECT 1 FROM {_q(table)} t WHERE {match} AND ({where_t}))"
                )
                missing = cur.fetchone()[0]
                if missing:
                    raise ExtractionError(
                        f"{table}: {missing} row(s) missing in the target after copy"
                    )
                tgt.commit()
            except Exception:
                tgt.rollback()
                raise

    def _copy_inbox(self, src: _Conn, tgt: _Conn) -> None:
        where, params = self._inbox_filter()
        self._copy_rows(
            src,
            tgt,
            "consumer_inbox",
            ("id", "consumer", "event_id", "status", "processed_at"),
            where,
            params,
            key=("consumer", "event_id"),
        )

    def _copy_command_log(self, src: _Conn, tgt: _Conn) -> None:
        where, params = self._command_filter()
        self._copy_rows(
            src,
            tgt,
            "contract_command_log",
            (
                "dedupe_key",
                "contract",
                "tenant_id",
                "args_digest",
                "status",
                "result",
                "created_at",
                "expires_at",
            ),
            where,
            params,
            key=("dedupe_key",),
        )

    def _copy_http_log(self, src: _Conn, tgt: _Conn) -> None:
        where, params = self._http_filter()
        self._copy_rows(
            src,
            tgt,
            "http_idempotency_log",
            (
                "dedupe_key",
                "scope",
                "tenant_id",
                "args_digest",
                "status",
                "result",
                "created_at",
                "expires_at",
            ),
            where,
            params,
            key=("dedupe_key",),
        )

    def _copy_sagas(self, src: _Conn, tgt: _Conn) -> None:
        """The module's saga instances, then their step rows (same filter)."""
        where, params = self._saga_filter()
        for table, key in (
            ("saga_instances", ("id",)),
            ("saga_steps", ("instance_id", "step", "direction")),
        ):
            if _table_exists(src, table):
                self._copy_rows(
                    src, tgt, table, _columns(src, table), where, params, key=key
                )

    def _copy_audit(self, src: _Conn, tgt: _Conn) -> None:
        where, params = self._audit_filter()
        self._copy_rows(
            src,
            tgt,
            "audit_log",
            (
                "id",
                "occurred_at",
                "actor_id",
                "actor_type",
                "action",
                "resource_type",
                "resource_id",
                "data",
                "request_id",
                "trace_id",
            ),
            where,
            params,
            key=("id",),
        )

    def _handoff_outbox(self, src: _Conn, tgt: _Conn) -> int:
        """Move undelivered module events to the target outbox (same ids)."""
        if not _table_exists(src, "outbox_messages"):
            return 0
        where, params = self._outbox_filter()
        rows = src.all(
            "SELECT id, channel, schema_version, payload::text, emitted_at, "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
            "delivery_attempts, last_error, dead_at, next_attempt_at "
            f"FROM outbox_messages WHERE delivered_at IS NULL AND {where} "
            "ORDER BY emitted_at, id FOR UPDATE",
            params,
        )  # the source transaction stays open: the rows stay locked
        if not rows:
            src.commit()
            return 0
        try:
            execute_values(
                tgt.cursor(),
                "INSERT INTO outbox_messages (id, channel, schema_version, payload, "
                "emitted_at, delivery_attempts, last_error, dead_at, "
                "next_attempt_at) VALUES %s ON CONFLICT (id) DO NOTHING",
                rows,
                template="(%s, %s, %s, %s::json, %s, %s, %s, %s, %s)",
            )
            ids = [str(r[0]) for r in rows]
            present = tgt.one(
                "SELECT count(*) FROM outbox_messages WHERE id::text = ANY(%s)",
                (ids,),
            )[0]
            if int(present) != len(ids):
                raise ExtractionError("outbox hand-off: rows missing in the target")
            tgt.commit()
        except Exception:
            tgt.rollback()
            src.rollback()
            raise
        marker = HANDOFF_MARKER + database_identity_of_url(self.target_url)
        src.cursor().execute(
            "UPDATE outbox_messages SET delivered_at = (now() AT TIME ZONE 'utc'), "
            "last_error = %s || CASE WHEN dead_at IS NOT NULL "
            "THEN ' (was dead-lettered)' ELSE '' END, "
            "dead_at = NULL, locked_until = NULL, locked_by = NULL "
            "WHERE id::text = ANY(%s)",
            (marker, ids),
        )
        src.commit()
        return len(rows)

    def _verify(self, src: _Conn, tgt: _Conn, ordered: Sequence[str]) -> None:
        manifest = self._manifest(tgt)
        problems: list[str] = []
        for table in ordered:
            cols = _columns(src, table)
            source = _checksum(src, table, cols)
            target = _checksum(tgt, table, cols)
            recorded = manifest.get(table)
            if recorded is None or recorded[1] != source[1]:
                problems.append(f"{table}: no matching manifest entry")
            elif target != source and COMPLETE_MARKER not in manifest:
                problems.append(f"{table}: source {source[1]} != target {target[1]}")
        where, params = self._outbox_filter()
        if _table_exists(src, "outbox_messages"):
            left = src.one(
                "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL "  # noqa: S608 - _q()-quoted identifiers / framework constants; values bound
                f"AND {where}",
                params,
            )[0]
            if int(left):
                problems.append(f"{left} undelivered module outbox row(s) remain")
        src.commit()
        tgt.commit()
        if problems:
            raise ExtractionError("verification failed: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Streams consumer-group ownership
# ---------------------------------------------------------------------------


async def transfer_group_owners(
    transport: Any,
    module: str,
    *,
    source_identity: str,
    target_identity: str,
    apply: bool,
) -> list[str]:
    """Re-point the module's consumer groups from the source to the target DB.

    A group's owner fingerprint (``sandloom:group-owner:<stream>:<group>``) pins it
    to ``(module, database)`` because its exactly-once inbox lives in that
    database. The extraction copied the inbox claims, so the group may move:
    groups owned by ``module`` on ``source_identity`` are rewritten to
    ``target_identity`` (the new service then claims them without a conflict,
    and a stale monolith still loading the module is refused). Returns the
    ``stream group`` pairs (to be) transferred.
    """
    lister = getattr(transport, "list_group_owners", None)
    transfer = getattr(transport, "transfer_group_owner", None)
    if lister is None or transfer is None:
        return []
    short = _module_name(module)
    moved: list[str] = []
    for entry in await lister():
        owner = entry.get("owner") or {}
        if owner.get("module") not in (module, short):
            continue
        if owner.get("database") != source_identity:
            continue
        stream, group = str(entry["stream"]), str(entry["group"])
        if apply:
            new_owner = {
                **owner,
                "database": target_identity,
                "transferred_from": source_identity,
            }
            if not await transfer(stream, group, expected=owner, owner=new_owner):
                raise ExtractionError(
                    f"group {group} on {stream}: owner changed concurrently"
                )
        moved.append(f"{stream} {group}")
    return moved


__all__ = [
    "COMPLETE_MARKER",
    "ExtractionError",
    "ExtractionReport",
    "ModuleDataSpec",
    "ModuleExtractor",
    "database_identity_of_url",
    "module_data_spec",
    "module_tables",
    "transfer_group_owners",
]
