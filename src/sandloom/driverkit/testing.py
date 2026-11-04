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
from sqlalchemy.schema import CreateIndex

from sandloom.core.infra.capabilities import NOTIFICATION_CAPABILITY
from sandloom.core.infra.contracts import NotificationChannel
from sandloom.core.infra.registry import versions_compatible
from sandloom.core.sql_alchemy.portable import RENDERER_FEATURES
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

    @pytest.mark.asyncio
    async def test_transaction_lock_answers_whether_it_locked(self) -> None:
        statements: list[str] = []
        dialect = self.dialect

        class Session:
            def get_bind(self) -> Any:
                return type("Bind", (), {"dialect": dialect})()

            async def execute(self, statement: Any) -> None:
                statements.append(str(statement.compile(dialect=dialect)))

        locked = await self.renderer.transaction_lock(cast(Any, Session()), 42)
        _require(isinstance(locked, bool), "transaction_lock must return a bool")
        if not locked:
            _require(statements == [], "a refused lock must run no statement")


__all__ = ["NotifierConformance", "SqlDialectConformance"]
