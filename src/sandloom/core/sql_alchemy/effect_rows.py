"""A Unit of Work's outbox and audit rows: plain rows, written by the flush.

``publish_event`` / ``record_audit`` hand their rows to the stores, which
register PLAIN row objects here (:class:`OutboxRow` / :class:`AuditRow`) —
not ORM instances: building an ``OutboxMessage`` / ``AuditLogEntry``, the
session's add / flush bookkeeping and the expunge / re-attach after a fused
write cost more than the INSERT's own Python.

A session ``before_flush`` listener writes the registered rows as ONE prebuilt
statement — both tables in one round trip on PostgreSQL::
    WITH audit AS (INSERT INTO audit_log (...) SELECT * FROM unnest(...))
    INSERT INTO outbox_messages (...) SELECT * FROM unnest(...)

(or the single-table form when the flush holds rows of one table only). Each
column is one array parameter, so the statement text (and asyncpg's prepared
statement) is the same for any number of rows. Other dialects get one Core
``INSERT`` per table. Everything the ORM path guaranteed still holds:

* **Written by the flush, at the same moment.** Plain rows do not make the
  session dirty, so registering one also puts the session's private FLUSH
  TRIGGER — a pending instance of an internal mapped class that is never
  written (its table does not exist: were it ever INSERTed the flush would
  fail loudly) — into ``session.new``. The session is then dirty exactly as
  with pending ORM rows: an autoflush before a query, an explicit
  ``flush()``, ``begin_nested()``, a savepoint RELEASE and the commit all run
  the flush, and the listener writes the rows and expunges the trigger. The
  rows are therefore written inside the UoW's transaction (or the savepoint
  then active), before ``COMMIT``, and visible to an autoflushing query in the
  same transaction. A flush limited to explicit ``objects`` leaves them
  pending, like pending ORM rows.
* **Savepoints.** A row carries the innermost transaction active when it was
  registered (the ``_savepoint_token`` rule of ``on_commit``). A row whose
  savepoint rolled back before it was written is dropped at the next flush
  (its transaction is no longer in the active chain) and never dispatched; a
  row written inside a savepoint that later rolls back (or whose ancestor
  does) is flagged by an ``after_soft_rollback`` listener. ``survived()`` is
  true only for a row that was written and not undone — so a row that was
  never written (its trigger was expunged with a rolled-back savepoint, or by
  ``expunge_all()``) is never dispatched either, as an expunged ORM row was
  not.
* **Same values.** Defaults the ORM applied at flush (ids, timestamps,
  ``delivery_attempts``) are applied the same way; JSON goes through the
  dialect's serializer.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import Column, Integer, MetaData, Table, bindparam, event, text
from sqlalchemy.orm import Session, registry

import sandloom.core.sql_alchemy.models.audit as _audit
import sandloom.core.sql_alchemy.models.outbox as _outbox
from sandloom.core.sql_alchemy.models.audit import _utc_naive_now as _audit_now

#: ``session.info`` key: rows registered by the stores, not yet written.
_ROWS_KEY = "sandloom_effect_rows"
#: ``session.info`` key: ``(transaction, row)`` of outbox rows written inside
#: a savepoint, watched for a rollback of their (sub)transaction.
_WRITTEN_KEY = "sandloom_effect_rows_written"
#: ``session.info`` key: the session's transaction listeners are attached.
_WATCHING_KEY = "sandloom_effect_rows_watching"
#: ``session.info`` key: the session's flush trigger instance.
_TRIGGER_KEY = "sandloom_effect_rows_trigger"

#: Row states.
PENDING = 0
WRITTEN = 1
#: Undone: its savepoint rolled back (before or after the write).
ROLLED_BACK = 2

_listening = False
_statements: dict[tuple[bool, bool], Any] = {}
_trigger_class: Any = None


class OutboxRow:
    """One ``outbox_messages`` row of a Unit of Work (not an ORM instance)."""

    __slots__ = (
        "id",
        "channel",
        "schema_version",
        "payload",
        "trace_id",
        "emitted_at",
        "token",
        "state",
    )

    def __init__(
        self,
        id: uuid.UUID,  # the column name
        channel: str,
        schema_version: str,
        payload: Any,
        trace_id: str | None,
        emitted_at: Any,
    ) -> None:
        self.id = id
        self.channel = channel
        self.schema_version = schema_version
        self.payload = payload
        self.trace_id = trace_id
        self.emitted_at = emitted_at
        self.token: Any = None
        self.state = PENDING


class AuditRow:
    """One ``audit_log`` row of a Unit of Work (not an ORM instance)."""

    __slots__ = (
        "action",
        "actor_id",
        "actor_type",
        "resource_type",
        "resource_id",
        "data",
        "request_id",
        "trace_id",
        "token",
        "state",
    )

    def __init__(
        self,
        action: str,
        actor_id: uuid.UUID | None,
        actor_type: str | None,
        resource_type: str | None,
        resource_id: str | None,
        data: Any,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        self.action = action
        self.actor_id = actor_id
        self.actor_type = actor_type
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.data = data
        self.request_id = request_id
        self.trace_id = trace_id
        self.token: Any = None
        self.state = PENDING


_AUDIT_COLUMNS = (
    ("id", "UUID[]"),
    ("occurred_at", "TIMESTAMP[]"),
    ("actor_id", "UUID[]"),
    ("actor_type", "VARCHAR[]"),
    ("action", "VARCHAR[]"),
    ("resource_type", "VARCHAR[]"),
    ("resource_id", "VARCHAR[]"),
    ("data", "JSON[]"),
    ("request_id", "VARCHAR[]"),
    ("trace_id", "VARCHAR[]"),
)
_OUTBOX_COLUMNS = (
    ("id", "UUID[]"),
    ("channel", "VARCHAR[]"),
    ("schema_version", "VARCHAR[]"),
    ("payload", "JSON[]"),
    ("trace_id", "VARCHAR[]"),
    ("emitted_at", "TIMESTAMP[]"),
    ("delivery_attempts", "INTEGER[]"),
)


def _insert_sql(table: str, prefix: str, columns: tuple[tuple[str, str], ...]) -> str:
    return (
        f"INSERT INTO {table} ("  # noqa: S608 - framework table/column constants, values bound
        + ", ".join(name for name, _ in columns)
        + ") SELECT * FROM unnest("
        + ", ".join(f"CAST(:{prefix}_{name} AS {cast})" for name, cast in columns)
        + ")"
    )


def _statement(audit: bool, outbox: bool) -> Any:
    """The prebuilt PostgreSQL INSERT for rows of these tables."""
    key = (audit, outbox)
    stmt = _statements.get(key)
    if stmt is None:
        audit_sql = _insert_sql("audit_log", "a", _AUDIT_COLUMNS)
        outbox_sql = _insert_sql("outbox_messages", "o", _OUTBOX_COLUMNS)
        if audit and outbox:
            sql = f"WITH audit AS ({audit_sql}) {outbox_sql}"
        else:
            sql = audit_sql if audit else outbox_sql
        parts: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        if audit:
            parts.append(("a", _AUDIT_COLUMNS))
        if outbox:
            parts.append(("o", _OUTBOX_COLUMNS))
        binds: list[Any] = [
            bindparam(f"{prefix}_{name}")
            for prefix, columns in parts
            for name, _ in columns
        ]
        stmt = _statements[key] = text(sql).bindparams(*binds)
    return stmt


def _trigger() -> Any:
    """A new flush trigger: a pending instance of an internal mapped class.

    Mapped on its own registry (never part of any application / migration
    metadata) to a table that does not exist.
    """
    global _trigger_class
    cls = _trigger_class
    if cls is None:

        class _EffectRowsFlushTrigger:
            """Never written: the effect-rows listener expunges it."""

        registry(metadata=MetaData()).map_imperatively(
            _EffectRowsFlushTrigger,
            Table(
                "sandloom_effect_rows_flush_trigger_never_written",
                MetaData(),
                Column("id", Integer, primary_key=True),
            ),
        )
        cls = _trigger_class = _EffectRowsFlushTrigger
    return cls()


def register(session: Any, row: OutboxRow | AuditRow) -> None:
    """Queue ``row`` for the next flush of ``session``'s UoW."""
    sync = getattr(session, "sync_session", session)
    info = sync.info
    rows = info.get(_ROWS_KEY)
    if rows is None:
        _ensure_listening()
        rows = info[_ROWS_KEY] = []
    row.token = sync.get_nested_transaction() or sync.get_transaction()
    rows.append(row)
    trigger = info.get(_TRIGGER_KEY)
    if trigger is None:
        trigger = info[_TRIGGER_KEY] = _trigger()
    if trigger not in sync:
        sync.add(trigger)


