"""Backup plan and restore verification (``sandloom backup plan|verify``, H-20).

* :func:`build_backup_plan` — what must be backed up, per configured database
  instance and per server a driver says holds data (security store, cache,
  broker, event bus: ``InfraProvider.backup_hint``), and how: ``pg_dump`` + PITR
  for PostgreSQL, what the drivers advise for their servers, and what a restore
  of each implies (``sandloom backup reconcile-security`` for the security
  atomic_store).
* :func:`verify_dump` — restore a ``pg_dump`` into a throw-away database on a
  scratch server, check that every migration chain the database hosts is at
  its head and run basic integrity checks, then drop it. A backup that was
  never restored is not a backup.

PostgreSQL access is plain psycopg2 (sync, like ``sandloom-migrate``); the restore
itself shells out to ``pg_restore`` (custom / directory / tar dumps) or
``psql`` (plain SQL), feeding the dump on stdin so a containerised client
(``--pg-restore "docker exec -i pg pg_restore"``) works the same way.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.config import module_settings as _module_settings
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.infra.config import DEFAULT_INSTANCE
from sandloom.core.infra.ports._base import BACKUP_CRITICALITIES, BackupHint
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.infra.tool_infra import filled_infra_config
from sandloom.core.migrations.migrations import (
    _mask_url,
    core_chain_dir,
    version_table_for,
)
from sandloom.core.migrations.pg_sync import psycopg2, sql
from sandloom.core.migrations.urls import to_sync_url

#: Shared infra tables of the core chain every hosting database carries. Kept
#: literal (deriving it reads the chain's revisions: I/O at import); a test
#: asserts it equals what the core chain creates.
CORE_TABLES = (
    "outbox_messages",
    "audit_log",
    "consumer_inbox",
    "contract_command_log",
    "http_idempotency_log",
    "saga_instances",
    "saga_steps",
    "projection_watermarks",
    "infra_lease",
    "infra_atomic_store",
)
#: What a restore adds to every ``infra_lease`` fence (see :func:`lift_fences`).
#: A restored dump rewinds fences to the snapshot; the holders of the lost
#: window kept tokens above it. The offset must exceed the acquisitions of one
#: lease name between the snapshot and the restore (10**12: never reached).
LEASE_FENCE_OFFSET = 10**12
#: Version table of the core chain.
CORE_VERSION_TABLE = "alembic_version"
#: Prefix of the scratch databases :func:`verify_dump` creates (and drops).
SCRATCH_PREFIX = "sandloom_restore_verify_"


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PlanItem:
    # "postgres" | "objectstore" | "secrets" | "warning", or the kind a driver's
    # ``BackupHint`` names for its server (``redis``)
    kind: str
    name: str
    target: str  # masked URL / path
    roles: list[str] = field(default_factory=list)
    criticality: str = "required"  # "critical" | "required" | "optional"
    backup: list[str] = field(default_factory=list)
    restore: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "target": self.target,
            "roles": self.roles,
            "criticality": self.criticality,
            "backup": self.backup,
            "restore": self.restore,
        }


def _hosted_chains(
    database: str, descriptors: Sequence[ModuleDescriptor]
) -> list[tuple[str, str, str]]:
    """``[(label, script_location, version_table)]`` of the chains in ``database``."""
    chains = [("core", str(core_chain_dir()), CORE_VERSION_TABLE)]
    for d in descriptors:
        if d.migrations_path and database in d.database_instances():
            chains.append((d.name, d.migrations_path, version_table_for(d)))  # noqa: PERF401
    return chains


def _postgres_item(
    name: str, url: str, descriptors: Sequence[ModuleDescriptor]
) -> PlanItem:
    masked = _mask_url(_libpq_url(url))
    chains = _hosted_chains(name, descriptors)
    stamp = "$(date -u +%Y%m%dT%H%M%SZ)"
    return PlanItem(
        kind="postgres",
        name=name,
        target=masked,
        roles=[f"chain {label} ({table})" for label, _p, table in chains],
        criticality="critical",
        backup=[
            # The OWNER/migration role: under tenant_rls (FORCEd, no bypass)
            # the app role sees one tenant at a time and pg_dump refuses.
            f"pg_dump --format=custom --no-owner --no-privileges "
            f'--file={name}-{stamp}.dump "{masked}"  # as the owner/migration '
            "role, NOT the app role (tenant RLS)",
            "PITR: wal_level=replica, archive_mode=on + archive_command (or the "
            "managed service's PITR) and a periodic pg_basebackup; keep WAL at "
            "least back to the oldest base backup you retain",
            f"sandloom backup verify {name}-<stamp>.dump --database {name}  "
            "# restore drill: schedule it, alert when it fails",
        ],
        restore=[
            "stop writers (scale app / worker / beat to 0) before restoring",
            "pg_restore --no-owner --no-privileges --exit-on-error into an empty "
            "database, or PITR to the chosen instant",
            "sandloom-migrate upgrade-all --apply (a restored database may be behind "
            "the deployed code)",
            "outbox rows pending at the snapshot are re-published by the relay "
            "(at-least-once: consumers dedupe on the envelope id)",
            "sandloom backup reconcile-security --apply BEFORE admitting traffic "
            "(audit rows newer than the snapshot are gone: pass the off-site "
            "audit export with --audit-export)",
            "sandloom backup fence-leases --apply --reason ... BEFORE starting "
            "writers (a dump rewinds the infra_lease fences below tokens a stale "
            "holder still carries)",
        ],
    )


def _atomic_store_note(name: str, atomic: Mapping[str, Any]) -> str:
    """What the ``atomic_store`` instance ``name`` holds for the kernel, as the
    suffix of its role line (empty for an instance of another name)."""
    if name == "security":
        tail = "]" if "idempotency" in atomic else ", idempotency]"
        return " [SECURITY: revocation marks, user blocks, RPC nonces" + tail
    if name == "idempotency":
        return " [IDEMPOTENCY: guard / task-enqueue dedupe records]"
    return ""


def _atomic_store_roles(
    atomic: Mapping[str, Any], driver: str
) -> list[tuple[str, str]]:
    """``(url, role)`` of every ``atomic_store`` instance of the ``database``
    driver (the rows of ``infra_atomic_store``)."""
    where = " (infra_atomic_store)" if driver == "database" else ""
    found: list[tuple[str, str]] = []
    for name, inst in sorted(atomic.items()):
        url = inst.options.get("url")
        if inst.driver != driver or not isinstance(url, str) or not url:
            continue
        note = _atomic_store_note(name, atomic)
        found.append((url, f"atomic_store:{name}{where}{note}"))
    return found


def _database_store_items(
    atomic: Mapping[str, Any],
    postgres: dict[str, PlanItem],
    descriptors: Sequence[ModuleDescriptor],
) -> list[PlanItem]:
    """Fold the ``database`` atomic_store instances into the plan.

    An instance on a database the plan already lists adds its role (and, for the
    security store, the restore warning) to that item; one on a URL of its own
    becomes an item of its own. ``postgres`` maps libpq URL -> item."""
    extra: list[PlanItem] = []
    for url, role in _atomic_store_roles(atomic, "database"):
        security = "SECURITY" in role
        key = _libpq_url(url)
        item = postgres.get(key)
        if item is None:
            item = _postgres_item(role.split(" ", 1)[0], url, descriptors)
            postgres[key] = item
            extra.append(item)
        item.roles.append(role)
        if security:
            item.restore.append(
                "infra_atomic_store holds the revocation marks and user blocks: a "
                "dump older than a later block / revocation LIFTS it silently: run "
                "sandloom backup reconcile-security --apply before admitting traffic"
            )
    return extra


#: The restore notes of the ``security`` atomic_store instance, whichever driver
#: holds it: they name the kernel's own repair commands.
_SECURITY_RESTORE = (
    "a snapshot older than a later block / revocation LIFTS it silently: "
    "run sandloom backup reconcile-security --apply before admitting traffic",
    "if the snapshot age is unknown and the outbox no longer covers the "
    "access-token lifetime: sandloom security revoke-sessions --all (or rotate "
    "the JWT signing key)",
)
#: Capabilities the plan lists by itself (a database dump, an object store's
#: copy); every other one asks its drivers (``InfraProvider.backup_hint``).
_LISTED_BY_THE_PLAN = frozenset({"database", "objectstore"})
#: The order the roles of a server are listed in; another capability follows by name.
_ROLE_ORDER = ("cache", "atomic_store", "eventbus", "taskqueue")


@dataclass(slots=True)
class _Server:
    """What the drivers say about one server: the instances that run on it."""

    kind: str
    url: str
    roles: list[str] = field(default_factory=list)
    criticality: str = "optional"
    backup: list[str] = field(default_factory=list)
    restore: list[str] = field(default_factory=list)
    tolerable: list[str] = field(default_factory=list)

    def add(self, role: str, hint: BackupHint) -> None:
        self.roles.append(role)
        self.criticality = min(
            self.criticality, hint.criticality, key=BACKUP_CRITICALITIES.index
        )
        _restate(self.backup, hint.backup)
        _restate(self.restore, hint.restore)
        if hint.tolerable_loss is not None:
            self.tolerable.append(hint.tolerable_loss)

    def item(self) -> PlanItem:
        restore = list(self.restore)
        # A server that also holds something that must survive has no tolerable loss.
        if self.tolerable and len(self.tolerable) == len(self.roles):
            restore.extend(dict.fromkeys(self.tolerable))
        return PlanItem(
            kind=self.kind,
            name=", ".join(r.split(" ")[0] for r in self.roles),
            target=_mask_url(self.url),
            roles=self.roles,
            criticality=self.criticality,
            backup=self.backup,
            restore=restore,
        )


def _restate(steps: list[str], new: Sequence[str]) -> None:
    """Add ``new`` to ``steps``: a step already listed moves to its latest place."""
    for step in new:
        if step in steps:
            steps.remove(step)
        steps.append(step)


def _default_cache_url(config: Any, registry: InfraRegistry) -> str | None:
    """The url ``cache.default`` is configured with (the option its driver
    declares as the one that holds it)."""
    default = (config.get("cache") or {}).get(DEFAULT_INSTANCE)
    driver = registry.driver_class("cache", default.driver) if default else None
    option = driver.url_option if driver is not None else None
    url = default.options.get(option) if default is not None and option else None
    return url if isinstance(url, str) and url else None


def _role_rank(capability: str) -> tuple[int, str]:
    known = capability in _ROLE_ORDER
    return (_ROLE_ORDER.index(capability) if known else len(_ROLE_ORDER), capability)


#: ``atomic_store`` instances the kernel needs to survive a restore. A driver
#: that returns no hint for one leaves it out of the plan: the plan says so.
_MUST_SURVIVE = ("security", "idempotency")
#: Drivers of those instances the plan needs no hint of: the database store is
#: folded into its database's item, the memory store is lost by design.
_NEEDS_NO_HINT = frozenset({"database", "memory"})


def _servers(
    config: Any, registry: InfraRegistry
) -> tuple[list[PlanItem], list[str], list[str]]:
    """``(items, missing, silent)``: one item per server the configured drivers
    say hold data (their ``backup_hint``), the ``capability:instance`` of every
    instance whose driver is not installed here, which cannot say, and that of
    every security or idempotency store whose driver gave no hint."""
    servers: dict[tuple[str, str], _Server] = {}
    missing: list[str] = []
    silent: list[str] = []
    for capability in sorted(
        (c for c in config if c not in _LISTED_BY_THE_PLAN), key=_role_rank
    ):
        for name, inst in sorted((config.get(capability) or {}).items()):
            driver = registry.driver_class(capability, inst.driver)
            if driver is None:
                missing.append(f"{capability}:{name} ({inst.driver})")
                continue
            hint = driver.backup_hint(inst.options)
            if hint is None:
                if (
                    capability == "atomic_store"
                    and name in _MUST_SURVIVE
                    and inst.driver not in _NEEDS_NO_HINT
                ):
                    silent.append(f"{capability}:{name} ({inst.driver})")
                continue
            url = hint.url or _default_cache_url(config, registry)
            if url is None:  # no url anywhere: no server to back up
                continue
            role = f"{capability}:{name}"
            if capability == "atomic_store":
                role += _atomic_store_note(name, config["atomic_store"])
                if name == "security":
                    hint = replace(
                        hint,
                        criticality="critical",
                        restore=(*_SECURITY_RESTORE, *hint.restore),
                    )
                elif name == "idempotency" and hint.criticality == "optional":
                    # The kernel keeps idempotency through a restore (_MUST_SURVIVE):
                    # its server is at least required, a stronger driver value stays.
                    hint = replace(hint, criticality="required")
            servers.setdefault((hint.kind, url), _Server(hint.kind, url)).add(
                role, hint
            )
    return [server.item() for server in servers.values()], missing, silent


def _secrets_item(models: Sequence[type[BaseModel]]) -> PlanItem:
    """The secret manager's item: the kernel's keys, plus what each loaded
    module's settings declare it holds (``backup``, ADR-0015): the kernel
    names no module's secret."""
    backup = [
        "JWT_SECRET / JWT_PRIVATE_KEY + JWT_PUBLIC_KEYS, RPC keys, "
        "INTERNAL_API_TOKEN — in the secret manager, NEVER next to the "
        "database dumps (a dump plus a key can expose what it encrypts)"
    ]
    restore: list[str] = []
    for model in models:
        for keep, lose in _module_settings.declared_backup(model):
            if keep:
                backup.append(
                    f"{keep} — in the secret manager, never next to the dumps"
                )
            if lose:
                restore.append(lose)
    return PlanItem(
        kind="secrets",
        name="keys",
        target="secret manager",
        criticality="critical",
        backup=backup,
        restore=restore,
    )


