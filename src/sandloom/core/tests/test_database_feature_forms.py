"""Slice 9: the database feature forms of ``sandloom.sdk.data`` and the
requirement rule.

``Array`` (``arrays``), ``fulltext_index`` / ``matches`` (``fulltext_search``),
``json_path`` and ``json_contains``: written by each renderer, run on the test
PostgreSQL and a SQLite file, refused where the renderer lacks the feature
(SANDLOOM-R015). A module whose tables use one is refused at load on an
instance without it (SANDLOOM-B033); ``sandloom check`` makes a module declare
the feature of every form it uses (SANDLOOM-C030).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.autogenerate import render_python_code
from alembic.migration import MigrationContext
from alembic.operations import ops
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateIndex, CreateTable

import sandloom.core.orm as orm
from sandloom.core.archcheck.module_rules import find_database_feature_problems
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.sql_dialects import POSTGRESQL, SQLITE, SqliteSql
from sandloom.core.drivers.sqlalchemy_database import (
    SQLALCHEMY_DB_FEATURES,
    SQLAlchemyDatabaseOptions,
    create_sqlalchemy_database,
    sqlalchemy_db_features,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.capabilities import DATABASE_CAPABILITY, DATABASE_FORM_FEATURES
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import DatabaseFeatureMissing, InfraRegistry
from sandloom.core.loader.plugin_loader import _preflight_capabilities
from sandloom.core.migrations.alembic_support import _render_item
from sandloom.core.sql_alchemy.portable import (
    IncompatibleSqlDialect,
    UnsupportedSqlForm,
    UtcDateTime,
    register_sql_dialect,
)
from sandloom.core.testing.workers import worker_env_values
from sandloom.sdk.data import (
    Array,
    fulltext_index,
    json_contains,
    json_path,
    matches,
)

PG = postgresql.dialect()  # type: ignore[no-untyped-call]
LITE = sqlite.dialect()
REPO = Path(__file__).resolve().parents[4]


def _sql(element: Any, dialect: Any) -> str:
    return " ".join(str(element.compile(dialect=dialect)).split())


def _items() -> sa.Table:
    return sa.Table(
        "slice9_items",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("tags", Array(sa.String(40))),
        sa.Column("title", sa.String(80)),
        sa.Column("body", sa.Text),
        sa.Column("doc", sa.JSON),
        fulltext_index("ix_slice9_items_text", "title", "body"),
    )


def _docs() -> sa.Table:
    return sa.Table(
        "slice9_docs",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("doc", sa.JSON),
    )


# -- the renderers ------------------------------------------------------------


def test_postgresql_advertises_every_feature_form_and_sqlite_json_path() -> None:
    forms = set(DATABASE_FORM_FEATURES.values())
    assert forms <= POSTGRESQL.features()
    assert SQLITE.features() & forms == {
        "json_path",
        "transaction_locks",
        "list_aggregates",
    }
    assert set(DATABASE_FORM_FEATURES.values()) <= DATABASE_CAPABILITY.features
    assert forms <= sqlalchemy_db_features({"url": "postgresql+asyncpg://h/d"})
    assert "arrays" not in sqlalchemy_db_features({"url": "sqlite+aiosqlite:///x.db"})


def test_postgresql_writes_array_columns_and_the_fulltext_index() -> None:
    table = _items()
    ddl = _sql(CreateTable(table), PG)
    assert "tags VARCHAR(40)[]" in ddl
    (index,) = table.indexes
    assert _sql(CreateIndex(index), PG) == (
        "CREATE INDEX ix_slice9_items_text ON slice9_items USING gin "
        "(to_tsvector('english'::regconfig, (coalesce(CAST(title AS TEXT), '') "
        "|| ' ') || coalesce(CAST(body AS TEXT), '')))"
    )
    assert "TIMESTAMP WITH TIME ZONE[]" in _sql(
        CreateTable(
            sa.Table("t", sa.MetaData(), sa.Column("at", Array(UtcDateTime())))
        ),
        PG,
    )


def test_postgresql_writes_the_expressions() -> None:
    table = _items()
    query = sa.select(table.c.id).where(
        matches([table.c.title, table.c.body], "cats"),
        json_path(table.c.doc, "a", 0) == "x",
        json_contains(table.c.doc, {"a": [1]}),
    )
    sql = _sql(query, PG)
    # The match's document is the index's expression (constants inline).
    assert (
        "to_tsvector('english'::regconfig, (coalesce(CAST(slice9_items.title AS "
        "TEXT), '') || ' ') || coalesce(CAST(slice9_items.body AS TEXT), '')) @@ "
        "websearch_to_tsquery('english'::regconfig, " in sql
    )
    assert "slice9_items.doc #>> CAST(ARRAY[" in sql
    assert "CAST(slice9_items.doc AS JSONB) @> CAST(" in sql


def test_sqlite_writes_json_path_and_refuses_the_rest() -> None:
    table = _items()
    sql = _sql(sa.select(table.c.id).where(json_path(table.c.doc, "a", 0) == "x"), LITE)
    assert "json_type(slice9_items.doc, ?)" in sql
    assert "CAST(json_extract(slice9_items.doc, ?) AS TEXT)" in sql
    refused = {
        "Array": lambda: CreateTable(table),
        "fulltext_index": lambda: CreateIndex(next(iter(table.indexes))),
        "matches": lambda: sa.select(table.c.id).where(matches(table.c.title, "x")),
        "json_contains": lambda: sa.select(table.c.id).where(
            json_contains(table.c.doc, {"a": 1})
        ),
    }
    for form, build in refused.items():
        with pytest.raises(UnsupportedSqlForm) as caught:
            build().compile(dialect=LITE)
        assert caught.value.form == form
        assert caught.value.feature == DATABASE_FORM_FEATURES[form]
        assert "SANDLOOM-R015" in str(caught.value)


def test_a_json_path_is_part_of_the_cache_key() -> None:
    table = _docs()
    one = sa.select(table.c.id).where(json_path(table.c.doc, "a") == "x")
    other = sa.select(table.c.id).where(json_path(table.c.doc, "b") == "x")
    key_one, key_other = one._generate_cache_key(), other._generate_cache_key()
    assert key_one is not None and key_other is not None
    assert key_one.key != key_other.key


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda c: json_path(c), ValueError),
        (lambda c: json_path(c, 'a"b'), ValueError),
        (lambda c: json_path(c, True), TypeError),
        (lambda c: json_path(c, "a", -1), ValueError),
        (lambda c: matches(c, "x", language="english; DROP"), ValueError),
        (lambda c: Array(Array(sa.String())), TypeError),
    ],
)
def test_the_forms_refuse_what_they_cannot_write(call: Any, error: type) -> None:
    with pytest.raises(error):
        call(_docs().c.doc)


def test_a_renderer_declaring_a_feature_form_must_write_it() -> None:
    class HalfSql(SqliteSql):
        def features(self) -> frozenset[str]:
            return super().features() | {"arrays"}

    with pytest.raises(IncompatibleSqlDialect, match="arrays: array_type"):
        register_sql_dialect("slice9_half", HalfSql())


def test_a_migration_renders_an_array_as_the_databases_type() -> None:
    context = MigrationContext.configure(dialect_name="postgresql")
    table = sa.Table(
        "slice9_mig",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tags", Array(sa.String(40))),
        sa.Column("ats", Array(UtcDateTime())),
    )
    code = render_python_code(
        ops.UpgradeOps([ops.CreateTableOp.from_table(table)]),
        render_item=_render_item,
        migration_context=context,
    )
    assert "sandloom" not in code
    assert "postgresql.ARRAY(sa.String(length=40))" in code
    assert "postgresql.ARRAY(sa.DateTime(timezone=True))" in code


# -- on the databases ---------------------------------------------------------

#: ``doc`` per row, and what json_path reads at each path on every database:
#: strings, booleans, null, 64-bit integers, missing paths and an index step
#: on an object (NULL) are the form's contract.
_ROWS = {
    1: {
        "s": "FR",
        "n": 42,
        "t": True,
        "f": False,
        "z": None,
        "a": ["x", {"k": "v"}],
        "o": {"0": 7},
    },
    2: {"s": "DE", "n": 7, "a": [], "big": 9223372036854775807},
}
#: A row written as raw JSON text: numbers outside the contract.
_RAW_ID = 3
_RAW = '{"frac": 1.50, "exp": 1e2, "huge": 12345678901234567890, "mz": -0.0}'
_PATHS: list[tuple[tuple[str | int, ...], dict[int, str | None]]] = [
    (("s",), {1: "FR", 2: "DE", 3: None}),
    (("n",), {1: "42", 2: "7", 3: None}),
    (("t",), {1: "true", 2: None, 3: None}),
    (("f",), {1: "false", 2: None, 3: None}),
    (("z",), {1: None, 2: None, 3: None}),
    (("a", 0), {1: "x", 2: None, 3: None}),
    (("a", 1, "k"), {1: "v", 2: None, 3: None}),
    (("o", 0), {1: None, 2: None, 3: None}),  # an index steps into arrays only
    (("o", "0"), {1: "7", 2: None, 3: None}),  # a key step into the object
    (("big",), {1: None, 2: "9223372036854775807", 3: None}),
    (("missing",), {1: None, 2: None, 3: None}),
]
#: Outside the contract: each database's own text of the raw row's numbers
#: (the documented reason to compare such numbers as integers or store them
#: as strings). Pinned so a change on either side is seen.
_OUTSIDE = {
    "postgresql": {
        "frac": "1.50",
        "exp": "1e2",
        "huge": "12345678901234567890",
        "mz": "-0.0",
    },
    "sqlite": {
        "frac": "1.5",
        "exp": "100.0",
        "huge": "1.23456789012346e+19",
        "mz": "0.0",
    },
}


def _pg_url() -> str:
    return worker_env_values(REPO / ".env.test")["DATABASE_URL"]


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[AsyncEngine, None]:
    engine = driver.build_disposable_engine(_pg_url())
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'slice9.db'}")
    yield engine
    await engine.dispose()


async def _json_path_answers(engine: AsyncEngine) -> dict[tuple[Any, ...], Any]:
    table = _docs()
    answers: dict[tuple[Any, ...], Any] = {}
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(table.metadata.create_all)
        await connection.execute(
            table.insert(), [{"id": i, "doc": doc} for i, doc in _ROWS.items()]
        )
        raw: Any = sa.literal(_RAW, sa.Text())
        if connection.dialect.name == "postgresql":
            raw = sa.cast(raw, sa.JSON())  # json keeps the text as written
        await connection.execute(table.insert().values(id=_RAW_ID, doc=raw))
        for key in _OUTSIDE[connection.dialect.name]:
            read = await connection.execute(
                sa.select(json_path(table.c.doc, key)).where(table.c.id == _RAW_ID)
            )
            answers[("outside", key)] = read.scalar_one()
        for path, _expected in _PATHS:
            rows = await connection.execute(
                sa.select(table.c.id, json_path(table.c.doc, *path)).order_by(
                    table.c.id
                )
            )
            answers[path] = dict(rows.all())
        hit = await connection.execute(
            sa.select(table.c.id).where(json_path(table.c.doc, "s") == "DE")
        )
        answers[("where",)] = hit.scalars().all()
        await transaction.rollback()
    return answers


def _expected(dialect: str) -> dict[tuple[Any, ...], Any]:
    outside = {("outside", k): v for k, v in _OUTSIDE[dialect].items()}
    return {**dict(_PATHS), ("where",): [2], **outside}


@pytest.mark.asyncio
async def test_json_path_reads_the_same_scalars_on_postgresql(pg: AsyncEngine) -> None:
    assert await _json_path_answers(pg) == _expected("postgresql")


@pytest.mark.asyncio
async def test_json_path_reads_the_same_scalars_on_sqlite(lite: AsyncEngine) -> None:
    assert await _json_path_answers(lite) == _expected("sqlite")


def test_numbers_outside_the_contract_read_differently() -> None:
    """The contract excludes them because the texts differ: if the databases
    ever agree, the documentation can widen."""
    pg, lite = _OUTSIDE["postgresql"], _OUTSIDE["sqlite"]
    assert all(pg[key] != lite[key] for key in pg)


@pytest.mark.asyncio
async def test_arrays_fulltext_and_containment_run_on_postgresql(
    pg: AsyncEngine,
) -> None:
    table = _items()
    ids = sa.Table(
        "slice9_ids",
        table.metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("refs", Array(sa.Uuid())),
    )
    ref = uuid.uuid4()
    async with pg.connect() as connection:
        transaction = await connection.begin()
        await connection.run_sync(table.metadata.create_all)
        await connection.execute(
            table.insert(),
            [
                {
                    "id": 1,
                    "tags": ["red", "blue"],
                    "title": "Cats and dogs",
                    "body": "The cats are sleeping",
                    "doc": {"tags": ["gift", "fragile"], "n": 1},
                },
                {
                    "id": 2,
                    "tags": [],
                    "title": "Birds",
                    "body": "A dog barks at birds",
                    "doc": {"tags": ["fragile"]},
                },
            ],
        )
        await connection.execute(ids.insert(), [{"id": 1, "refs": [ref]}])

        async def ids_where(*conditions: Any) -> list[int]:
            rows = await connection.execute(
                sa.select(table.c.id).where(*conditions).order_by(table.c.id)
            )
            return list(rows.scalars().all())

        tags = (await connection.execute(sa.select(table.c.tags))).scalars().all()
        assert sorted(tags) == [[], ["red", "blue"]]
        assert await ids_where(sa.literal("blue") == sa.any_(table.c.tags)) == [1]
        text = [table.c.title, table.c.body]
        assert await ids_where(matches(text, "cat")) == [1]  # stemmed
        assert await ids_where(matches(text, "dog")) == [1, 2]
        assert await ids_where(matches(text, "dog -cats")) == [2]
        assert await ids_where(json_contains(table.c.doc, {"tags": ["gift"]})) == [1]
        assert await ids_where(json_contains(table.c.doc, {"tags": ["fragile"]})) == [
            1,
            2,
        ]
        refs = (await connection.execute(sa.select(ids.c.refs))).scalar_one()
        assert refs == [ref]
        indexes = await connection.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = "
                "'ix_slice9_items_text'"
            )
        )
        assert "USING gin" in indexes.scalar_one()
        await transaction.rollback()


@pytest.mark.asyncio
async def test_sqlite_refuses_an_array_table(lite: AsyncEngine) -> None:
    with pytest.raises(UnsupportedSqlForm, match="arrays"):
        async with lite.begin() as connection:
            await connection.run_sync(_items().metadata.create_all)


# -- the requirement at startup (SANDLOOM-B033) ---------------------------------


class _Base(DeclarativeBase):
    pass


class _Tagged(_Base):
    __tablename__ = "slice9mod_tagged"

    id: Mapped[int] = mapped_column(primary_key=True)
    tags: Mapped[list[str]] = mapped_column(Array(sa.String(20)))
    title: Mapped[str] = mapped_column(sa.String(80))
    __table_args__ = (fulltext_index("ix_slice9mod_tagged_text", "title"),)


# As if declared in the module package ``slice9mod`` (its models file).
_Tagged.__module__ = "slice9mod.models"


@pytest.fixture
def module_models(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The module's models on their own base: the application's
    ``Base.metadata`` (built on SQLite by other tests) never sees them."""
    monkeypatch.setattr(orm, "Base", _Base)
    yield


