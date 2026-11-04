"""Relational durable dedupe record — the one claim implementation (ADR-0005).

:class:`RelationalDedupeLog` claims, completes, replays and purges a durable
dedupe record (:mod:`sandloom.core.effects`) on the table a
:class:`~sandloom.core.effects.DedupeRecordSpec` describes. Its three adapters are
the consumer inbox (:mod:`sandloom.core.sql_alchemy.durable_inbox`), the
contract command log (:mod:`sandloom.core.sql_alchemy.command_log`) and the
durable HTTP idempotency log (:mod:`sandloom.core.sql_alchemy.request_log`).

Claim protocol (one statement on the happy path)::

    INSERT INTO <table> (...) VALUES (...)
    ON CONFLICT (<conflict>) DO NOTHING                  -- no TTL
    ON CONFLICT (<conflict>) DO UPDATE SET ...           -- TTL: take over an
        WHERE <table>.expires_at <= :claim_now           --   EXPIRED row only
    RETURNING <primary key>

* a row comes back -> claimed (new key, or an expired record taken over);
* no row -> the record exists: a replaying record reads it back (now locked
  by the statement) and replays it (same args digest) or reports a mismatch;
  an inbox-style record (no digest) is a duplicate without a read;
* it runs with autoflush DISABLED: only THIS record's conflict reads as a
  duplicate — a flush of the caller's pending objects could otherwise turn
  the caller's own constraint violation into "duplicate -> skip";
* a running claim is never taken over: its insert is uncommitted, so the
  conflict applies whatever its ``expires_at``; a concurrent claim of the
  same key waits for that transaction (unique-index speculative insertion),
  bounded by the connection's ``lock_timeout`` — exceeding it (what the
  database's renderer classifies as ``lock_not_available``) raises
  :class:`~sandloom.core.effects.DedupeBusy` when the spec asks for it; the
  unit then rolls back (PostgreSQL has already aborted the transaction);
* completion restarts the TTL (``expires_at`` += runtime, computed in Python
  from the claim's own values, so on every database), so a command whose
  transaction outlived the TTL never commits an already expired record that
  a retry could take over.

The ``INSERT ... ON CONFLICT`` comes from the SQL dialect renderer
(:func:`~sandloom.core.sql_alchemy.portable.conflict_insert_for`); a database
whose renderer has none claims through a savepoint instead.

Every statement of the hot path is built once per log (every value a bind
parameter), instead of constructing and cache-keying one per call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, and_, bindparam, delete, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError

from sandloom.core.effects import DedupeBusy, DedupeClaim, DedupeRecordSpec
from sandloom.core.sql_alchemy.portable import (
    UnsupportedSqlForm,
    classify_error,
    conflict_insert_for,
)

logger = logging.getLogger(__name__)

#: Bind-parameter prefix of the key columns in WHERE clauses (a bindparam may
#: not share a column's name inside an UPDATE).
_KEY = "k_"

#: Bind parameter of the restarted expiry (completion).
_NEW_EXPIRY = "new_expires_at"
#: ``session.info`` key: (log, key values) -> the TTL of a record this
#: session's transaction claimed (completion restarts it).
_CLAIMED_TTLS = "sandloom.dedupe_claimed_ttls"
#: ``claim_statements`` value for a database without ``ON CONFLICT``.
_NO_UPSERT = object()

_CLAIMED = DedupeClaim("claimed")


def _column(name: str | None) -> str:
    """A column the spec must declare for the feature being used."""
    if name is None:
        raise TypeError("this dedupe record does not declare that column")
    return name


class RelationalDedupeLog:
    """Claim / complete / replay / purge of one durable dedupe-record table.

    ``table`` returns the mapped :class:`~sqlalchemy.Table` (resolved lazily,
    so building a log never touches the ORM registry at import).
    """

    __slots__ = (
        "spec",
        "_table",
        "_resolved",
        "claim_statements",
        "completion_statements",
        "_replay",
        "_release",
        "_amend",
        "_stored_window",
        "_key_params",
    )

    def __init__(self, spec: DedupeRecordSpec, table: Callable[[], Table]) -> None:
        self.spec = spec
        self._table = table
        self._resolved: Table | None = None
        #: dialect -> the prebuilt claim INSERT (``_NO_UPSERT``: the database
        #: has no ``ON CONFLICT``; claims go through a savepoint).
        self.claim_statements: dict[str, Any] = {}
        #: extend-expiry? -> the prebuilt completion UPDATE.
        self.completion_statements: dict[bool, Any] = {}
        self._replay: Any = None
        self._release: Any = None
        self._amend: Any = None
        self._stored_window: Any = None
        self._key_params = tuple(_KEY + name for name in spec.conflict)

    # -- statements -------------------------------------------------------- #

    def table(self) -> Table:
        table = self._resolved
        if table is None:
            table = self._resolved = self._table()
        return table

    def _key_match(self) -> Any:
        table = self.table()
        return and_(
            *(
                table.c[name] == bindparam(param)
                for name, param in zip(
                    self.spec.conflict, self._key_params, strict=True
                )
            )
        )

    def claim_statement(self, dialect: str) -> Any:
        """The prebuilt claim ``INSERT ... ON CONFLICT`` of ``dialect``, or
        ``None`` when its renderer has no ``ON CONFLICT``."""
        stmt = self.claim_statements.get(dialect)
        if stmt is None:
            spec, table = self.spec, self.table()
            try:
                insert = conflict_insert_for(dialect, table)
            except UnsupportedSqlForm:
                self.claim_statements[dialect] = _NO_UPSERT
                return None
            base = insert.values({name: bindparam(name) for name in spec.columns})
            index = [table.c[name] for name in spec.conflict]
            if spec.expires_at is None:
                stmt = base.on_conflict_do_nothing(index_elements=index)
            else:
                takeover = {
                    name: base.excluded[name]
                    for name in spec.columns
                    if name not in spec.conflict
                }
                stmt = base.on_conflict_do_update(
                    index_elements=index,
                    set_=takeover,
                    # take over an EXPIRED record only
                    where=table.c[spec.expires_at] <= bindparam("claim_now"),
                )
            stmt = self.claim_statements[dialect] = stmt.returning(
                table.c[spec.returning]
            )
        return None if stmt is _NO_UPSERT else stmt

    def replay_statement(self) -> Any:
        stmt = self._replay
        if stmt is None:
            spec, table = self.spec, self.table()
            columns = [
                table.c[_column(spec.args_digest)],
                table.c[_column(spec.status)],
                table.c[_column(spec.result)],
            ]
            if spec.effects is not None:
                columns.append(table.c[spec.effects])
            stmt = self._replay = select(*columns).where(self._key_match())
        return stmt

    def prebuilt_replay_statement(self) -> Any:
        """The replay SELECT if a replay already built it, else ``None``."""
        return self._replay

    def completion_statement(self, *, extend_expiry: bool) -> Any:
        stmt = self.completion_statements.get(extend_expiry)
        if stmt is None:
            spec, table = self.spec, self.table()
            values: dict[str, Any] = {
                _column(spec.status): bindparam("status"),
                _column(spec.result): bindparam("result"),
            }
            if spec.effects is not None:
                values[spec.effects] = bindparam("effects")
            if extend_expiry:
                # now + the claim's TTL, which is what the stored
                # ``expires_at + (now - created_at)`` is: computed in Python
                # (datetime arithmetic in SQL is not portable).
                values[_column(spec.expires_at)] = bindparam(_NEW_EXPIRY)
            stmt = self.completion_statements[extend_expiry] = (
                update(table).where(self._key_match()).values(values)
            )
        return stmt

    def _key_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            param: values[name]
            for name, param in zip(self.spec.conflict, self._key_params, strict=True)
        }

    # -- claim -------------------------------------------------------------- #

    async def claim(
        self, session: Any, values: Mapping[str, Any], *, now: datetime | None = None
    ) -> DedupeClaim:
        """Claim the record ``values`` describes in ``session``'s transaction.

        ``values`` holds every column of ``spec.columns``; ``now`` is the
        expiry cutoff of a TTL record (an expired record is taken over).
        """
        spec = self.spec
        dialect = session.sync_session.get_bind().dialect.name
        try:
            with session.no_autoflush:
                stmt = self.claim_statements.get(dialect)
                if stmt is None:
                    stmt = self.claim_statement(dialect)
                if stmt is not None and stmt is not _NO_UPSERT:
                    params = values if now is None else {**values, "claim_now": now}
                    result = await session.execute(stmt, params)
                    claimed = result.first() is not None
                else:  # pragma: no cover - no ON CONFLICT: savepoint fallback
                    claimed = await self._generic_claim(session, values, now)
                if claimed:
                    if spec.expires_at is not None and spec.created_at is not None:
                        self._remember_ttl(session, values)
                    return _CLAIMED
                if not spec.replays:
                    return DedupeClaim("replay", status=spec.replay_status)
                row = (
                    await session.execute(
                        self.replay_statement(), self._key_values(values)
                    )
                ).first()
        except DBAPIError as exc:
            if (
                spec.busy_on_lock_timeout
                and classify_error(dialect, exc) == "lock_not_available"
            ):
                raise DedupeBusy(str(values.get(spec.conflict[0]))) from exc
            raise
        if row is None:  # pragma: no cover - the conflicting row is locked
            raise DedupeBusy(str(values.get(spec.conflict[0])))
        if row[0] != values.get(cast(str, spec.args_digest)):
            return DedupeClaim("mismatch")
        return DedupeClaim(
            "replay",
            status=row[1],
            result=row[2],
            effects=row[3] if spec.effects is not None else None,
        )

    def _remember_ttl(self, session: Any, values: Mapping[str, Any]) -> None:
        """Keep the claimed record's TTL for :meth:`complete` (same
        transaction; a rolled-back claim leaves an entry nobody reads)."""
        spec = self.spec
        info = session.info
        ttls = info.get(_CLAIMED_TTLS)
        if ttls is None:
            ttls = info[_CLAIMED_TTLS] = {}
        ttls[(self, *map(values.__getitem__, spec.conflict))] = (
            values[cast(str, spec.expires_at)] - values[cast(str, spec.created_at)]
        )

    def _claimed_ttl(self, session: Any, key: Mapping[str, Any]) -> timedelta | None:
        ttls = session.info.get(_CLAIMED_TTLS)
        if not ttls:
            return None
        found: timedelta | None = ttls.pop(
            (self, *map(key.__getitem__, self.spec.conflict)), None
        )
        return found

    async def _generic_claim(  # pragma: no cover - databases without ON CONFLICT
        self, session: Any, values: Mapping[str, Any], now: datetime | None
    ) -> bool:
        """Dialects without ``ON CONFLICT``: drop an expired record, then insert
        the claim in a savepoint; only a violation of THIS record's constraint
        (when the spec names it) reads as a conflict."""
        spec, table = self.spec, self.table()
        if spec.expires_at is not None and now is not None:
            await session.execute(
                delete(table).where(self._key_match(), table.c[spec.expires_at] <= now),
                self._key_values(values),
            )
        try:
            async with session.begin_nested():
                await session.execute(insert(table).values(**values))
        except IntegrityError as exc:
            if spec.constraint is None or spec.constraint in str(
                getattr(exc, "orig", exc)
            ):
                return False
            raise
        return True

    # -- complete ----------------------------------------------------------- #

    async def complete(
        self,
        session: Any,
        key: Mapping[str, Any],
        *,
        status: str,
        result: str | None,
        now: datetime | None = None,
        effects: str | None = None,
    ) -> None:
        """Store the outcome on the claimed record (same transaction).

        ``key`` holds the conflict columns. A TTL record this session
        claimed restarts its expiry at ``now`` (see the module doc). ``effects``: what
        the operation left on its caller (a record declaring the column)."""
        params: dict[str, Any] = {
            **self._key_values(key),
            "status": status,
            "result": result,
        }
        if self.spec.effects is not None:
            params["effects"] = effects
        ttl = None
        if self.spec.expires_at is not None and now is not None:
            ttl = self._claimed_ttl(session, key)
            if ttl is None:
                ttl = await self._stored_ttl(session, key)
        extend = ttl is not None
        if ttl is not None and now is not None:
            params[_NEW_EXPIRY] = now + ttl
        with session.no_autoflush:
            await session.execute(
                self.completion_statement(extend_expiry=extend), params
            )

    async def _stored_ttl(
        self, session: Any, key: Mapping[str, Any]
    ) -> timedelta | None:
        """The TTL of a record this session did not claim (a completion out
        of the expected order), read back from the row: one more round trip,
        and a warning, since every adapter claims and completes in one
        session. ``None`` (the expiry stays) when the row or its
        ``created_at`` cannot be read."""
        spec = self.spec
        logger.warning(
            "dedupe record %s completed in a session that did not claim it: "
            "its expiry is restarted from the stored row",
            spec.table,
        )
        if spec.created_at is None:
            return None
        stmt = self._stored_window
        if stmt is None:
            table = self.table()
            stmt = self._stored_window = select(
                table.c[cast(str, spec.expires_at)], table.c[spec.created_at]
            ).where(self._key_match())
        with session.no_autoflush:
            row = (await session.execute(stmt, self._key_values(key))).first()
        if row is None or row[0] is None or row[1] is None:
            return None
        ttl: timedelta = row[0] - row[1]
        return ttl

    async def amend_effects(
        self, session: Any, key: Mapping[str, Any], effects: str | None
    ) -> None:
        """Replace the effects of the record ``key`` names (a record
        declaring the column), in ``session``'s transaction."""
        stmt = self._amend
        if stmt is None:
            column = cast(str, self.spec.effects)
            stmt = self._amend = (
                update(self.table())
                .where(self._key_match())
                .values({column: bindparam("effects")})
            )
        with session.no_autoflush:
            await session.execute(stmt, {**self._key_values(key), "effects": effects})

    # -- release ------------------------------------------------------------ #

    async def release(self, session: Any, key: Mapping[str, Any]) -> None:
        """Delete the record ``key`` names in ``session``'s transaction.

        For a claim whose outcome must not be replayed although its
        transaction commits (the HTTP adapter: a ``5xx`` the endpoint
        returned); a rolled-back claim needs no release.
        """
        if self.spec.expires_at is not None:
            self._claimed_ttl(session, key)  # nothing left to complete
        stmt = self._release
        if stmt is None:
            stmt = self._release = delete(self.table()).where(self._key_match())
        with session.no_autoflush:
            await session.execute(stmt, self._key_values(key))

    # -- purge -------------------------------------------------------------- #

    async def purge(
        self,
        session: Any,
        cutoff: datetime,
        *,
        limit: int | None = None,
        inclusive: bool = False,
    ) -> int:
        """Delete records whose ``purge_column`` is before ``cutoff``.

        ``inclusive`` makes it ``<=`` (a TTL record expiring at ``cutoff`` is
        due). ``limit`` bounds one call (``pk IN (SELECT … LIMIT n)``, served
        by the purge column's index); ``None`` deletes every match. The cutoff
        is re-checked on the outer ``DELETE``: the victim subquery reads its
        snapshot, so a record a concurrent claim is taking over (row locked,
        new ``expires_at``) would otherwise be deleted once that claim commits
        — PostgreSQL re-evaluates only the outer ``WHERE`` against the new row
        version — and the operation's retry would run again. Runs in the
        caller's transaction.
        """
        if limit is not None and limit <= 0:
            return 0
        spec, table = self.spec, self.table()
        column = table.c[spec.purge_column]
        due = column <= cutoff if inclusive else column < cutoff
        criterion = due
        if limit is not None:
            victims = (
                select(table.c[spec.returning])
                .where(due)
                .limit(limit)
                .scalar_subquery()
            )
            criterion = and_(table.c[spec.returning].in_(victims), due)
        res = cast(
            "CursorResult[Any]", await session.execute(delete(table).where(criterion))
        )
        return int(getattr(res, "rowcount", 0) or 0)


__all__ = ["RelationalDedupeLog"]