def build_backup_plan(
    settings: Any,
    descriptors: Sequence[ModuleDescriptor] = (),
    models: Sequence[type[BaseModel]] = (),
) -> list[PlanItem]:
    """Every backup the deployment configured by ``settings`` needs.

    ``models``: the settings declarations of the loaded modules
    (``core.module_settings.records()``), whose secrets the plan lists."""
    # Filled as the application is: ``load_infra_config`` alone fills no driver
    # option, so a bound a driver takes from the settings would be missing.
    config, unfilled, registry = filled_infra_config(settings)
    items: list[PlanItem] = []
    postgres: dict[str, PlanItem] = {}
    for name, inst in sorted((config.get("database") or {}).items()):
        url = inst.options.get("url")
        if isinstance(url, str) and url:
            items.append(item := _postgres_item(name, url, descriptors))
            postgres[_libpq_url(url)] = item
    items.extend(
        _database_store_items(config.get("atomic_store") or {}, postgres, descriptors)
    )
    # Drivers that did not all load cannot say which servers hold data: the
    # warning below says so, and none is listed from a half-loaded registry.
    servers, missing, silent = (
        _servers(config, registry) if unfilled is None else ([], [], [])
    )
    items.extend(servers)
    for name, inst in sorted((config.get("objectstore") or {}).items()):
        where = inst.options.get("bucket") or inst.options.get("path") or "?"
        items.append(
            PlanItem(
                kind="objectstore",
                name=name,
                target=f"{inst.driver}:{where}",
                backup=[
                    "local: snapshot / rsync the directory; s3: bucket versioning "
                    "+ replication"
                ],
                restore=["restore to the same logical keys; no reconciliation"],
            )
        )
    unlisted: list[str] = []
    if unfilled is not None:
        unlisted.append(
            f"an installed driver failed to load ({unfilled}): this plan cannot "
            "ask the drivers which servers hold your data, so it lists none of "
            "the cache, atomic_store, event bus or task queue servers; fix the "
            "installation and rerun"
        )
    if missing:
        unlisted.append(
            f"no driver is installed here for {', '.join(missing)}: the servers "
            "behind them are not in this plan; run it where the drivers are "
            "installed"
        )
    if silent:
        unlisted.append(
            f"the driver of {', '.join(silent)} reports no backup hint: the "
            "server holding that state is not in this plan, and the state must "
            "survive a restore; back it up as its driver documents"
        )
    if unlisted:
        items.append(
            PlanItem(
                kind="warning",
                name="backing servers",
                target="incomplete",
                criticality="critical",
                backup=unlisted,
            )
        )
    items.append(_secrets_item(models))
    return items