def survived(row: Any) -> bool:
    """True when ``row`` was written and its write was not undone."""
    state = getattr(row, "state", WRITTEN)
    return bool(state == WRITTEN)


def _ensure_listening() -> None:
    global _listening
    if _listening:
        return

    # Class level: before_flush only. The transaction listeners are attached
    # to the ONE session that wrote rows inside a savepoint
    # (_watch_transactions): a class-level after_* session listener would
    # read, to the execution planner's safety check, as an application policy
    # and force every declared read onto the general executor.
    event.listen(Session, "before_flush", _before_flush)
    _listening = True


def _watch_transactions(session: Any) -> None:
    if session.info.get(_WATCHING_KEY):
        return

    event.listen(session, "after_soft_rollback", _after_soft_rollback)
    event.listen(session, "after_transaction_end", _after_transaction_end)
    session.info[_WATCHING_KEY] = True


def _json(dialect: Any) -> Any:
    return getattr(dialect, "_json_serializer", None) or json.dumps


def _outbox_columns(row: OutboxRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "channel": row.channel,
        "schema_version": row.schema_version,
        "payload": row.payload,
        "trace_id": row.trace_id,
        "emitted_at": row.emitted_at,
        "delivery_attempts": 0,
    }


def _audit_columns(row: AuditRow) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "occurred_at": _audit_now(),
        "actor_id": row.actor_id,
        "actor_type": row.actor_type if row.actor_type is not None else "user",
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "data": row.data,
        "request_id": row.request_id,
        "trace_id": row.trace_id,
    }


