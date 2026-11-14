"""``SqlDialectConformance``: what a SQL dialect renderer must do.

A renderer is not a provider (it has no lifecycle, health or isolation
identity), so this kit runs the harness's declaration checks only: the integer
``contract_version`` and features within the capability's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable

from sandloom.core.sql_alchemy.portable import (
    RENDERER_FEATURES,
    ROW_SECURITY_MEMBERS,
    SQL_DIALECT_CONTRACT,
    TENANT_SETTING,
    ConflictInsert,
    LockStatement,
    SqlDialect,
    UtcDateTime,
    WallClock,
    sql_dialect,
)
from sandloom.driverkit.kits._harness import check_contract_version, require

#: The kit's JSON document for :meth:`SqlDialectConformance.read_json_paths`:
#: JSON text whose numbers must read back as written (risk fix 3).
JSON_PATH_DOCUMENT = (
    '{"s": "FR", "t": true, "f": false, "z": null, "n": 42, "one": 1.0, '
    '"frac": 1.50, "exp": 1e2, "expu": 1E+2, "mz": -0.0, "tenth": 0.1, '
    '"huge": 12345678901234567890123, "below": -9223372036854775809, '
    '"pi": 3.141592653589793238, "small": 1.230e-5, "sn": "1.5", '
    '"a": ["x", [2e1, {"k": -0.0}]], "o": {"0": 7}}'
)
#: What ``json_path`` reads at each path of :data:`JSON_PATH_DOCUMENT` on
#: every database: the scalar's JSON text as stored, a string unquoted,
#: ``None`` for JSON null, a missing path and an index step on an object.
JSON_PATH_READS: tuple[tuple[tuple[str | int, ...], str | None], ...] = (
    (("s",), "FR"),
    (("t",), "true"),
    (("f",), "false"),
    (("z",), None),
    (("n",), "42"),
    (("one",), "1.0"),
    (("frac",), "1.50"),
    (("exp",), "1e2"),
    (("expu",), "1E+2"),
    (("mz",), "-0.0"),
    (("tenth",), "0.1"),
    (("huge",), "12345678901234567890123"),
    (("below",), "-9223372036854775809"),
    (("pi",), "3.141592653589793238"),
    (("small",), "1.230e-5"),
    (("sn",), "1.5"),
    (("a", 0), "x"),
    (("a", 1, 0), "2e1"),
    (("a", 1, 1, "k"), "-0.0"),
    (("o", 0), None),
    (("o", "0"), "7"),
    (("missing",), None),
)


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
    #: True only when every read-only transaction of the database reads one
    #: snapshot by design (SQLite's deferred read under WAL), so a unit asking
    #: for a fresh snapshot per statement may begin like a snapshot unit.
    read_units_share_one_snapshot: ClassVar[bool] = False

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
        check_contract_version(
            getattr(self.renderer, "contract_version", None), SQL_DIALECT_CONTRACT
        )
        require(
            getattr(self.renderer, "name", None) == self.dialect.name,
            f"name must be the SQLAlchemy dialect name {self.dialect.name!r}",
        )

    def test_declares_only_database_renderer_features(self) -> None:
        declared = self.renderer.features()
        unknown = set(declared) - RENDERER_FEATURES
        require(not unknown, f"features outside the capability: {sorted(unknown)}")
        require(
            "concurrent_writers" not in declared,
            "concurrent_writers is derived from serial_writers: do not declare it",
        )

    def test_classifies_an_unknown_error_as_other(self) -> None:
        require(
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
        require(
            bool(str(type_.compile(dialect=self.dialect))),
            "the case-insensitive type does not compile on its database",
        )

    def test_partial_index_options_restrict_the_index(self) -> None:
        condition = sa.text("state = 'live'")
        options = self.renderer.partial_index_options(condition)
        if options is None:
            return  # refused: startup and migrations check it (R015)
        require(
            all(name.startswith(f"{self.dialect.name}_") for name in options),
            f"partial_index_options must be {self.dialect.name}_* options: {options}",
        )
        index = sa.Index("ix_conformance_live", "key", unique=True, **options)
        self._table().append_constraint(index)
        sql = self._sql(CreateIndex(index))
        require("state = 'live'" in sql, f"the condition is missing from: {sql}")

    def test_search_index_is_whole_or_absent(self) -> None:
        options = self.renderer.search_index_options()
        operand = self.renderer.search_operand("key", False)
        folded = self.renderer.search_operand("key", True)
        if options is None:
            require(
                operand is None and folded is None,
                "search_operand must be None when search_index_options is",
            )
            return
        require(
            bool(operand) and bool(folded),
            "a renderer with a search index must write both operand forms",
        )

    def test_prefix_members_are_whole_or_absent(self) -> None:
        """``prefix_pattern`` / ``prefix_match`` (optional, contract 2): both
        or neither; the match compiles for the dialect and the pattern of a
        prefix ends in a wildcard that is not the prefix's own character."""
        pattern = getattr(self.renderer, "prefix_pattern", None)
        match = getattr(self.renderer, "prefix_match", None)
        require(
            callable(pattern) == callable(match),
            "prefix_pattern and prefix_match come together",
        )
        if not callable(pattern) or not callable(match):
            return
        for ignore_case in (False, True):
            value = pattern("a%_*?[\\", ignore_case=ignore_case)
            require(
                isinstance(value, str) and value.startswith("a"),
                f"prefix_pattern must answer the pattern text, not {value!r}",
            )
            expression = match(
                sa.column("title", sa.String()),
                sa.bindparam("p", value),
                ignore_case=ignore_case,
            )
            sql = str(expression.compile(dialect=self.dialect))
            require("title" in sql, f"prefix_match must read the column: {sql}")

    def test_conflict_insert_has_the_on_conflict_shape(self) -> None:
        table = self._table()
        statement = self.renderer.conflict_insert(table)
        if statement is None:
            return  # refused at the call (R015)
        for name in ("on_conflict_do_nothing", "on_conflict_do_update", "excluded"):
            require(hasattr(statement, name), f"conflict_insert lacks .{name}")
        ignore = statement.values(id=1, key="k", value=1, state="s")
        ignore = ignore.on_conflict_do_nothing(index_elements=["id"])
        require(
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
        require(bool(self._sql(upsert)), "on_conflict_do_update does not compile")

    def test_greatest_writes_one_expression_of_its_arguments(self) -> None:
        table = self._table()
        element = self.renderer.greatest(table.c.value, sa.literal(3))
        if element is None:
            raise AssertionError("greatest returned None")
        sql = self._sql(sa.select(element))
        require(
            "conformance_items.value" in sql,
            f"greatest dropped an argument: {sql}",
        )

    def test_id_in_binds_the_ids_under_its_key(self) -> None:
        table = self._table()
        element = self.renderer.id_in(table.c.id, [1, 2], key="conformance_ids")
        if element is None:
            raise AssertionError("id_in returned None")
        compiled = sa.select(table.c.id).where(element).compile(dialect=self.dialect)
        require(
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
            require(table in sql, f"array_insert_sql lacks {table}: {sql}")
            for column, _ in columns:
                require(
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
                require(isinstance(locked, sa.Select), f"{what} must return a Select")
                sql = self._sql(locked)
                require(sql.startswith(base), f"{what} changed the query: {sql}")
                rendered[(skip_locked, read)] = sql
        if "skip_locked" in self.renderer.features():
            require(
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
        require(bool(sql), "relaxed_commit_clause does not compile in a WHERE")

    def test_wall_clock_is_a_default_and_an_update_value(self) -> None:
        sql = self.renderer.wall_clock()
        require(
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
        require(sql in ddl, f"wall_clock is not the column's default: {ddl}")
        update = self._sql(sa.update(table).values(at=sa.text(sql)))
        require(sql in update, f"wall_clock is not an update value: {update}")
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
        require(sql in ddl, f"WallClock() is not written as wall_clock: {ddl}")
        update = self._sql(sa.update(stamped).values(n=1))
        require(sql in update, f"WallClock() is not the update value: {update}")

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
        require(
            len(columns) == 1 and isinstance(columns[0].type, sa.Boolean),
            f"{what} must select one boolean column (whether it locked), "
            f"selects {columns}",
        )

    def _lock_runs(self, lock: Any, what: str) -> str:
        """``lock`` is a ``LockStatement`` whose parameters bind its statement."""
        require(
            isinstance(lock, LockStatement),
            f"{what} must return a LockStatement(statement, parameters), "
            f"returns {lock!r}",
        )
        sql = self._sql(lock.statement)
        require(bool(sql), f"{what} compiles to nothing")
        try:
            lock.statement.compile(dialect=self.dialect).construct_params(
                dict(lock.parameters)
            )
        except sa.exc.InvalidRequestError as exc:
            raise AssertionError(
                f"{what}: its parameters do not bind its statement: {exc}"
            ) from exc
        return sql

    def test_lock_statement_compiles_or_is_absent(self) -> None:
        for key in self.LOCK_KEYS:
            for scope in ("transaction", "session"):
                for wait in (True, False):
                    what = f"lock_statement({key!r}, scope={scope!r}, wait={wait})"
                    lock = self.renderer.lock_statement(key, scope=scope, wait=wait)
                    if lock is None:
                        continue  # the kernel refuses rather than run unlocked
                    self._lock_runs(lock, what)
                    if not wait:
                        self._lock_answers_a_boolean(lock.statement, what)

    def test_a_lock_binds_its_key_at_execution(self) -> None:
        """The kernel takes a lock per request: the key is a parameter, so
        the statement (and its compiled form) is the same for every key of
        one shape."""
        pairs = ((42, 43), ("a", "b"), ((1, 2), (1, 3)), ((1, "a"), (1, "b")))
        for one, other in pairs:
            first = self.renderer.lock_statement(one, scope="transaction", wait=False)
            second = self.renderer.lock_statement(
                other, scope="transaction", wait=False
            )
            if first is None or second is None:
                continue
            require(
                self._sql(first.statement) == self._sql(second.statement)
                and first.parameters != second.parameters,
                f"lock_statement({one!r}) and ({other!r}) must share one "
                "statement and differ in their parameters",
            )

    def test_a_session_lock_has_its_unlock(self) -> None:
        for key in self.LOCK_KEYS:
            locks = self.renderer.lock_statement(key, scope="session", wait=True)
            unlock = self.renderer.unlock_statement(key)
            require(
                (locks is None) == (unlock is None),
                f"unlock_statement({key!r}) must exist exactly when a session "
                "lock does",
            )
            if unlock is not None:
                self._lock_runs(unlock, f"unlock_statement({key!r})")
                self._lock_answers_a_boolean(unlock.statement, "unlock_statement")

    def test_lock_statement_refuses_a_key_of_no_shape(self) -> None:
        keys: tuple[Any, ...] = (1.5, True, (1, 2, 3), (1 << 40, 2), None)
        for key in keys:
            try:
                self.renderer.lock_statement(key, scope="transaction", wait=True)
            except TypeError:
                continue
            raise AssertionError(f"lock_statement accepted the key {key!r}")

    #: Every unit shape the kernel asks :meth:`SqlDialect.begin_statement` for.
    UNITS: ClassVar[tuple[tuple[bool, bool, bool], ...]] = tuple(
        (write, read_only, snapshot)
        for write in (False, True)
        for read_only in (False, True)
        for snapshot in (False, True)
        if not (write and read_only)
    )

    def test_begin_statement_is_a_begin_or_the_plain_one(self) -> None:
        features = self.renderer.features()
        for write, read_only, snapshot in self.UNITS:
            what = (
                f"begin_statement(write={write}, read_only={read_only}, "
                f"snapshot={snapshot})"
            )
            sql = self.renderer.begin_statement(
                write=write, read_only=read_only, snapshot=snapshot
            )
            if sql is None:
                require(
                    not (read_only and "readonly_transactions" in features),
                    f"{what} is the plain BEGIN, but the renderer declares "
                    "readonly_transactions",
                )
                continue
            require(
                isinstance(sql, str) and sql.upper().startswith("BEGIN"),
                f"{what} must be a BEGIN statement, got {sql!r}",
            )
            require(bool(self._sql(sa.text(sql))), f"{what} compiles")
        if "snapshot_reads" in features and "readonly_transactions" in features:
            one, fresh = (
                self.renderer.begin_statement(
                    write=False, read_only=True, snapshot=snapshot
                )
                for snapshot in (True, False)
            )
            require(
                one is not None,
                "begin_statement(write=False, read_only=True, snapshot=True) "
                "is the plain BEGIN, but the renderer declares snapshot_reads",
            )
            if not self.read_units_share_one_snapshot:
                require(
                    one != fresh,
                    "a read-only unit with one snapshot must begin differently "
                    "from one with a snapshot per statement (set "
                    "read_units_share_one_snapshot only when every read-only "
                    "transaction of the database is one snapshot)",
                )

    def test_begin_statement_refuses_a_writing_read_only_unit(self) -> None:
        try:
            self.renderer.begin_statement(write=True, read_only=True, snapshot=False)
        except ValueError:
            return
        raise AssertionError("begin_statement accepted write=True, read_only=True")

    def test_transaction_options_are_execution_options(self) -> None:
        features = self.renderer.features()
        for read_only in (False, True):
            for snapshot in (None, False, True):
                what = (
                    f"transaction_options(read_only={read_only}, snapshot={snapshot})"
                )
                options = self.renderer.transaction_options(
                    read_only=read_only, snapshot=snapshot
                )
                require(isinstance(options, Mapping), f"{what} must be a mapping")
                require(
                    all(isinstance(key, str) for key in options),
                    f"{what} keys must be execution option names",
                )
                if read_only and "readonly_transactions" in features:
                    require(
                        bool(options),
                        f"{what} is empty, but the renderer declares "
                        "readonly_transactions",
                    )
                if snapshot and "snapshot_reads" in features:
                    require(
                        bool(options),
                        f"{what} is empty, but the renderer declares "
                        "snapshot_reads: a snapshot unit needs options of its own",
                    )

    def _form(self, feature: str, member: str) -> Callable[..., Any]:
        found = getattr(self.renderer, member, None)
        require(
            callable(found),
            f"the renderer declares {feature!r} but has no {member}()",
        )
        return cast("Callable[..., Any]", found)

    def _declares(self, feature: str) -> bool:
        return feature in self.renderer.features()

    def _feature_table(self) -> sa.Table:
        return sa.Table(
            "conformance_docs",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
            sa.Column("title", sa.String(80)),
            sa.Column("body", sa.Text),
            sa.Column("doc", sa.JSON),
        )

    def test_arrays_store_a_list_column(self) -> None:
        if not self._declares("arrays"):
            return  # Array refuses (SANDLOOM-R015); a module is refused (B033)
        for item in (sa.String(40), sa.Integer(), sa.Uuid(), UtcDateTime()):
            type_ = self._form("arrays", "array_type")(item)
            require(type_ is not None, f"array_type({item!r}) answered None")
            column = sa.Column("tags", type_)
            sql = self._sql(
                CreateTable(sa.Table("conformance_arr", sa.MetaData(), column))
            )
            require("tags" in sql, f"an array column does not compile: {sql}")

    def test_fulltext_search_has_an_index_and_a_match(self) -> None:
        if not self._declares("fulltext_search"):
            return  # fulltext_index / matches refuse (SANDLOOM-R015)
        table = self._feature_table()
        columns = [table.c.title, table.c.body]
        document = self._form("fulltext_search", "fulltext_document")(
            columns, "english"
        )
        require(document is not None, "fulltext_document answered None")
        options = self._form("fulltext_search", "fulltext_index_options")()
        require(
            options is not None
            and all(name.startswith(f"{self.dialect.name}_") for name in options),
            f"fulltext_index_options must be {self.dialect.name}_* options: {options}",
        )
        index = sa.Index("ix_conformance_text", document, **(options or {}))
        table.append_constraint(index)
        ddl = self._sql(CreateIndex(index))
        require("title" in ddl and "body" in ddl, f"the index misses a column: {ddl}")
        query = sa.bindparam("q", "cats", type_=sa.Text())
        condition = self._form("fulltext_search", "fulltext_match")(
            document, query, "english"
        )
        require(condition is not None, "fulltext_match answered None")
        compiled = sa.select(table.c.id).where(condition).compile(dialect=self.dialect)
        require("q" in compiled.params, "fulltext_match must bind the query")
        require(
            not document.compile(dialect=self.dialect).params,
            "fulltext_document binds a parameter: its constants must be inline, "
            "or the query's expression differs from the index's",
        )

    def test_json_path_reads_a_scalar_as_text(self) -> None:
        if not self._declares("json_path"):
            return  # json_path refuses (SANDLOOM-R015)
        table = self._feature_table()
        value = self._form("json_path", "json_path")(table.c.doc, ("a", 0))
        require(value is not None, "json_path answered None")
        sql = self._sql(sa.select(table.c.id).where(value == "x"))
        require("doc" in sql, f"json_path does not read the column: {sql}")

    async def read_json_paths(
        self, document: str, paths: tuple[tuple[str | int, ...], ...]
    ) -> list[str | None]:
        """Run ``json_path`` on the real database: store ``document`` (JSON
        text, kept as written) in a JSON column and answer what
        ``sandloom.sdk.data.json_path`` reads at each path. A renderer that
        declares ``json_path`` must override it: the kit checks the answers
        against :data:`JSON_PATH_READS` and fails without the override."""
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_json_path_reads_scalars_as_stored(self) -> None:
        if not self._declares("json_path"):
            return  # json_path refuses (SANDLOOM-R015)
        paths = tuple(path for path, _text in JSON_PATH_READS)
        try:
            read = await self.read_json_paths(JSON_PATH_DOCUMENT, paths)
        except NotImplementedError:
            raise AssertionError(
                "the renderer declares json_path: override read_json_paths to "
                "run the kit's JSON_PATH_READS on the database"
            ) from None
        wrong = [
            f"{path}: {got!r}, not {text!r}"
            for (path, text), got in zip(JSON_PATH_READS, read, strict=True)
            if got != text
        ]
        require(
            not wrong,
            "json_path must read the scalar's JSON text as stored (a number "
            "as written, not as the database's float): " + "; ".join(wrong),
        )

    async def read_aware_time(self, value: datetime) -> Any:
        """Store the aware ``value`` (an offset other than UTC) in a
        ``DateTime(timezone=True)`` column on the real database and answer
        what its DBAPI reads back, with no type processing (a plain
        ``DateTime(timezone=True)``, not ``UtcDateTime``). A renderer that
        states ``aware_times`` must override it: the kit checks that the
        value comes back aware, the same instant."""
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_aware_times_read_back_aware(self) -> None:
        if not getattr(self.renderer, "aware_times", False):
            return  # UtcDateTime converts on this database
        value = datetime(
            2026, 11, 5, 12, 30, 15, 123456, tzinfo=timezone(timedelta(hours=2))
        )
        try:
            read = await self.read_aware_time(value)
        except NotImplementedError:
            raise AssertionError(
                "the renderer states aware_times: override read_aware_time to "
                "read an aware time back from the database"
            ) from None
        require(
            isinstance(read, datetime) and read.tzinfo is not None,
            f"aware_times: the DBAPI must read an aware time back, read {read!r} "
            "(UtcDateTime converts nothing on this database)",
        )
        require(read == value, f"aware_times: read {read!r}, stored {value!r}")

    def test_json_contains_binds_its_document(self) -> None:
        if not self._declares("json_contains"):
            return  # json_contains refuses (SANDLOOM-R015)
        table = self._feature_table()
        bound = sa.bindparam("v", {"a": [1]}, type_=sa.JSON())
        condition = self._form("json_contains", "json_contains")(table.c.doc, bound)
        require(condition is not None, "json_contains answered None")
        compiled = sa.select(table.c.id).where(condition).compile(dialect=self.dialect)
        require("v" in compiled.params, "json_contains must bind the value")

    def test_list_aggregates_read_a_list_or_are_absent(self) -> None:
        if not self._declares("list_aggregates"):
            # aggregate_list refuses (SANDLOOM-R015); a module that declares
            # list_aggregates is refused at startup (SANDLOOM-B033).
            return
        table = self._table()
        aggregate = self._form("list_aggregates", "aggregate_list")(table.c.key)
        require(aggregate is not None, "aggregate_list answered None")
        sql = self._sql(sa.select(aggregate).select_from(table))
        require(
            sql.count("key") == 1 and sql.startswith("SELECT ") and " FROM " in sql,
            f"aggregate_list must be one expression over its value: {sql}",
        )
        type_ = self._form("list_aggregates", "aggregate_list_type")(sa.String(40))
        require(type_ is not None, "aggregate_list_type answered None")
        require(
            type_.python_type is list,
            f"aggregate_list_type must read a list (python_type list): {type_!r}",
        )

    def test_row_security_is_whole_or_absent(self) -> None:
        security = self.renderer.row_security()
        if security is None:
            return  # tenant_rls refused (SANDLOOM-B026); the ORM guard only
        missing = [m for m in ROW_SECURITY_MEMBERS if not hasattr(security, m)]
        require(not missing, f"row_security() lacks {missing}")
        table = "conformance_items"
        require(bool(security.policy_name(table)), "policy_name must name the policy")
        for system_access in (False, True):
            ddl = security.enable_statements(
                table,
                column="tenant_id",
                schema=None,
                system_access=system_access,
                system_role="conformance_system",
            )
            require(bool(ddl), "enable_statements wrote no DDL")
            for statement in ddl:
                require(
                    bool(self._sql(sa.text(statement))),
                    f"enable_statements: {statement!r} does not compile",
                )
            require(
                any(table in statement for statement in ddl),
                f"enable_statements does not name the table: {ddl}",
            )
        revert = security.disable_statements(table, schema=None)
        require(bool(revert), "disable_statements wrote no DDL")
        for statement in revert:
            require(bool(self._sql(sa.text(statement))), f"{statement!r} compiles")
        try:
            security.enable_statements(
                'x"; DROP TABLE t; --',
                column="tenant_id",
                schema=None,
                system_access=False,
                system_role="conformance_system",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("enable_statements accepted an unsafe identifier")
        bind = security.bind_statement("$1")
        require(
            "$1" in bind and TENANT_SETTING in bind,
            f"bind_statement must set {TENANT_SETTING} to the placeholder: {bind}",
        )
        value = "00000000-0000-0000-0000-00000000000a"
        merged = security.begin_and_bind("BEGIN", value)
        require(
            merged.startswith("BEGIN") and value in merged,
            f"begin_and_bind must open with the BEGIN and bind the value: {merged}",
        )
        for name in (
            "role_attributes",
            "privileged_memberships",
            "owned_secured_tables",
            "unconditional_policies",
        ):
            require(bool(self._sql(getattr(security, name)())), f"{name} compiles")
        for name in ("secured_tables", "table_policies"):
            compiled = getattr(security, name)().compile(dialect=self.dialect)
            require(
                "names" in compiled.params,
                f"{name} must bind the table list as 'names': {compiled.params}",
            )


__all__ = ["JSON_PATH_DOCUMENT", "JSON_PATH_READS", "SqlDialectConformance"]