def format_plan(items: Sequence[PlanItem]) -> str:
    lines: list[str] = []
    for item in items:
        lines.append(f"[{item.criticality}] {item.kind} {item.name}: {item.target}")
        for role in item.roles:
            lines.append(f"    holds  {role}")  # noqa: PERF401
        for step in item.backup:
            lines.append(f"    backup {step}")  # noqa: PERF401
        for step in item.restore:
            lines.append(f"    restore {step}")  # noqa: PERF401
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class VerifyReport:
    dump: str
    scratch: str
    checks: list[CheckResult] = field(default_factory=list)
    kept: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dump": self.dump,
            "scratch_database": self.scratch,
            "ok": self.ok,
            "kept": self.kept,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
        }


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(to_sync_url(url))
    return urlunsplit(parts._replace(path=f"/{database}"))


def _libpq_url(url: str) -> str:
    """``postgresql://`` form for libpq (psycopg2, ``pg_restore``, ``psql``)."""
    parts = urlsplit(to_sync_url(url))
    return urlunsplit(parts._replace(scheme="postgresql"))


@contextmanager
def scratch_database(server_url: str, *, keep: bool = False) -> Iterator[str]:
    """Create an empty database on ``server_url``'s server; drop it afterwards."""
    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    admin = _connect(_with_database(server_url, "postgres"), autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        try:
            yield _with_database(server_url, name)
        finally:
            if not keep:
                with admin.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


def dump_format(path: Path) -> str:
    """``custom`` / ``tar`` / ``directory`` (pg_restore) or ``plain`` (psql)."""
    if path.is_dir():
        return "directory"
    with path.open("rb") as handle:
        head = handle.read(512)
    if head.startswith(b"PGDMP"):
        return "custom"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return "plain"


Restorer = Callable[[Path, str], None]


def command_restorer(
    pg_restore: str = "pg_restore",
    psql: str = "psql",
    restore_url: str | None = None,
) -> Restorer:
    """A restorer that pipes the dump into ``pg_restore`` / ``psql``.

    ``restore_url``: the server as the CLIENT TOOL reaches it (a containerised
    client sees another host/port); only its scheme/credentials/host/port are
    used — the database name is always the scratch database.
    """

    def _restore(dump: Path, target_url: str) -> None:
        dbname = urlsplit(target_url).path.lstrip("/")
        url = _libpq_url(
            _with_database(restore_url, dbname) if restore_url else target_url
        )
        fmt = dump_format(dump)
        if fmt == "directory":
            argv = [
                *shlex.split(pg_restore),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={url}",
                str(dump),
            ]
            subprocess.run(argv, check=True)  # noqa: S603 - operator-chosen tool
            return
        if fmt == "plain":
            argv = [
                *shlex.split(psql),
                "--quiet",
                "--set",
                "ON_ERROR_STOP=1",
                f"--dbname={url}",
            ]
        else:
            argv = [
                *shlex.split(pg_restore),
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                f"--dbname={url}",
            ]
        with dump.open("rb") as stdin:
            subprocess.run(argv, stdin=stdin, check=True)  # noqa: S603 - operator-chosen tool

    return _restore


def _connect(url: str, *, autocommit: bool) -> Any:
    conn = psycopg2.connect(_libpq_url(url))
    conn.autocommit = autocommit
    return conn


def _rows(conn: Any, statement: Any, params: Any = None) -> list[Any]:
    with conn.cursor() as cur:
        cur.execute(statement, params)
        return list(cur.fetchall()) if cur.description else []


def _table_exists(conn: Any, table: str) -> bool:
    return bool(_rows(conn, "SELECT to_regclass(%s) IS NOT NULL", (table,))[0][0])


def _chain_heads(script_location: str) -> set[str]:
    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    return set(ScriptDirectory.from_config(cfg).get_heads())


def check_restored_database(
    url: str,
    chains: Sequence[tuple[str, str, str]],
) -> list[CheckResult]:
    """Migration heads + basic integrity of a (restored) database at ``url``."""
    results: list[CheckResult] = []
    conn = _connect(url, autocommit=True)
    try:
        for label, location, table in chains:
            heads = _chain_heads(location)
            if not _table_exists(conn, table):
                results.append(CheckResult(f"chain {label}", False, f"{table} missing"))
                continue
            stamped = {
                r[0]
                for r in _rows(
                    conn,
                    sql.SQL("SELECT version_num FROM {}").format(sql.Identifier(table)),
                )
            }
            behind = stamped != heads
            results.append(
                CheckResult(
                    f"chain {label}",
                    not behind,
                    f"at {sorted(stamped)}"
                    + (f", head is {sorted(heads)}" if behind else " (head)"),
                )
            )
        for table in CORE_TABLES:
            if _table_exists(conn, table):
                count = _rows(
                    conn,
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)),
                )[0][0]
                results.append(CheckResult(f"table {table}", True, f"{count} rows"))
            else:
                results.append(CheckResult(f"table {table}", False, "missing"))
        invalid = _rows(
            conn,
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE NOT i.indisvalid",
        )
        results.append(
            CheckResult(
                "indexes valid",
                not invalid,
                ", ".join(r[0] for r in invalid) or "all valid",
            )
        )
        unvalidated = _rows(
            conn,
            "SELECT conrelid::regclass::text || '.' || conname FROM pg_constraint "
            "WHERE NOT convalidated AND connamespace = 'public'::regnamespace",
        )
        results.append(
            CheckResult(
                "constraints validated",
                not unvalidated,
                ", ".join(r[0] for r in unvalidated) or "all validated",
            )
        )
        # Foreign keys hold (a partial / out-of-order restore can break them).
        orphans: list[str] = []
        for (
            conname,
            child_schema,
            child,
            parent_schema,
            parent,
            child_cols,
            parent_cols,
        ) in _rows(
            conn,
            "SELECT c.conname, child_ns.nspname, child_rel.relname, "
            "parent_ns.nspname, parent_rel.relname, "
            "array(SELECT attname FROM unnest(c.conkey) WITH ORDINALITY k(n, o) "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n "
            "ORDER BY k.o), "
            "array(SELECT attname FROM unnest(c.confkey) WITH ORDINALITY k(n, o) "
            "JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n "
            "ORDER BY k.o) "
            "FROM pg_constraint c "
            "JOIN pg_class child_rel ON child_rel.oid = c.conrelid "
            "JOIN pg_namespace child_ns ON child_ns.oid = child_rel.relnamespace "
            "JOIN pg_class parent_rel ON parent_rel.oid = c.confrelid "
            "JOIN pg_namespace parent_ns ON parent_ns.oid = parent_rel.relnamespace "
            "WHERE c.contype = 'f' AND child_ns.nspname = 'public'",
        ):
            on = sql.SQL(" AND ").join(
                sql.SQL("p.{} = ch.{}").format(sql.Identifier(pc), sql.Identifier(cc))
                for cc, pc in zip(child_cols, parent_cols, strict=True)
            )
            not_null = sql.SQL(" AND ").join(
                sql.SQL("ch.{} IS NOT NULL").format(sql.Identifier(cc))
                for cc in child_cols
            )
            missing = _rows(
                conn,
                sql.SQL(
                    "SELECT count(*) FROM {} ch WHERE {} AND NOT EXISTS "
                    "(SELECT 1 FROM {} p WHERE {})"
                ).format(
                    sql.Identifier(child_schema, child),
                    not_null,
                    sql.Identifier(parent_schema, parent),
                    on,
                ),
            )[0][0]
            if missing:
                orphans.append(f"{conname}: {missing}")
        results.append(
            CheckResult(
                "foreign keys hold",
                not orphans,
                ", ".join(orphans) or "no orphan rows",
            )
        )
        if _table_exists(conn, "audit_log"):
            newest = _rows(conn, "SELECT max(occurred_at) FROM audit_log")[0][0]
            results.append(
                CheckResult(
                    "newest audit row",
                    True,
                    newest.isoformat() if newest is not None else "none",
                )
            )
        if _table_exists(conn, "outbox_messages"):
            pending, dead = _rows(
                conn,
                "SELECT"
                " count(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),"
                " count(*) FILTER (WHERE dead_at IS NOT NULL) FROM outbox_messages",
            )[0]
            results.append(
                CheckResult(
                    "outbox state",
                    True,
                    f"{pending} pending (re-published after restore), {dead} dead",
                )
            )
    finally:
        conn.close()
    return results


