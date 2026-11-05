"""The built-in SQL dialect renderers pass the driver kit's conformance suite,
and the suite fails a renderer that breaks the contract."""

from __future__ import annotations

import contextlib
import tempfile
import types
import uuid
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import asyncpg, psycopg2
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.sql_dialects import (
    POSTGRESQL,
    SQLITE,
    PostgresqlSql,
    SqliteSql,
)
from sandloom.core.infra.contracts import RequestLogBusy
from sandloom.core.migrations import alembic_support
from sandloom.core.migrations.migrations import (
    MIGRATION_LOCK_KEY,
    PROVISION_LOCK_CLASS,
    migration_lock,
)
from sandloom.core.sql_alchemy import portable, request_log
from sandloom.core.sql_alchemy.command_log import COMMAND_LOG
from sandloom.core.sql_alchemy.durable_inbox import INBOX_LOG
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store.statements import _any_id
from sandloom.core.sql_alchemy.portable import (
    IncompatibleSqlDialect,
    UnsupportedSqlForm,
    conflict_insert_for,
    dialect_name,
    greatest_for,
    id_in_for,
    register_sql_dialect,
)
from sandloom.core.sql_alchemy.projection_watermarks import (
    _take_purge_lock,
    purge_lock_key,
)
from sandloom.core.testing.workers import worker_env_values
from sandloom.driverkit import SqlDialect
from sandloom.driverkit.testing import SqlDialectConformance

REPO = Path(__file__).resolve().parents[4]


async def _json_path_reads(
    engine: AsyncEngine, document: str, paths: tuple[tuple[str | int, ...], ...]
) -> list[str | None]:
    """``json_path`` at each path of ``document``, stored as written."""
    table = sa.Table(
        "conformance_json",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("doc", sa.JSON),
    )
    raw: Any = sa.literal(document, sa.Text())
    if engine.dialect.name == "postgresql":
        raw = sa.cast(raw, sa.JSON())  # json keeps the text as written
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.run_sync(table.metadata.create_all)
            await connection.execute(table.insert().values(id=1, doc=raw))
            read = await connection.execute(
                sa.select(
                    *(portable.json_path(table.c.doc, *path) for path in paths)
                ).select_from(table)
            )
            answers = list(read.one())
            await transaction.rollback()
    finally:
        await engine.dispose()
    return answers


class TestPostgresqlRenderer(SqlDialectConformance):
    renderer: ClassVar[SqlDialect] = POSTGRESQL
    dialect: ClassVar[Any] = postgresql.dialect()  # type: ignore[no-untyped-call]

    async def read_json_paths(
        self, document: str, paths: tuple[tuple[str | int, ...], ...]
    ) -> list[str | None]:
        url = worker_env_values(REPO / ".env.test")["DATABASE_URL"]
        if not url.startswith("postgresql"):
            pytest.skip("the test database is not PostgreSQL")
        engine = driver.build_disposable_engine(url)
        return await _json_path_reads(engine, document, paths)


class TestSqliteRenderer(SqlDialectConformance):
    renderer: ClassVar[SqlDialect] = SQLITE
    dialect: ClassVar[Any] = sqlite.dialect()
    # A deferred read transaction under WAL is one snapshot (slice 8a).
    read_units_share_one_snapshot: ClassVar[bool] = True

    async def read_json_paths(
        self, document: str, paths: tuple[tuple[str | int, ...], ...]
    ) -> list[str | None]:
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+aiosqlite:///{Path(directory) / 'conformance.db'}"
            return await _json_path_reads(driver._build_engine(url), document, paths)


class _HalfSearch(PostgresqlSql):
    """Advertises a search index but cannot write its columns."""

    def search_operand(self, column: str, case_insensitive: bool) -> None:  # type: ignore[override]
        return None


class _OneReadOnlyBegin(PostgresqlSql):
    """Begins every read-only unit alike: a snapshot scope reads several."""

    def begin_statement(
        self, *, write: bool, read_only: bool, snapshot: bool
    ) -> str | None:
        if read_only:
            return "BEGIN READ ONLY"
        return super().begin_statement(
            write=write, read_only=read_only, snapshot=snapshot
        )


