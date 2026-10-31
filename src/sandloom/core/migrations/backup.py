"""Backup plan and restore verification (``sandloom backup plan|verify``, H-20).

* :func:`build_backup_plan` — what must be backed up, per configured database
  instance and per Redis endpoint (security cache, cache, broker, event bus),
  and how: ``pg_dump`` + PITR for PostgreSQL, AOF/RDB for Redis, and what a
  restore of each implies (``sandloom backup reconcile-security`` for the security
  cache).
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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg2  # type: ignore[import-untyped]
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg2 import sql

from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.infra.config import load_infra_config
from sandloom.core.migrations.migrations import (
    _mask_url,
    core_chain_dir,
    version_table_for,
)
from sandloom.core.migrations.urls import to_sync_url

#: Shared infra tables of the core chain every hosting database carries.
CORE_TABLES = (
    "outbox_messages",
    "audit_log",
    "consumer_inbox",
    "contract_command_log",
    "http_idempotency_log",
    "projection_watermarks",
)
#: Version table of the core chain.
CORE_VERSION_TABLE = "alembic_version"
#: Prefix of the scratch databases :func:`verify_dump` creates (and drops).
SCRATCH_PREFIX = "sandloom_restore_verify_"


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PlanItem:
    kind: str  # "postgres" | "redis" | "objectstore" | "secrets"
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
        ],
    )


def _redis_roles(config: Any, settings: Any) -> dict[str, list[str]]:
    """``{redis url: [role, ...]}`` across cache / eventbus / taskqueue."""
    security = getattr(settings, "security_cache_instance", None) or "default"
    idempotency = getattr(settings, "idempotency_cache_instance", None) or security
    roles: dict[str, list[str]] = {}
    for name, inst in sorted((config.get("cache") or {}).items()):
        url = inst.options.get("url")
        if not isinstance(url, str) or not url:
            continue
        role = f"cache:{name}"
        if name == security:
            role += (
                " [SECURITY: revocation marks, user blocks, idempotency]"
                if idempotency == security
                else " [SECURITY: revocation marks, user blocks]"
            )
        elif name == idempotency:
            role += " [IDEMPOTENCY: guard / task-enqueue dedupe records]"
        roles.setdefault(url, []).append(role)
    for name, inst in sorted((config.get("eventbus") or {}).items()):
        if inst.driver not in ("streams", "redis"):
            continue
        url = inst.options.get("url") or settings.cache_url
        roles.setdefault(url, []).append(f"eventbus:{name} ({inst.driver})")
    for name, inst in sorted((config.get("taskqueue") or {}).items()):
        broker = inst.options.get("broker")
        if isinstance(broker, str) and broker.startswith(("redis://", "rediss://")):
            roles.setdefault(broker, []).append(f"taskqueue:{name} broker")
    return roles


def _redis_item(url: str, roles: list[str]) -> PlanItem:
    security = any("SECURITY" in r for r in roles)
    streams = any(r.startswith("eventbus:") and "streams" in r for r in roles)
    backup = [
        "appendonly yes + appendfsync everysec (AOF) AND periodic RDB "
        "snapshots (save / BGSAVE); copy dump.rdb + the appendonly dir off-host",
    ]
    restore: list[str] = []
    if security:
        backup.insert(
            0,
            "maxmemory-policy noeviction (an evicted block mark = an unblocked "
            "user); a SEPARATE instance from the best-effort cache in prod",
        )
        restore += [
            "a snapshot older than a later block / revocation LIFTS it silently: "
            "run sandloom backup reconcile-security --apply before admitting traffic",
            "if the snapshot age is unknown and the outbox no longer covers the "
            "access-token lifetime: sandloom security revoke-sessions --all (or rotate "
            "the JWT signing key)",
        ]
    if streams:
        restore += [
            "events delivered to the stream after the snapshot are lost from it "
            "(the outbox already marked them delivered): rebuild projections "
            "(sandloom projection rebuild) and replay affected consumers by hand",
            "consumer-group offsets come back older: consumers re-read and dedupe",
        ]
    if any(r.startswith("taskqueue:") for r in roles):
        restore.append(
            "queued Celery tasks newer than the snapshot are lost and older ones "
            "may run again: task effects need DB-atomic idempotency; re-enqueue "
            "what is missing (docs/operations/operator-reference.md: "
            "uncertain task enqueues)"
        )
    if not security and not streams and all(r.startswith("cache:") for r in roles):
        restore.append(
            "losing it is tolerable: rate-limit windows reset, cache-backed "
            "idempotency replays are forgotten (DB-atomic dedupe is unaffected)"
        )
    return PlanItem(
        kind="redis",
        name=", ".join(r.split(" ")[0] for r in roles),
        target=_mask_url(url),
        roles=roles,
        criticality="critical" if security else ("required" if streams else "optional"),
        backup=backup,
        restore=restore,
    )


def build_backup_plan(
    settings: Any, descriptors: Sequence[ModuleDescriptor] = ()
) -> list[PlanItem]:
    """Every backup the deployment configured by ``settings`` needs."""
    config = load_infra_config(settings)
    items: list[PlanItem] = []
    for name, inst in sorted((config.get("database") or {}).items()):
        url = inst.options.get("url")
        if isinstance(url, str) and url:
            items.append(_postgres_item(name, url, descriptors))
    for url, roles in _redis_roles(config, settings).items():
        items.append(_redis_item(url, roles))
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
    items.append(
        PlanItem(
            kind="secrets",
            name="keys",
            target="secret manager",
            criticality="critical",
            backup=[
                "JWT_SECRET / JWT_PRIVATE_KEY + JWT_PUBLIC_KEYS, "
                "TWOFA_ENCRYPTION_KEY (every key still in the list), RPC keys, "
                "INTERNAL_API_TOKEN, STRIPE_* — in the secret manager, NEVER "
                "next to the database dumps (dump + 2FA key = readable TOTP "
                "secrets)",
            ],
            restore=[
                "losing TWOFA_ENCRYPTION_KEY makes every 2FA secret "
                "undecryptable: users must re-enrol (operator reset-2fa)",
            ],
        )
    )
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
    admin = psycopg2.connect(_libpq_url(_with_database(server_url, "postgres")))
    admin.autocommit = True
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


def _rows(conn: Any, statement: str | sql.Composable, params: Any = None) -> list[Any]:
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
    conn = psycopg2.connect(_libpq_url(url))
    conn.autocommit = True
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


def database_chains(
    database: str, descriptors: Sequence[ModuleDescriptor]
) -> list[tuple[str, str, str]]:
    """The migration chains ``database`` hosts (core + its modules)."""
    return _hosted_chains(database, descriptors)


__all__ = [
    "CORE_TABLES",
    "CheckResult",
    "PlanItem",
    "VerifyReport",
    "build_backup_plan",
    "check_restored_database",
    "command_restorer",
    "database_chains",
    "dump_format",
    "format_plan",
    "scratch_database",
    "verify_dump",
]