def _write_postgresql(
    connection: Any, outbox: list[OutboxRow], audit: list[AuditRow]
) -> None:
    dumps = _json(connection.dialect)
    params: dict[str, Any] = {}
    if audit:
        cols = [_audit_columns(row) for row in audit]
        for name, _ in _AUDIT_COLUMNS:
            values = [col[name] for col in cols]
            if name == "data":
                values = [None if v is None else dumps(v) for v in values]
            params[f"a_{name}"] = values
    if outbox:
        cols = [_outbox_columns(row) for row in outbox]
        for name, _ in _OUTBOX_COLUMNS:
            values = [col[name] for col in cols]
            if name == "payload":
                values = [dumps(v) for v in values]
            params[f"o_{name}"] = values
    connection.execute(_statement(bool(audit), bool(outbox)), params)


def _write_portable(
    connection: Any, outbox: list[OutboxRow], audit: list[AuditRow]
) -> None:
    """One Core INSERT per table (dialects without ``unnest``)."""

    audit_table: Any = _audit.AuditLogEntry.__table__
    outbox_table: Any = _outbox.OutboxMessage.__table__
    if audit:
        connection.execute(audit_table.insert(), [_audit_columns(row) for row in audit])
    if outbox:
        connection.execute(
            outbox_table.insert(), [_outbox_columns(row) for row in outbox]
        )


def _active_chain(session: Any) -> list[Any]:
    chain: list[Any] = []
    txn = session.get_nested_transaction() or session.get_transaction()
    while txn is not None:
        chain.append(txn)
        txn = getattr(txn, "parent", None)
    return chain


def _before_flush(session: Any, _flush_context: Any, instances: Any) -> None:  # noqa: C901
    info = session.info
    trigger = info.get(_TRIGGER_KEY)
    if trigger is None or instances is not None:
        # Not ours, or a flush limited to explicit objects: the rows stay
        # pending (with the trigger), as pending ORM rows would.
        return
    if trigger in session:
        session.expunge(trigger)
    rows = info.get(_ROWS_KEY)
    if not rows:
        return
    info[_ROWS_KEY] = []
    chain = _active_chain(session)
    outbox: list[OutboxRow] = []
    audit: list[AuditRow] = []
    for row in rows:
        token = row.token
        row.token = None  # no session reference left on the row
        if token is not None and not any(token is txn for txn in chain):
            # Registered inside a savepoint that rolled back before it was
            # written: never written, never dispatched.
            row.state = ROLLED_BACK
        elif type(row) is OutboxRow:
            outbox.append(row)
        else:
            audit.append(row)
    if not outbox and not audit:
        return
    connection = session.connection()
    if connection.dialect.name == "postgresql":
        _write_postgresql(connection, outbox, audit)
    else:
        _write_portable(connection, outbox, audit)

    # Only a SAVEPOINT can roll back while the UoW still commits afterwards;
    # rows written in the outermost transaction are undone only together
    # with the whole UoW, which then dispatches nothing. So the transaction
    # listeners (and the bookkeeping) are needed only inside a savepoint.
    nested = session.get_nested_transaction()
    written: list[Any] | None = None
    if nested is not None and outbox:
        _watch_transactions(session)
        written = info.get(_WRITTEN_KEY)
        if written is None:
            written = info[_WRITTEN_KEY] = []
    for audit_row in audit:
        audit_row.state = WRITTEN
    for outbox_row in outbox:
        outbox_row.state = WRITTEN
        if written is not None:
            written.append((nested, outbox_row))


def _after_transaction_end(session: Any, transaction: Any) -> None:
    """Forget the written rows when the outermost transaction ends.

    The (transaction, row) pairs reference the session (a cycle through
    ``session.info``); dropping them at the end keeps a UoW free of cyclic
    garbage. Nothing can roll back afterwards.
    """
    if transaction.parent is None and session.info.get(_WRITTEN_KEY):
        session.info[_WRITTEN_KEY] = []


def _after_soft_rollback(session: Any, previous_transaction: Any) -> None:
    written = session.info.get(_WRITTEN_KEY)
    if not written:
        return
    kept = []
    for txn, row in written:
        undone = False
        probe = txn
        while probe is not None:
            if probe is previous_transaction:
                undone = True
                break
            probe = getattr(probe, "parent", None)
        if undone:
            row.state = ROLLED_BACK
        else:
            kept.append((txn, row))
    session.info[_WRITTEN_KEY] = kept


__all__ = [
    "AuditRow",
    "OutboxRow",
    "register",
    "survived",
]