class _WrongOptions(PostgresqlSql):
    """Writes another database's partial-index option."""

    def partial_index_options(self, where: Any) -> dict[str, Any]:
        return {"sqlite_where": where}


@pytest.mark.parametrize(
    ("renderer", "check"),
    [
        (_HalfSearch(), "test_search_index_is_whole_or_absent"),
        (_WrongOptions(), "test_partial_index_options_restrict_the_index"),
        (_OneReadOnlyBegin(), "test_begin_statement_is_a_begin_or_the_plain_one"),
    ],
)
def test_the_suite_fails_a_renderer_that_breaks_the_contract(
    renderer: SqlDialect, check: str
) -> None:
    class Broken(SqlDialectConformance):
        pass

    Broken.renderer = renderer
    Broken.dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    with pytest.raises(AssertionError):
        getattr(Broken(), check)()


@pytest.mark.asyncio
async def test_the_suite_fails_a_json_path_renderer_without_read_json_paths() -> None:
    """A renderer declaring json_path that never runs it on its database
    fails the kit (risk fix 3): its number text is unchecked otherwise."""

    class Unrun(SqlDialectConformance):
        renderer = SQLITE
        dialect = sqlite.dialect()

    with pytest.raises(AssertionError, match="override read_json_paths"):
        await Unrun().test_json_path_reads_scalars_as_stored()


def test_only_a_one_snapshot_database_may_begin_read_units_alike() -> None:
    """SQLite states it by design and passes; the same flag off fails it."""

    class Lite(SqlDialectConformance):
        renderer = SQLITE
        dialect = sqlite.dialect()
        read_units_share_one_snapshot = True

    Lite().test_begin_statement_is_a_begin_or_the_plain_one()
    Lite.read_units_share_one_snapshot = False
    with pytest.raises(AssertionError, match="begin differently"):
        Lite().test_begin_statement_is_a_begin_or_the_plain_one()


# -- greatest, id_in and the kernel sites on them (slice 2) ----------------- #


def _items() -> sa.Table:
    return sa.Table(
        "renderer_items",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("n", sa.Integer, nullable=False),
    )


def _sql(element: Any, dialect: Any) -> str:
    return " ".join(str(element.compile(dialect=dialect)).split())


def test_greatest_is_greatest_on_postgresql_and_max_on_sqlite() -> None:
    table = _items()
    pg = _sql(sa.select(greatest_for("postgresql", table.c.n, 0)), asyncpg.dialect())
    lite = _sql(sa.select(greatest_for("sqlite", table.c.n, 0)), sqlite.dialect())
    assert pg.startswith("SELECT greatest(renderer_items.n, $1::INTEGER)")
    assert lite.startswith("SELECT max(renderer_items.n, ?)")


def test_id_in_is_one_array_bind_on_postgresql_and_in_elsewhere() -> None:
    table = _items()
    stmt = sa.select(table.c.n).where(
        id_in_for("postgresql", table.c.id, [], key="ids")
    )
    assert _sql(stmt, asyncpg.dialect()).endswith(
        "WHERE renderer_items.id = ANY ($1::UUID[])"
    )
    stmt = sa.select(table.c.n).where(id_in_for("sqlite", table.c.id, [], key="ids"))
    assert "renderer_items.id IN (__[POSTCOMPILE_ids])" in _sql(stmt, sqlite.dialect())


def test_the_forms_run_on_sqlite() -> None:
    table = _items()
    engine = sa.create_engine("sqlite://")
    ids = [uuid.uuid4() for _ in range(3)]
    with engine.begin() as conn:
        table.create(conn)
        conn.execute(table.insert(), [{"id": i, "n": n} for n, i in enumerate(ids)])
        assert conn.execute(sa.select(greatest_for(conn, 1, 7, 3))).scalar() == 7
        chosen = sa.select(table.c.n).where(id_in_for(conn, table.c.id, [], key="ids"))
        found = conn.execute(chosen, {"ids": ids[1:]}).scalars().all()
        assert sorted(found) == [1, 2]
        upsert = conflict_insert_for(conn, table).values(id=ids[0], n=5)
        conn.execute(
            upsert.on_conflict_do_update(
                index_elements=["id"],
                set_={"n": greatest_for(conn, table.c.n, upsert.excluded.n)},
            )
        )
        assert (
            conn.execute(sa.select(table.c.n).where(table.c.id == ids[0])).scalar() == 5
        )
    engine.dispose()