def _core(url: str) -> CoreFacade:
    reg = InfraRegistry(
        config={
            "database": {
                "default": InstanceConfig(
                    capability="database",
                    name="default",
                    driver="sqlalchemy",
                    options={"url": url},
                )
            }
        },
        strict_capabilities=True,
    )
    reg.register_capability(DATABASE_CAPABILITY)
    reg.register_driver(
        "database",
        "sqlalchemy",
        create_sqlalchemy_database,
        features=SQLALCHEMY_DB_FEATURES,
        config_schema=SQLAlchemyDatabaseOptions,
        instance_features=sqlalchemy_db_features,
    )
    core = CoreFacade(app=FastAPI())
    core.set_infra(reg)
    return core


_LITE_URL = "sqlite+aiosqlite:///never-opened-slice9.db"
_PG_URL = "postgresql+asyncpg://u:p@localhost:1/never_opened_slice9"


def test_module_feature_uses_are_derived_from_its_tables(module_models: None) -> None:
    assert orm.module_feature_uses("slice9mod") == [
        ("Array", "arrays", "slice9mod_tagged.tags"),
        (
            "fulltext_index",
            "fulltext_search",
            "slice9mod_tagged.ix_slice9mod_tagged_text",
        ),
    ]
    assert orm.module_feature_uses("slice9") == []  # a prefix is not the package


