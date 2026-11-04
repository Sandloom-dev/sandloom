"""Conformance kits: subclass one in your driver's tests, provide ``make_provider``.

Each obligation is its own test, so a failure names what to fix. Requires pytest
and pytest-asyncio.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, ClassVar, cast, get_args

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable

from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.contracts import NotificationChannel
from sandloom.core.infra.registry import versions_compatible
from sandloom.core.sql_alchemy.portable import (
    RENDERER_FEATURES,
    UtcDateTime,
    WallClock,
    sql_dialect,
)
from sandloom.driverkit import (
    ConflictInsert,
    HealthResult,
    Notification,
    NotifierProvider,
    SqlDialect,
)

_MARKER = "sandloom-conformance-marker"


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(what)


def _advertised_channels(provider: NotifierProvider) -> list[NotificationChannel]:
    channels = provider.features() & set(get_args(NotificationChannel))
    return [cast("NotificationChannel", channel) for channel in sorted(channels)]


def _message(
    channel: NotificationChannel, recipients: Mapping[str, str]
) -> Notification:
    return Notification(
        channel=channel,
        recipient=recipients[channel],
        subject="Sandloom driver conformance",
        body=f"body {_MARKER}",
        body_fields={"link": _MARKER},
    )


@pytest.mark.asyncio
class NotifierConformance:
    """What a ``notification`` driver must do. It sends real messages: point
    ``make_provider`` at a test backend. Logs are read through the root logger, so
    a logger with ``propagate=False`` is not checked.
    """

    #: Where each channel sends. Override for your test backend.
    recipients: ClassVar[Mapping[str, str]] = {
        "email": "driver-test@example.com",
        "sms": "+15550100000",
        "push": "driver-test-device",
        "inapp": "driver-test-user",
    }

    @pytest.fixture
    def make_provider(self) -> Callable[[], NotifierProvider]:
        raise NotImplementedError("override make_provider to build your driver")

    @pytest_asyncio.fixture
    async def provider(
        self, make_provider: Callable[[], NotifierProvider]
    ) -> AsyncIterator[NotifierProvider]:
        built = make_provider()
        await built.aopen()
        yield built
        await built.aclose()

    async def test_implements_the_contract(self, provider: NotifierProvider) -> None:
        _require(
            isinstance(provider, NotifierProvider),
            f"{type(provider).__name__} must subclass NotifierProvider",
        )

    async def test_states_the_contract_version(
        self, provider: NotifierProvider
    ) -> None:
        version = getattr(type(provider), "contract_version", None)
        _require(
            isinstance(version, str)
            and versions_compatible(NOTIFICATION_CAPABILITY.version, version),
            f"contract_version must match {NOTIFICATION_CAPABILITY.version!r}, got "
            f"{version!r}",
        )

    async def test_advertises_only_defined_features(
        self, provider: NotifierProvider
    ) -> None:
        unknown = provider.features() - NOTIFICATION_CAPABILITY.features
        _require(not unknown, f"advertises undefined feature(s): {sorted(unknown)}")

    async def test_reports_the_features_it_declares(
        self, provider: NotifierProvider
    ) -> None:
        declared = type(provider).features_for(provider.options)
        _require(
            provider.features() == declared,
            "declare features in supported_features or features_for; do not "
            "override features()",
        )

    async def test_sends_on_each_advertised_channel(
        self, provider: NotifierProvider
    ) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        for channel in channels:
            await provider.send(_message(channel, self.recipients))

    async def test_keeps_the_body_out_of_the_logs(
        self, provider: NotifierProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        channels = _advertised_channels(provider)
        if not channels:
            pytest.skip("this driver advertises no channel: nothing was sent")
        with caplog.at_level(logging.DEBUG):
            for channel in channels:
                await provider.send(_message(channel, self.recipients))
        logged = [f"{r.getMessage()} {r.__dict__}" for r in caplog.records]
        _require(
            not any(_MARKER in line for line in logged),
            "a body or body_fields value was logged; these can hold secret links",
        )

    async def test_reports_health(self, provider: NotifierProvider) -> None:
        _require(
            isinstance(await provider.health(), HealthResult),
            "health() must return a HealthResult",
        )

    async def test_closes_more_than_once(self, provider: NotifierProvider) -> None:
        await provider.aclose()
        await provider.aclose()


class SqlDialectConformance:
    """What a SQL dialect renderer must do for the portable forms of
    ``sandloom.sdk.data``. Subclass it in the renderer's tests and set
    ``renderer`` and ``dialect`` (the SQLAlchemy dialect the renderer is named
    for). The checks compile SQL only; a renderer that writes a form may still
    need a test against its real database. A form the renderer returns ``None``
    for is checked to be refused, not run.
    """

    #: The renderer under test.
    renderer: ClassVar[SqlDialect]
    #: An instance of the SQLAlchemy dialect it renders for.
    dialect: ClassVar[Any]

    @staticmethod
    def _table() -> sa.Table:
        return sa.Table(
            "conformance_items",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column("key", sa.String(40), nullable=False),
            sa.Column("value", sa.Integer, nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
        )

    def _sql(self, element: Any) -> str:
        return " ".join(str(element.compile(dialect=self.dialect)).split())

    def test_states_contract_version_2_and_its_dialect(self) -> None:
        _require(
            getattr(self.renderer, "contract_version", 1) == 2,
            "contract_version must be 2",
        )
        _require(
            getattr(self.renderer, "name", None) == self.dialect.name,
            f"name must be the SQLAlchemy dialect name {self.dialect.name!r}",
        )

    def test_declares_only_database_renderer_features(self) -> None:
        declared = self.renderer.features()
        unknown = set(declared) - RENDERER_FEATURES
        _require(not unknown, f"features outside the capability: {sorted(unknown)}")

    def test_classifies_an_unknown_error_as_other(self) -> None:
        _require(
            self.renderer.classify_error(RuntimeError("not a database error"))
            == "other",
            "classify_error must answer 'other' for an error it does not know",
        )

    def test_case_insensitive_text_is_a_type_of_this_database(self) -> None:
        type_ = self.renderer.case_insensitive_text(80)
        if type_ is None:
            raise AssertionError("case_insensitive_text returned None")
        column = sa.Column("email", type_)
        sa.Table("conformance_people", sa.MetaData(), column)
        _require(
            bool(str(type_.compile(dialect=self.dialect))),
            "the case-insensitive type does not compile on its database",
        )

    def test_partial_index_options_restrict_the_index(self) -> None:
        condition = sa.text("state = 'live'")
        options = self.renderer.partial_index_options(condition)
        if options is None:
            return  # refused: startup and migrations check it (R015)
        _require(
            all(name.startswith(f"{self.dialect.name}_") for name in options),
            f"partial_index_options must be {self.dialect.name}_* options: {options}",
        )
        index = sa.Index("ix_conformance_live", "key", unique=True, **options)
        self._table().append_constraint(index)
        sql = self._sql(CreateIndex(index))
        _require("state = 'live'" in sql, f"the condition is missing from: {sql}")

    def test_search_index_is_whole_or_absent(self) -> None:
        options = self.renderer.search_index_options()
        operand = self.renderer.search_operand("key", False)
        folded = self.renderer.search_operand("key", True)
        if options is None:
            _require(
                operand is None and folded is None,
                "search_operand must be None when search_index_options is",
            )
            return
        _require(
            bool(operand) and bool(folded),
            "a renderer with a search index must write both operand forms",
        )

    def test_conflict_insert_has_the_on_conflict_shape(self) -> None:
        table = self._table()
        statement = self.renderer.conflict_insert(table)
        if statement is None:
            return  # refused at the call (R015)
        for name in ("on_conflict_do_nothing", "on_conflict_do_update", "excluded"):
            _require(hasattr(statement, name), f"conflict_insert lacks .{name}")
        ignore = statement.values(id=1, key="k", value=1, state="s")
        ignore = ignore.on_conflict_do_nothing(index_elements=["id"])
        _require(
            "ON CONFLICT" in self._sql(ignore).upper()
            or "MERGE" in self._sql(ignore).upper(),
            f"on_conflict_do_nothing wrote no conflict clause: {self._sql(ignore)}",
        )
        upsert = cast(ConflictInsert, self.renderer.conflict_insert(table))
        upsert = upsert.values(id=1, key="k", value=1, state="s")
        upsert = upsert.on_conflict_do_update(
            index_elements=["id"],
            set_={"value": table.c.value + upsert.excluded.value},
        )
        _require(bool(self._sql(upsert)), "on_conflict_do_update does not compile")

    def test_greatest_writes_one_expression_of_its_arguments(self) -> None:
        table = self._table()
        element = self.renderer.greatest(table.c.value, sa.literal(3))
        if element is None:
            raise AssertionError("greatest returned None")
        sql = self._sql(sa.select(element))
        _require(
            "conformance_items.value" in sql,
            f"greatest dropped an argument: {sql}",
        )

    def test_id_in_binds_the_ids_under_its_key(self) -> None:
        table = self._table()
        element = self.renderer.id_in(table.c.id, [1, 2], key="conformance_ids")
        if element is None:
            raise AssertionError("id_in returned None")
        compiled = sa.select(table.c.id).where(element).compile(dialect=self.dialect)
        _require(
            list(compiled.params.get("conformance_ids") or ()) == [1, 2],
            "id_in must bind the ids as one parameter named by key= "
            f"(a prebuilt statement rebinds it): {compiled.params}",
        )

    def test_array_insert_binds_every_column_or_is_absent(self) -> None:
        inserts: list[Any] = [
            ("conformance_a", "a", (("id", "uuid"), ("at", "timestamp"))),
            ("conformance_b", "b", (("n", "integer"), ("doc", "json"))),
        ]
        sql = self.renderer.array_insert_sql(inserts)
        if sql is None:
            return  # the kernel writes row by row
        for table, prefix, columns in inserts:
            _require(table in sql, f"array_insert_sql lacks {table}: {sql}")
            for column, _ in columns:
                _require(
                    f":{prefix}_{column}" in sql,
                    f"array_insert_sql does not bind :{prefix}_{column}: {sql}",
                )

    def test_for_update_keeps_the_query_and_skips_only_with_the_feature(
        self,
    ) -> None:
        table = self._table()
        stmt = sa.select(table.c.id).where(table.c.state == "due")
        base = self._sql(stmt)
        rendered: dict[tuple[bool, bool], str] = {}
        for skip_locked in (True, False):
            for read in (True, False):
                what = f"for_update(skip_locked={skip_locked}, read={read})"
                locked = self.renderer.for_update(
                    stmt, skip_locked=skip_locked, read=read
                )
                _require(isinstance(locked, sa.Select), f"{what} must return a Select")
                sql = self._sql(locked)
                _require(sql.startswith(base), f"{what} changed the query: {sql}")
                rendered[(skip_locked, read)] = sql
        if "skip_locked" in self.renderer.features():
            _require(
                rendered[(True, False)] != rendered[(False, False)],
                "a renderer that declares skip_locked must write it in "
                "for_update(skip_locked=True)",
            )

    def test_relaxed_commit_clause_is_a_where_term_or_absent(self) -> None:
        clause = self.renderer.relaxed_commit_clause()
        if clause is None:
            return  # the kernel commits as usual
        table = self._table()
        sql = self._sql(sa.update(table).where(table.c.id == 1, clause))
        _require(bool(sql), "relaxed_commit_clause does not compile in a WHERE")

    def test_wall_clock_is_a_default_and_an_update_value(self) -> None:
        sql = self.renderer.wall_clock()
        _require(
            isinstance(sql, str) and bool(sql.strip()),
            "wall_clock must return the SQL of the time of the call",
        )
        table = sa.Table(
            "conformance_stamped",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column(
                "at",
                sa.DateTime(timezone=True),
                server_default=sa.text(sql),
                nullable=False,
            ),
        )
        ddl = self._sql(CreateTable(table))
        _require(sql in ddl, f"wall_clock is not the column's default: {ddl}")
        update = self._sql(sa.update(table).values(at=sa.text(sql)))
        _require(sql in update, f"wall_clock is not an update value: {update}")
        if sql_dialect(self.dialect.name) is not self.renderer:
            return  # WallClock() compiles through the registered renderer
        stamped = sa.Table(
            "conformance_wall_clock",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column(
                "at",
                UtcDateTime(),
                server_default=WallClock(),
                onupdate=WallClock(),
                nullable=False,
            ),
            sa.Column("n", sa.Integer),
        )
        ddl = self._sql(CreateTable(stamped))
        _require(sql in ddl, f"WallClock() is not written as wall_clock: {ddl}")
        update = self._sql(sa.update(stamped).values(n=1))
        _require(sql in update, f"WallClock() is not the update value: {update}")

    #: One key of each shape a lock takes (``portable.LockKey``).
    LOCK_KEYS: ClassVar[tuple[Any, ...]] = (
        42,
        0x414A_4152_4241_4331,
        (0x414A4148, -7),
        "sandloom.conformance:lock",
        (0x414A4150, "conformance"),
    )

    def _lock_answers_a_boolean(self, statement: Any, what: str) -> None:
        columns = list(getattr(statement, "selected_columns", ()))
        _require(
            len(columns) == 1 and isinstance(columns[0].type, sa.Boolean),
            f"{what} must select one boolean column (whether it locked), "
            f"selects {columns}",
        )

    def test_lock_statement_compiles_or_is_absent(self) -> None:
        for key in self.LOCK_KEYS:
            for scope in ("transaction", "session"):
                for wait in (True, False):
                    what = f"lock_statement({key!r}, scope={scope!r}, wait={wait})"
                    statement = self.renderer.lock_statement(
                        key, scope=scope, wait=wait
                    )
                    if statement is None:
                        continue  # the kernel refuses rather than run unlocked
                    _require(bool(self._sql(statement)), f"{what} compiles to nothing")
                    if not wait:
                        self._lock_answers_a_boolean(statement, what)

    def test_a_session_lock_has_its_unlock(self) -> None:
        for key in self.LOCK_KEYS:
            locks = self.renderer.lock_statement(key, scope="session", wait=True)
            unlock = self.renderer.unlock_statement(key)
            _require(
                (locks is None) == (unlock is None),
                f"unlock_statement({key!r}) must exist exactly when a session "
                "lock does",
            )
            if unlock is not None:
                _require(bool(self._sql(unlock)), "unlock_statement compiles")
                self._lock_answers_a_boolean(unlock, "unlock_statement")

    def test_lock_statement_refuses_a_key_of_no_shape(self) -> None:
        keys: tuple[Any, ...] = (1.5, True, (1, 2, 3), (1 << 40, 2), None)
        for key in keys:
            try:
                self.renderer.lock_statement(key, scope="transaction", wait=True)
            except TypeError:
                continue
            raise AssertionError(f"lock_statement accepted the key {key!r}")


__all__ = ["NotifierConformance", "SqlDialectConformance"]