def test_the_helpers_read_the_dialect_of_any_bind() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn, Session(conn) as session:
        for bind in ("sqlite", sqlite.dialect(), engine, conn, session):
            assert dialect_name(bind) == "sqlite"
    engine.dispose()


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registrations of the test stay in a copy of the registry."""
    portable.sql_dialect("sqlite")  # the entry points are read first
    monkeypatch.setattr(portable, "_registered", dict(portable._registered))


@pytest.mark.usefixtures("isolated_registry")
def test_a_renderer_without_the_form_refuses() -> None:
    class _NoGreatest(SqliteSql):
        name = "renderer_test_nogreatest"

        def greatest(self, *exprs: Any) -> None:  # type: ignore[override]
            return None

    register_sql_dialect("renderer_test_nogreatest", _NoGreatest())
    with pytest.raises(UnsupportedSqlForm):
        greatest_for("renderer_test_nogreatest", 1, 2)
    with pytest.raises(UnsupportedSqlForm):
        id_in_for("renderer_test_unknown", sa.column("id"), [])


def test_the_hot_claim_renders_the_same_sql_on_postgresql() -> None:
    """The command log's claim is the INSERT the kernel wrote by hand before
    (``postgresql.insert`` ... ON CONFLICT DO UPDATE WHERE expired)."""
    table = COMMAND_LOG.table()
    spec = COMMAND_LOG.spec
    base = postgresql.insert(table).values(
        {name: sa.bindparam(name) for name in spec.columns}
    )
    before = base.on_conflict_do_update(
        index_elements=[table.c[name] for name in spec.conflict],
        set_={
            name: base.excluded[name]
            for name in spec.columns
            if name not in spec.conflict
        },
        where=table.c["expires_at"] <= sa.bindparam("claim_now"),
    ).returning(table.c[spec.returning])
    now = COMMAND_LOG.claim_statement("postgresql")
    assert _sql(now, asyncpg.dialect()) == _sql(before, asyncpg.dialect())
    inbox = INBOX_LOG.claim_statement("postgresql")
    assert "ON CONFLICT (consumer, event_id) DO NOTHING" in _sql(
        inbox, asyncpg.dialect()
    )


def test_the_outbox_id_membership_renders_the_same_sql_on_postgresql() -> None:
    """``id = ANY($1::UUID[])``: one array bind named ``outbox_ids``."""
    table: Any = OutboxMessage.__table__
    before = table.c.id == sa.any_(
        sa.bindparam(
            "outbox_ids",
            value=[],
            type_=postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
        )
    )
    after = _any_id("postgresql", table, [])
    for dialect in (asyncpg.dialect(), postgresql.dialect()):
        assert _sql(sa.update(table).where(after).values(last_error=None), dialect) == (
            _sql(sa.update(table).where(before).values(last_error=None), dialect)
        )


class _AsyncOver:
    """The slice of ``AsyncSession`` the dedupe log uses, over a sync one
    (no async SQLite driver until slice 8a)."""

    def __init__(self, session: Session) -> None:
        self.sync_session = session
        self.info = session.info

    @property
    def no_autoflush(self) -> Any:
        return self.sync_session.no_autoflush

    async def execute(self, statement: Any, params: Any = None) -> Any:
        return self.sync_session.execute(statement, params)


async def test_the_command_log_claims_and_restarts_its_expiry_on_sqlite() -> None:
    from datetime import datetime, timedelta

    table: Any = COMMAND_LOG.table()
    engine = sa.create_engine("sqlite://")
    table.create(engine)
    created = datetime(2026, 1, 1, 12, 0, 0)
    values = {
        "dedupe_key": "k1",
        "contract": "c",
        "tenant_id": None,
        "args_digest": "d",
        "status": "pending",
        "result": None,
        "effects": None,
        "created_at": created,
        "expires_at": created + timedelta(seconds=60),
    }
    with Session(engine) as sync, sync.begin():
        session = _AsyncOver(sync)
        claim = await COMMAND_LOG.claim(session, values, now=created)
        assert claim.outcome == "claimed"
        done = created + timedelta(seconds=90)  # the command outlived its TTL
        await COMMAND_LOG.complete(
            session, {"dedupe_key": "k1"}, status="ok", result="r", now=done
        )
        expires = sync.execute(sa.select(table.c.expires_at)).scalar()
        # expires_at + (now - created_at), as PostgreSQL computed it in SQL
        assert expires == done + timedelta(seconds=60)
    with Session(engine) as sync, sync.begin():
        replay = await COMMAND_LOG.claim(_AsyncOver(sync), values, now=created)
        assert (replay.outcome, replay.result) == ("replay", "r")
    engine.dispose()


async def test_a_lock_timeout_of_the_claim_is_busy() -> None:
    from sqlalchemy.exc import DBAPIError

    from sandloom.core.effects import DedupeBusy

    class _LockTimeout(Exception):
        sqlstate = "55P03"

    class _Pg:
        info: dict[str, Any] = {}

        @property
        def no_autoflush(self) -> Any:
            return Session().no_autoflush

        class sync_session:
            @staticmethod
            def get_bind() -> Any:
                return type("Bind", (), {"dialect": asyncpg.dialect()})()

        async def execute(self, statement: Any, params: Any = None) -> Any:
            raise DBAPIError("INSERT", {}, _LockTimeout())

    with pytest.raises(DedupeBusy):
        await COMMAND_LOG.claim(
            _Pg(), dict.fromkeys(COMMAND_LOG.spec.columns), now=None
        )


def test_the_completion_binds_the_restarted_expiry() -> None:
    sql = _sql(COMMAND_LOG.completion_statement(extend_expiry=True), asyncpg.dialect())
    assert "expires_at=$" in sql.replace(" ", "")


@pytest.mark.usefixtures("isolated_registry")
@pytest.mark.parametrize("member", portable.REQUIRED_MEMBERS)
def test_a_renderer_missing_a_member_of_the_contract_is_refused(member: str) -> None:
    class _Partial:
        contract_version = 2

        def features(self) -> frozenset[str]:
            return frozenset()

    renderer = _Partial()
    for name in portable.REQUIRED_MEMBERS:
        if name not in (member, "features"):
            setattr(renderer, name, getattr(SQLITE, name))
    if member == "features":
        del _Partial.features
    with pytest.raises(IncompatibleSqlDialect) as err:
        register_sql_dialect("renderer_test_partial", cast(Any, renderer))
    assert member in str(err.value) and "SANDLOOM-B025" in str(err.value)
    assert "renderer_test_partial" not in portable._registered


def _command_values(key: str, created: Any, ttl_s: int) -> dict[str, Any]:
    from datetime import timedelta

    return {
        "dedupe_key": key,
        "contract": "c",
        "tenant_id": None,
        "args_digest": "d",
        "status": "pending",
        "result": None,
        "effects": None,
        "created_at": created,
        "expires_at": created + timedelta(seconds=ttl_s),
    }


async def test_a_completion_without_its_claim_warns_and_reads_the_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from datetime import datetime, timedelta

    table: Any = COMMAND_LOG.table()
    engine = sa.create_engine("sqlite://")
    table.create(engine)
    created = datetime(2026, 1, 1, 12, 0, 0)
    with Session(engine) as sync, sync.begin():
        claimed = await COMMAND_LOG.claim(
            _AsyncOver(sync), _command_values("k2", created, 60), now=created
        )
        assert claimed.outcome == "claimed"
    done = created + timedelta(seconds=90)
    with Session(engine) as sync, sync.begin():  # not the claiming session
        with caplog.at_level("WARNING", logger="sandloom.core.sql_alchemy.dedupe_log"):
            await COMMAND_LOG.complete(
                _AsyncOver(sync),
                {"dedupe_key": "k2"},
                status="ok",
                result="r",
                now=done,
            )
        expires = sync.execute(sa.select(table.c.expires_at)).scalar()
    assert expires == done + timedelta(seconds=60)
    assert "did not claim it" in caplog.text
    engine.dispose()


async def test_an_expired_record_taken_over_restarts_from_the_new_claim() -> None:
    from datetime import datetime, timedelta

    table: Any = COMMAND_LOG.table()
    engine = sa.create_engine("sqlite://")
    table.create(engine)
    first = datetime(2026, 1, 1, 12, 0, 0)
    with Session(engine) as sync, sync.begin():  # a record that expired
        await COMMAND_LOG.claim(
            _AsyncOver(sync), _command_values("k3", first, 10), now=first
        )
    later = first + timedelta(hours=1)
    with Session(engine) as sync, sync.begin():
        session = _AsyncOver(sync)
        taken = await COMMAND_LOG.claim(
            session, _command_values("k3", later, 120), now=later
        )
        assert taken.outcome == "claimed"
        done = later + timedelta(seconds=30)
        await COMMAND_LOG.complete(
            session, {"dedupe_key": "k3"}, status="ok", result="r", now=done
        )
        row = sync.execute(sa.select(table.c.created_at, table.c.expires_at)).one()
    # the takeover's own TTL (120 s), counted from completion
    assert row == (later, done + timedelta(seconds=120))
    engine.dispose()


async def test_a_release_forgets_the_claimed_ttl() -> None:
    from datetime import datetime

    table: Any = COMMAND_LOG.table()
    engine = sa.create_engine("sqlite://")
    table.create(engine)
    created = datetime(2026, 1, 1, 12, 0, 0)
    with Session(engine) as sync, sync.begin():
        session = _AsyncOver(sync)
        await COMMAND_LOG.claim(
            session, _command_values("k4", created, 60), now=created
        )
        assert sync.info["sandloom.dedupe_claimed_ttls"]
        await COMMAND_LOG.release(session, {"dedupe_key": "k4"})
        assert not sync.info["sandloom.dedupe_claimed_ttls"]
    engine.dispose()


def test_the_outbox_array_insert_is_the_renderers() -> None:
    from sandloom.core.sql_alchemy import effect_rows

    class _Pg:
        dialect = asyncpg.dialect()

    class _Lite:
        dialect = sqlite.dialect()

    sql = effect_rows._statement(_Pg, True, True).text
    assert sql.startswith("WITH audit AS (INSERT INTO audit_log (id, occurred_at")
    assert "SELECT * FROM unnest(CAST(:o_id AS UUID[])" in sql
    assert effect_rows._statement(_Lite, True, True) is None


# -- locks through the renderer (slice 3) ------------------------------------ #


class _Recording:
    """An async session of ``dialect`` that records the SQL it is given and
    answers ``answer`` to a scalar read."""

    def __init__(self, dialect: Any, answer: Any = True) -> None:
        self.dialect = dialect
        self.answer = answer
        self.statements: list[tuple[str, dict[str, Any]]] = []

    @property
    def no_autoflush(self) -> Any:
        return contextlib.nullcontext()

    async def execute(self, statement: Any, params: Any = None) -> Any:
        compiled = statement.compile(dialect=self.dialect)
        self.statements.append((str(compiled), dict(compiled.params)))
        answer = self.answer

        class _Result:
            def scalar(self) -> Any:
                return answer

        return _Result()


def _compiled(statement: Any, dialect: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=dialect)
    return str(compiled), dict(compiled.params)


async def test_transaction_lock_is_todays_statement_on_postgresql() -> None:
    session = _Recording(asyncpg.dialect())
    await portable.transaction_lock(0x414A_4152_4241_4331, session=cast(Any, session))
    before = _compiled(
        sa.select(sa.func.pg_advisory_xact_lock(0x414A_4152_4241_4331)),
        asyncpg.dialect(),
    )
    assert session.statements == [before]
    assert before[0] == (
        "SELECT pg_advisory_xact_lock($1::BIGINT) AS pg_advisory_xact_lock_1"
    )


async def test_the_request_log_try_lock_is_todays_statement_and_busy_is_busy() -> None:
    key = "f" * 64
    session = _Recording(asyncpg.dialect(), answer=False)
    with pytest.raises(RequestLogBusy):
        await request_log.SQLAlchemyRequestLogStore().claim(
            session,
            dedupe_key=key,
            scope="s",
            tenant=None,
            fingerprint="fp",
            ttl_seconds=60,
        )
    # only the try-lock ran: a lock held elsewhere never waits and never claims
    old = sa.text("SELECT pg_try_advisory_xact_lock(:ns, :k)")
    assert session.statements == [
        (
            str(old.compile(dialect=asyncpg.dialect())),
            {"ns": request_log.ADVISORY_NAMESPACE, "k": request_log.advisory_key(key)},
        )
    ]
    assert session.statements[0][0] == "SELECT pg_try_advisory_xact_lock($1, $2)"


async def test_the_projection_purge_lock_hashes_the_name_on_the_server() -> None:
    session = _Recording(asyncpg.dialect())
    await _take_purge_lock(session, "orders")
    old = sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
    assert session.statements == [
        (
            str(old.compile(dialect=asyncpg.dialect())),
            {"k": purge_lock_key("orders")},
        )
    ]


def test_the_migration_locks_are_todays_on_psycopg2() -> None:
    dialect = psycopg2.dialect()

    def literal(statement: Any) -> str:
        return str(
            statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        )

    key = MIGRATION_LOCK_KEY
    run_wide = portable.lock_statement_for(
        "postgresql", key, scope="session", wait=False
    )
    assert literal(run_wide) == (
        f"SELECT pg_try_advisory_lock({key}) AS pg_try_advisory_lock_1"
    )
    (answer,) = cast(Any, run_wide).selected_columns
    assert isinstance(answer.type, sa.Boolean)
    assert literal(portable.unlock_statement_for("postgresql", key)) == (
        f"SELECT pg_advisory_unlock({key}) AS pg_advisory_unlock_1"
    )
    chain = portable.lock_statement_for(
        "postgresql", key, scope="transaction", wait=True
    )
    assert (
        literal(chain)
        == f"SELECT pg_advisory_xact_lock({key}) AS pg_advisory_xact_lock_1"
    )
    provision = (PROVISION_LOCK_CLASS, "app_db")
    for statement, old in (
        (
            portable.lock_statement_for(
                "postgresql", provision, scope="session", wait=True
            ),
            "SELECT pg_advisory_lock(:ns, hashtext(:k))",
        ),
        (
            portable.unlock_statement_for("postgresql", provision),
            "SELECT pg_advisory_unlock(:ns, hashtext(:k))",
        ),
    ):
        assert _compiled(statement, dialect) == (
            str(sa.text(old).compile(dialect=dialect)),
            {"ns": PROVISION_LOCK_CLASS, "k": "app_db"},
        )


def test_sqlite_has_no_named_lock_yet() -> None:
    for scope in ("transaction", "session"):
        assert (
            portable.optional_lock_statement_for(
                "sqlite", 1, scope=cast(Any, scope), wait=True
            )
            is None
        )
        with pytest.raises(UnsupportedSqlForm, match="lock_statement"):
            portable.lock_statement_for("sqlite", 1, scope=cast(Any, scope), wait=True)
    with pytest.raises(UnsupportedSqlForm, match="unlock_statement"):
        portable.unlock_statement_for("sqlite", 1)
    with pytest.raises(UnsupportedSqlForm, match="no SQL dialect"):
        portable.lock_statement_for("mysql", 1, scope="transaction", wait=True)


async def test_the_request_log_claims_unlocked_without_a_named_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite: no try-lock runs before the claim (its writers queue at BEGIN
    IMMEDIATE instead, slice 8a)."""
    claims: list[Any] = []

    class _Log:
        async def claim(self, session: Any, values: Any, *, now: Any) -> Any:
            claims.append(values["dedupe_key"])
            return types.SimpleNamespace(outcome="claimed")

    monkeypatch.setattr(request_log, "REQUEST_LOG", _Log())
    session = _Recording(sqlite.dialect())
    claim = await request_log.SQLAlchemyRequestLogStore().claim(
        session,
        dedupe_key="a" * 64,
        scope="s",
        tenant=None,
        fingerprint="fp",
        ttl_seconds=60,
    )
    assert claim.outcome == "claimed" and claims == ["a" * 64]
    assert session.statements == []