def test_a_module_using_array_is_refused_on_sqlite(module_models: None) -> None:
    desc = ModuleDescriptor(name="slice9mod", version="1", capabilities=[])
    with pytest.raises(DatabaseFeatureMissing) as caught:
        _preflight_capabilities(_core(_LITE_URL), desc)
    message = str(caught.value)
    assert caught.value.sandloom_code == "SANDLOOM-B033"
    for part in (
        "'slice9mod'",
        "Array (slice9mod_tagged.tags) needs the feature 'arrays'",
        "fulltext_index (slice9mod_tagged.ix_slice9mod_tagged_text) needs the "
        "feature 'fulltext_search'",
        "database instance 'default'",
        "[SANDLOOM-B033]",
    ):
        assert part in message


def test_a_module_using_array_loads_on_postgresql(module_models: None) -> None:
    desc = ModuleDescriptor(name="slice9mod", version="1", capabilities=[])
    _preflight_capabilities(_core(_PG_URL), desc)  # no raise


def test_a_declared_feature_is_refused_with_the_same_code() -> None:
    desc = ModuleDescriptor(
        name="slice9decl",
        version="1",
        capabilities=[
            CapabilityRequirement("database", features=frozenset({"json_contains"}))
        ],
    )
    with pytest.raises(DatabaseFeatureMissing, match="declared in capabilities="):
        _preflight_capabilities(_core(_LITE_URL), desc)
    _preflight_capabilities(_core(_PG_URL), desc)
    optional = ModuleDescriptor(
        name="slice9decl",
        version="1",
        capabilities=[
            CapabilityRequirement(
                "database", features=frozenset({"json_contains"}), required=False
            )
        ],
    )
    _preflight_capabilities(_core(_LITE_URL), optional)  # only warns