def verify_dump(
    dump: str | os.PathLike[str],
    *,
    server_url: str,
    chains: Sequence[tuple[str, str, str]],
    restorer: Restorer | None = None,
    keep: bool = False,
) -> VerifyReport:
    """Restore ``dump`` into a scratch database on ``server_url``; check it.

    The scratch database (``sandloom_restore_verify_*``) is dropped afterwards
    unless ``keep``. A failing restore is reported as a failed check.
    """
    path = Path(dump)
    if not path.exists():
        raise FileNotFoundError(f"dump not found: {path}")
    restore = restorer or command_restorer()
    with scratch_database(server_url, keep=keep) as url:
        report = VerifyReport(
            dump=str(path), scratch=urlsplit(url).path.lstrip("/"), kept=keep
        )
        try:
            restore(path, url)
        except (OSError, subprocess.CalledProcessError) as exc:
            report.checks.append(CheckResult("restore", False, str(exc)))
            return report
        report.checks.append(CheckResult("restore", True, dump_format(path)))
        report.checks.extend(check_restored_database(url, chains))
        return report


# --------------------------------------------------------------------------- #
# Lease fences after a restore
# --------------------------------------------------------------------------- #
# This operates on the table of the ``database`` lease driver
# (``sandloom.core.drivers.database_lease``): the kernel's operator commands do
# not import a driver, and the lease contract has no fence-bump operation. The
# columns below are the ones the bump reads and writes; a test pins them to the
# driver's model (``test_lease_table_matches_the_driver``), so a driver change
# breaks it.
_LEASE_TABLE = Table(
    "infra_lease",
    MetaData(),
    Column("name", String(255), primary_key=True),
    Column("fence", BigInteger, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class FencedLease:
    name: str
    fence_before: int
    fence_after: int
    held: bool  # unexpired in the restored data (released by the bump)


async def lift_fences(
    session: AsyncSession, *, apply: bool, offset: int = LEASE_FENCE_OFFSET
) -> list[FencedLease]:
    """After restoring a database: lift every ``infra_lease`` fence by ``offset``
    and release every lease, so no holder of the lost window can outrank one.

    The driver issues ``fence + 1`` on every acquire and keeps the row after
    release to keep the counter. A restore to an older point rewinds it: a stale
    holder could then present a fence the restored counter reissues, and a
    fenced write of a new holder would not outrank it. The rows are NOT dropped
    (the next acquire would start at 1); the driver starts a name first acquired
    later above the highest fence of the table. The caller owns the transaction:
    the bump commits with it (the CLI writes its audit row in the same one). A
    database without the table yields ``[]``. Dry-run unless ``apply``; running
    it twice lifts twice.
    """
    if offset < 1:
        raise ValueError("the fence offset must be positive")
    has_table = await session.run_sync(
        lambda sync: inspect(sync.connection()).has_table(_LEASE_TABLE.name)
    )
    if not has_table:
        return []
    table = _LEASE_TABLE
    selected = (
        select(table.c.name, table.c.fence, table.c.expires_at > func.now())
        .order_by(table.c.name)
        .with_for_update()
    )
    rows = (await session.execute(selected)).all()
    if apply and rows:
        await session.execute(
            update(table).values(fence=table.c.fence + offset, expires_at=func.now())
        )
    return [
        FencedLease(
            name=str(name),
            fence_before=int(fence),
            fence_after=int(fence) + offset,
            held=bool(held),
        )
        for name, fence, held in rows
    ]


def database_chains(
    database: str, descriptors: Sequence[ModuleDescriptor]
) -> list[tuple[str, str, str]]:
    """The migration chains ``database`` hosts (core + its modules)."""
    return _hosted_chains(database, descriptors)


__all__ = [
    "CORE_TABLES",
    "LEASE_FENCE_OFFSET",
    "CheckResult",
    "FencedLease",
    "PlanItem",
    "VerifyReport",
    "build_backup_plan",
    "check_restored_database",
    "command_restorer",
    "database_chains",
    "dump_format",
    "lift_fences",
    "format_plan",
    "scratch_database",
    "verify_dump",
]