def test_the_run_wide_migration_lock_skips_a_database_without_one() -> None:
    with migration_lock(["sqlite:///never-opened.db"], timeout=1):
        assert alembic_support.LOCKED_URLS == set()


@pytest.mark.parametrize("key", [1.5, True, (1, 2, 3), (1 << 40, 2), 1 << 64, None])
def test_a_lock_key_of_no_shape_is_refused(key: Any) -> None:
    with pytest.raises(TypeError, match="lock key"):
        portable.lock_statement_for("postgresql", key, scope="transaction", wait=True)


class _LockWithoutUnlock(PostgresqlSql):
    """Has a session lock it cannot release."""

    def unlock_statement(self, key: Any) -> None:  # type: ignore[override]
        return None


class _TryLockAnswersNothing(PostgresqlSql):
    """A never-waiting lock that does not say whether it locked."""

    def lock_statement(self, key: Any, *, scope: Any, wait: bool) -> Any:
        return sa.text("SELECT 1")


@pytest.mark.parametrize(
    ("renderer", "check"),
    [
        (_LockWithoutUnlock(), "test_a_session_lock_has_its_unlock"),
        (_TryLockAnswersNothing(), "test_lock_statement_compiles_or_is_absent"),
    ],
)
def test_the_suite_fails_a_renderer_with_broken_locks(
    renderer: SqlDialect, check: str
) -> None:
    class Broken(SqlDialectConformance):
        pass

    Broken.renderer = renderer
    Broken.dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    with pytest.raises(AssertionError):
        getattr(Broken(), check)()