def test_sqlite_serves_a_module_declaring_json_path_and_transaction_locks() -> None:
    desc = ModuleDescriptor(
        name="slice9lite",
        version="1",
        capabilities=[
            CapabilityRequirement(
                "database", features=frozenset({"json_path", "transaction_locks"})
            )
        ],
    )
    _preflight_capabilities(_core(_LITE_URL), desc)


# -- the static rule (SANDLOOM-C030) ------------------------------------------


def _module(tmp_path: Path, **files: str) -> list[Path]:
    paths = []
    for name, source in files.items():
        path = tmp_path / f"{name}.py"
        path.write_text(source)
        paths.append(path)
    return paths


_DECLARED = (
    "from sandloom.sdk import define_module\n"
    "from sandloom.sdk.module import CapabilityRequirement\n"
    "register = define_module(__name__, capabilities=["
    'CapabilityRequirement("database", features=frozenset({"arrays"}))])\n'
)


def _severities(files: list[Path]) -> list[str]:
    return [severity for _, _, severity in find_database_feature_problems(files)]


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("from sandloom.sdk.data import Array\nc = Array(String())\n", []),
        (
            "from sandloom.sdk.data import json_path\nc = json_path(T.doc, 'a')\n",
            ["error"],
        ),
        (
            "from sandloom.sdk.data import json_path as jp\nc = jp(T.doc, 'a')\n",
            ["error"],
        ),
        ("import sandloom.sdk.data as d\nc = d.matches(T.t, 'x')\n", ["error"]),
        ("from sandloom.sdk import data\nc = data.json_contains(T.d, {})\n", ["error"]),
        (
            "from sandloom.sdk.data import fulltext_index\ni = fulltext_index('i', 'a')\n",
            ["error"],
        ),
        # Referenced, not called: still a use.
        ("import sandloom.sdk.data as d\nlock = d.json_path\n", ["error"]),
        (
            "from sandloom.sdk.data import json_contains\nF = (json_contains,)\n",
            ["error"],
        ),
        # A * import hides the forms it brings.
        ("from sandloom.sdk.data import *\n", ["error"]),
        # transaction_lock warns for one minor release.
        (
            "from sandloom.sdk.data import transaction_lock\n"
            "async def f():\n    await transaction_lock(1)\n",
            ["warning"],
        ),
        ("import sandloom.sdk.data as d\nlock = d.transaction_lock\n", ["warning"]),
        ("from other import json_path\nc = json_path(T.doc, 'a')\n", []),
        ("from sandloom.sdk.data import db\nc = db()\n", []),
    ],
)
def test_the_c030_analyzer(tmp_path: Path, source: str, found: list[str]) -> None:
    files = _module(tmp_path, __init__=_DECLARED, models=source)
    assert _severities(files) == found, source