def test_a_direct_alembic_run_takes_no_lock_while_sandloom_migrate_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL under the run-wide session lock must not take the transaction
    lock on the same key from another connection: it would wait for itself."""
    import sandloom.core.migrations.alembic_support as support

    asked: list[str] = []

    def lock_statement(*_args: Any, **_kwargs: Any) -> str:
        asked.append("asked")
        return "LOCK"

    monkeypatch.setattr(support, "optional_lock_statement_for", lock_statement)
    url = "postgresql+psycopg2://h/held"
    monkeypatch.setattr(support, "LOCKED_URLS", {url})
    assert support._direct_run_lock(object(), url) is None
    assert asked == []
    assert support._direct_run_lock(object(), "postgresql+psycopg2://h/free") == "LOCK"
    assert asked == ["asked"]


# -- row locks and the relaxed commit through the renderer (slice 4) --------- #


class _Rows:
    """A session of ``dialect`` that records the SQL and reads no rows."""

    def __init__(self, dialect: Any) -> None:
        self.dialect = dialect
        self.statements: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append(_compiled(statement, self.dialect))

        class _Result:
            rowcount = 0

            def all(self) -> list[Any]:
                return []

            def first(self) -> None:
                return None

            def mappings(self) -> Any:
                return self

        return _Result()


def test_for_update_is_todays_lock_on_postgresql_and_none_on_sqlite() -> None:
    table = OutboxMessage.__table__
    stmt = sa.select(table.c.id).where(table.c.id == uuid.uuid4())
    pg = asyncpg.dialect()
    for skip_locked in (True, False):
        for read in (True, False):
            ours = portable.for_update_for(
                "postgresql", stmt, skip_locked=skip_locked, read=read
            )
            today = stmt.with_for_update(skip_locked=skip_locked, read=read)
            assert _compiled(ours, pg) == _compiled(today, pg)
            assert (
                portable.for_update_for(
                    "sqlite", stmt, skip_locked=skip_locked, read=read
                )
                is stmt
            )
    assert _compiled(
        portable.for_update_for("postgresql", stmt, skip_locked=False), pg
    ) == _compiled(stmt.with_for_update(), pg)


async def test_the_saga_locks_are_todays_statements_on_postgresql() -> None:
    from datetime import datetime

    from sandloom.core.sql_alchemy import saga_store

    pg = asyncpg.dialect()
    instances = saga_store.INSTANCES
    instance_id = uuid.uuid4()
    for skip_locked in (True, False):
        session = _Rows(pg)
        await saga_store.lock_instance(
            cast(Any, session),
            instance_id,
            tenant=saga_store.ANY_TENANT,
            skip_locked=skip_locked,
        )
        today = (
            sa.select(instances)
            .where(instances.c.id == instance_id)
            .with_for_update(skip_locked=skip_locked)
        )
        assert session.statements == [_compiled(today, pg)]

    now = datetime(2026, 1, 1)
    before = datetime(2025, 12, 31)
    session = _Rows(pg)
    await saga_store.claim_due(
        cast(Any, session), now=now, swept_before=before, limit=7
    )
    today = (
        sa.select(
            instances.c.id,
            instances.c.saga_type,
            instances.c.tenant_id,
            instances.c.version,
        )
        .where(
            saga_store._due_clause(now, before),
            sa.or_(instances.c.swept_at.is_(None), instances.c.swept_at <= before),
        )
        .order_by(instances.c.next_attempt_at)
        .limit(7)
        .with_for_update(skip_locked=True)
    )
    assert session.statements == [_compiled(today, pg)]
    assert "FOR UPDATE SKIP LOCKED" in session.statements[0][0]


async def test_the_relay_batch_claim_is_todays_statement_on_postgresql() -> None:
    from datetime import datetime

    from sandloom.core.sql_alchemy.outbox_store.relay import _RelayMixin

    pg = asyncpg.dialect()
    session = _Rows(pg)
    await _RelayMixin._claim_batch(session, 5, 0.0, 10)
    ((sql, _params),) = session.statements
    table = OutboxMessage.__table__
    now = datetime(2026, 1, 1)
    due = (
        sa.select(table.c.id)
        .where(
            table.c.delivered_at.is_(None),
            table.c.dead_at.is_(None),
            table.c.delivery_attempts < 5,
            table.c.emitted_at < now,
            sa.or_(table.c.next_attempt_at.is_(None), table.c.next_attempt_at <= now),
            sa.or_(table.c.locked_until.is_(None), table.c.locked_until < now),
        )
        .order_by(table.c.emitted_at)
        .limit(10)
        .with_for_update(skip_locked=True)
        .cte("due")
    )
    today = (
        sa.update(table)
        .where(table.c.id == due.c.id)
        .values(locked_until=now, locked_by=uuid.uuid4())
        .returning(
            table.c.id,
            table.c.channel,
            table.c.payload,
            table.c.schema_version,
            table.c.emitted_at,
            table.c.trace_id,
            table.c.delivery_attempts,
        )
    )
    assert sql == _compiled(today, pg)[0]
    assert "FOR UPDATE SKIP LOCKED" in sql


async def test_the_watermark_share_lock_is_todays_statement_on_postgresql() -> None:
    from sandloom.core.sql_alchemy import projection_watermarks

    pg = asyncpg.dialect()
    session = _Rows(pg)
    await projection_watermarks.locked_watermark(session, "orders")
    table = projection_watermarks._TABLE
    today = (
        sa.select(table.c.purged_before, table.c.rebuild_started_at)
        .where(table.c.projection == "orders")
        .with_for_update(read=True)
    )
    assert session.statements[0] == _compiled(today, pg)
    assert session.statements[0][0].endswith("FOR SHARE")


def test_the_mark_delivered_statement_is_todays_on_postgresql() -> None:
    from sandloom.core.sql_alchemy import outbox_store

    class _Pg:
        dialect = asyncpg.dialect()

    class _Lite:
        dialect = sqlite.dialect()

    outbox_store._mark_delivered_statements.clear()
    try:
        table = OutboxMessage.__table__
        today = (
            sa.update(table)
            .where(
                _any_id(_Pg, table, ()),
                table.c.dead_at.is_(None),
                table.c.delivered_at.is_(None),
                sa.text("set_config('synchronous_commit', 'off', true) IS NOT NULL"),
            )
            .values(delivered_at=sa.bindparam("delivered_at"), last_error=None)
        )
        ours = outbox_store._mark_delivered_statement(_Pg)
        assert _compiled(ours, _Pg.dialect) == _compiled(today, _Pg.dialect)
        lite = str(
            outbox_store._mark_delivered_statement(_Lite).compile(dialect=_Lite.dialect)
        )
        assert "set_config" not in lite and "outbox_messages.id IN" in lite
    finally:
        outbox_store._mark_delivered_statements.clear()


def test_relaxed_commit_is_postgresqls_synchronous_commit_only() -> None:
    clause = POSTGRESQL.relaxed_commit_clause()
    assert str(clause) == "set_config('synchronous_commit', 'off', true) IS NOT NULL"
    assert SQLITE.relaxed_commit_clause() is None