def test_the_transaction_lock_warning_says_it_becomes_an_error(tmp_path: Path) -> None:
    uses = "from sandloom.sdk.data import transaction_lock\nf = transaction_lock\n"
    (hit,) = find_database_feature_problems(
        _module(tmp_path, __init__=_DECLARED, models=uses)
    )
    assert "an error from the next minor release" in hit[1]


def _declares(entry: str) -> str:
    return f"register = define_module(__name__, capabilities=[{entry}])\n"


def test_the_c030_analyzer_reads_every_declaration_shape(tmp_path: Path) -> None:
    uses = "from sandloom.sdk.data import json_path\nc = json_path(T.doc, 'a')\n"
    for declared in (
        _declares('CapabilityRequirement("database", features={"json_path"})'),
        _declares('CapabilityRequirement("database", features=["json_path"])'),
        _declares(
            'CapabilityRequirement(capability="database", features=("json_path",))'
        ),
        _declares('CapabilityRequirement("database", features=set(["json_path"]))'),
        # A module-level constant, an entry or the whole list.
        'DB = CapabilityRequirement("database", features={"json_path"})\n'
        + _declares('DB, "cache"'),
        'CAPS = [CapabilityRequirement("database", features={"json_path"})]\n'
        "register = define_module(__name__, capabilities=CAPS)\n",
        # The older descriptor form.
        "d = ModuleDescriptor(name='m', version='1', capabilities=["
        'CapabilityRequirement("database", features={"json_path"})])\n',
    ):
        files = _module(tmp_path, __init__=declared, models=uses)
        assert find_database_feature_problems(files) == [], declared
    for other in (
        _declares('CapabilityRequirement("cache", features={"json_path"})'),
        _declares('CapabilityRequirement("database", instance="x")'),
        # A requirement outside define_module declares nothing.
        'x = [CapabilityRequirement("database", features={"json_path"})]\n',
    ):
        files = _module(tmp_path, __init__=other, models=uses)
        (hit,) = find_database_feature_problems(files)
        assert "json_path needs the database feature 'json_path'" in hit[1]
        assert 'features=frozenset({"json_path"})' in hit[1]


@pytest.mark.parametrize(
    "declared",
    [
        _declares('CapabilityRequirement("database", features=FEATURES)'),
        "register = define_module(__name__, capabilities=build())\n",
        _declares("make_requirement()"),
    ],
)
def test_an_unreadable_declaration_is_a_finding(tmp_path: Path, declared: str) -> None:
    uses = "from sandloom.sdk.data import json_path\nc = json_path(T.doc, 'a')\n"
    found = find_database_feature_problems(
        _module(tmp_path, __init__=declared, models=uses)
    )
    messages = [message for _, message, _ in found]
    assert any("cannot be read statically" in m for m in messages), messages
    assert any("json_path needs the database feature" in m for m in messages)
