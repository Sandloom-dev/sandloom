"""What the DATABASE does with a plain INSERT that the cursor guard cannot see
(ADR-0018, decision 7): three model shapes are refused when the model is defined.

The guard keeps a statement's tenant values and filters inside the bound tenant.
An INSERT whose every value is the bound tenant's passes it, and the database can
still reach another tenant's row:

* SANDLOOM-R019: a SQLite ``ON CONFLICT REPLACE`` clause on a key deletes the row
  that holds the key, whoever owns it, before anything else is asked;
* SANDLOOM-R020: a foreign key to another TenantScoped table that leaves
  ``tenant_id`` out accepts, locks and probes a parent row of another tenant;
* SANDLOOM-R021: a tenant column type that processes the value (a
  ``TypeDecorator``) writes a tenant other than the one the guard bound.

Every way to declare one is covered, each in a fresh ``DeclarativeBase`` so no
table of one case is another's parent: a constraint in ``__table_args__``, a
column keyword, ``mapped_column`` and ``Column``, a mixin, ``declared_attr``,
joined inheritance, imperative mapping, a ``Table`` reused by two models, a
parent defined after its child, a constraint or column added to the table after
the model, and the controls that must NOT be refused. The behaviour tests run on
SQLite and on PostgreSQL: they show the hazard is real on a plain table and that
the shape the error asks for holds under the guard.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    event,
    insert,
    select,
    text,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    column_property,
    declared_attr,
    mapped_column,
    registry,
)
from sqlalchemy.types import UserDefinedType

from sandloom.core.config.settings import settings
from sandloom.core.di import UnsafeTenantOperationError, bind_tenant, unscoped
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.tenant import UnsafeTenantModelError
from sandloom.core.testing import reload_test_env

#: A case defines its models in the base and the suffix it is given.
Define = Callable[[Any, str], None]

_cases = itertools.count()


def _fresh() -> tuple[Any, str]:
    """A declarative base and a suffix of its own: tables never meet across cases."""

    class Base(DeclarativeBase):
        pass

    return Base, str(next(_cases))


def _refused(define: Define) -> UnsafeTenantModelError:
    base, suffix = _fresh()
    with pytest.raises(UnsafeTenantModelError) as caught:
        define(base, suffix)
    assert isinstance(caught.value, UnsafeTenantOperationError)
    return caught.value


# --------------------------------------------------- the types a tenant column can take


class _Prefixed(TypeDecorator[uuid.UUID]):
    """Stores the tenant as ``t-<hex>``: the value in the database is not the value
    the guard bound."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else "t-" + value.hex

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        return None if value is None else uuid.UUID(value[2:])


class _OverUuid(TypeDecorator[uuid.UUID]):
    """Wraps SQLAlchemy's own ``Uuid``: still a type that may process the value."""

    impl = Uuid
    cache_ok = True


class _SubclassedUuid(Uuid):  # type: ignore[type-arg]
    pass


class _Opaque(UserDefinedType):  # type: ignore[type-arg]
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "CHAR(36)"


# ----------------------------------- SANDLOOM-R019: a REPLACE conflict clause, every form


def _replace_in_primary_key_constraint(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_pk_{t}"
        __table_args__ = (PrimaryKeyConstraint("id", sqlite_on_conflict="REPLACE"),)

        id: Mapped[int] = mapped_column(Integer)


def _replace_in_unique_constraint(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_uq_{t}"
        __table_args__ = (UniqueConstraint("email", sqlite_on_conflict="REPLACE"),)

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(Text)


def _replace_in_column_unique(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_cu_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(
            Text, unique=True, sqlite_on_conflict_unique="REPLACE"
        )


def _replace_in_column_primary_key(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_cp_{t}"

        id: Mapped[int] = mapped_column(
            primary_key=True, sqlite_on_conflict_primary_key="REPLACE"
        )


def _replace_in_column_not_null(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_nn_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(
            Text, nullable=False, default="x", sqlite_on_conflict_not_null="REPLACE"
        )


def _replace_spelled_in_lower_case_on_a_classic_column(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_lc_{t}"

        id = Column(Integer, primary_key=True)
        email = Column(Text, unique=True, sqlite_on_conflict_unique="replace")


def _replace_in_a_mixin_column(base: Any, t: str) -> None:
    class Mixin:
        email = mapped_column(Text, unique=True, sqlite_on_conflict_unique="REPLACE")

    class M(Mixin, TenantScoped, base):
        __tablename__ = f"tgap_mx_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)


def _replace_in_a_declared_attr_table_args(base: Any, t: str) -> None:
    class Mixin:
        @declared_attr.directive
        def __table_args__(cls) -> tuple[Any, ...]:
            return (UniqueConstraint("email", sqlite_on_conflict="REPLACE"),)

    class M(Mixin, TenantScoped, base):
        __tablename__ = f"tgap_da_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(Text)


def _replace_on_the_table_of_a_joined_subclass(base: Any, t: str) -> None:
    class Parent(TenantScoped, base):
        __tablename__ = f"tgap_jp_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)

    class Child(Parent):
        __tablename__ = f"tgap_jc_{t}"

        id: Mapped[int] = mapped_column(ForeignKey(f"tgap_jp_{t}.id"), primary_key=True)
        email: Mapped[str] = mapped_column(
            Text, unique=True, sqlite_on_conflict_unique="REPLACE"
        )


def _replace_on_an_imperatively_mapped_table(base: Any, t: str) -> None:
    table = Table(
        f"tgap_im_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
        Column("email", Text),
        UniqueConstraint("email", sqlite_on_conflict="REPLACE"),
    )

    class M(TenantScoped):
        pass

    registry().map_imperatively(M, table)


def _replace_on_a_table_a_plain_model_mapped_first(base: Any, t: str) -> None:
    """One ``Table`` under two models: the plain one is no tenant model and is not
    refused, the TenantScoped one mapped over the same table is."""
    table = Table(
        f"tgap_re_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
        Column("email", Text, unique=True, sqlite_on_conflict_unique="REPLACE"),
    )

    class Plain:
        pass

    class Scoped(TenantScoped):
        pass

    reg = registry()
    reg.map_imperatively(Plain, table)
    reg.map_imperatively(Scoped, table)


def _replace_on_a_declarative_table_object(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __table__ = Table(
            f"tgap_dt_{t}",
            base.metadata,
            Column("id", Integer, primary_key=True),
            Column("tenant_id", Uuid, nullable=False),
            Column("email", Text),
            UniqueConstraint("email", sqlite_on_conflict="REPLACE"),
        )


REPLACE_FORMS: dict[str, Define] = {
    "primary key constraint": _replace_in_primary_key_constraint,
    "unique constraint": _replace_in_unique_constraint,
    "column unique": _replace_in_column_unique,
    "column primary key": _replace_in_column_primary_key,
    "column not null": _replace_in_column_not_null,
    "lower case, classic Column": _replace_spelled_in_lower_case_on_a_classic_column,
    "mixin column": _replace_in_a_mixin_column,
    "declared_attr table args": _replace_in_a_declared_attr_table_args,
    "joined subclass table": _replace_on_the_table_of_a_joined_subclass,
    "imperative mapping": _replace_on_an_imperatively_mapped_table,
    "table reused by a plain model": _replace_on_a_table_a_plain_model_mapped_first,
    "declarative __table__": _replace_on_a_declarative_table_object,
}


@pytest.mark.parametrize("define", REPLACE_FORMS.values(), ids=REPLACE_FORMS)
def test_a_replace_clause_is_refused_when_the_model_is_defined(define: Define) -> None:
    error = _refused(define)
    assert error.sandloom_code == "SANDLOOM-R019"
    assert "REPLACE" in str(error)
    assert "sandloom explain SANDLOOM-R019" in str(error)


def _a_clean_tenant_model(base: Any, t: str) -> Any:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_late_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(Text)

    table = M.__table__
    assert isinstance(table, Table)
    return table


@pytest.mark.parametrize(
    "attach",
    [
        pytest.param(
            lambda table: table.append_constraint(
                UniqueConstraint("email", sqlite_on_conflict="REPLACE")
            ),
            id="constraint",
        ),
        pytest.param(
            lambda table: table.append_column(
                Column("slug", Text, unique=True, sqlite_on_conflict_unique="REPLACE")
            ),
            id="column",
        ),
    ],
)
def test_a_replace_clause_added_to_the_table_afterwards_is_refused(
    attach: Callable[[Table], None],
) -> None:
    base, suffix = _fresh()
    table = _a_clean_tenant_model(base, suffix)
    with pytest.raises(UnsafeTenantModelError) as caught:
        attach(table)
    assert caught.value.sandloom_code == "SANDLOOM-R019"


@pytest.mark.parametrize("clause", ["IGNORE", "ABORT", "FAIL", "ROLLBACK"])
def test_a_clause_that_never_deletes_a_row_is_not_refused(clause: str) -> None:
    """The refusal is exact: only a clause that deletes the row holding the key."""
    base, suffix = _fresh()

    class M(TenantScoped, base):
        __tablename__ = f"tgap_ok_{suffix}"
        __table_args__ = (UniqueConstraint("email", sqlite_on_conflict=clause),)

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(Text)


def test_a_composite_natural_key_is_not_refused() -> None:
    base, suffix = _fresh()

    class M(TenantScoped, base):
        __tablename__ = f"tgap_nk_{suffix}"
        __table_args__ = (
            UniqueConstraint("tenant_id", "email"),
            Index("ix_tgap_nk_email", "email"),
        )

        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(Text)


# ------------------------ SANDLOOM-R020: a foreign key between tenant tables, every form


def _tenant_parent(base: Any, t: str) -> Any:
    class Parent(TenantScoped, base):
        __tablename__ = f"tgap_p_{t}"
        __table_args__ = (UniqueConstraint("tenant_id", "id"),)

        id: Mapped[int] = mapped_column(primary_key=True)
        a: Mapped[int] = mapped_column(Integer, default=0)
        b: Mapped[int] = mapped_column(Integer, default=0)

    return Parent


def _fk_on_a_column(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"tgap_p_{t}.id"))


def _fk_whose_parent_is_defined_second(base: Any, t: str) -> None:
    """The child names a table that does not exist yet: it is the PARENT's
    definition that is refused, so the order of the model files decides nothing."""

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"tgap_p_{t}.id"))

    _tenant_parent(base, t)


def _fk_in_a_constraint(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"
        __table_args__ = (ForeignKeyConstraint(["parent_id"], [f"tgap_p_{t}.id"]),)

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(Integer)


def _composite_fk_without_the_tenant(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"
        __table_args__ = (
            ForeignKeyConstraint(["a", "b"], [f"tgap_p_{t}.a", f"tgap_p_{t}.b"]),
        )

        id: Mapped[int] = mapped_column(primary_key=True)
        a: Mapped[int] = mapped_column(Integer)
        b: Mapped[int] = mapped_column(Integer)


def _composite_fk_that_crosses_the_tenant_with_another_column(
    base: Any, t: str
) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"
        __table_args__ = (
            ForeignKeyConstraint(
                ["tenant_id", "parent_id"], [f"tgap_p_{t}.id", f"tgap_p_{t}.tenant_id"]
            ),
        )

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(Integer)


def _fk_in_a_mixin_column(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Mixin:
        parent_id = mapped_column(ForeignKey(f"tgap_p_{t}.id"))

    class Child(Mixin, TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)


def _fk_on_a_classic_column(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, ForeignKey(f"tgap_p_{t}.id"))


def _fk_to_its_own_table(base: Any, t: str) -> None:
    class Node(TenantScoped, base):
        __tablename__ = f"tgap_node_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        up_id: Mapped[int | None] = mapped_column(ForeignKey(f"tgap_node_{t}.id"))


def _fk_that_spells_the_table_in_upper_case(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"TGAP_P_{t}.id"))


def _fk_to_a_column_object(base: Any, t: str) -> None:
    parent = _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(parent.__table__.c.id))


def _fk_that_names_a_schema(base: Any, t: str) -> None:
    _tenant_parent(base, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"main.tgap_p_{t}.id"))


def _fk_to_a_parent_of_another_metadata(base: Any, t: str) -> None:
    other, _ = _fresh()
    _tenant_parent(other, t)

    class Child(TenantScoped, base):
        __tablename__ = f"tgap_c_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"tgap_p_{t}.id"))


def _fk_between_imperatively_mapped_tables(base: Any, t: str) -> None:
    metadata = MetaData()
    parent = Table(
        f"tgap_p_{t}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
    )
    child = Table(
        f"tgap_c_{t}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
        Column("parent_id", Integer, ForeignKey(f"tgap_p_{t}.id")),
    )

    class Parent(TenantScoped):
        pass

    class Child(TenantScoped):
        pass

    reg = registry()
    reg.map_imperatively(Parent, parent)
    reg.map_imperatively(Child, child)


def _fk_on_a_table_a_plain_model_mapped_first(base: Any, t: str) -> None:
    _tenant_parent(base, t)
    child = Table(
        f"tgap_c_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
        Column("parent_id", Integer, ForeignKey(f"tgap_p_{t}.id")),
    )

    class Plain:
        pass

    class Scoped(TenantScoped):
        pass

    reg = registry()
    reg.map_imperatively(Plain, child)
    reg.map_imperatively(Scoped, child)


FOREIGN_KEY_FORMS: dict[str, Define] = {
    "column": _fk_on_a_column,
    "parent defined second": _fk_whose_parent_is_defined_second,
    "constraint": _fk_in_a_constraint,
    "composite without tenant": _composite_fk_without_the_tenant,
    "composite crossing the tenant": _composite_fk_that_crosses_the_tenant_with_another_column,
    "mixin column": _fk_in_a_mixin_column,
    "classic Column": _fk_on_a_classic_column,
    "own table": _fk_to_its_own_table,
    "upper case table name": _fk_that_spells_the_table_in_upper_case,
    "column object": _fk_to_a_column_object,
    "schema qualified": _fk_that_names_a_schema,
    "parent of another metadata": _fk_to_a_parent_of_another_metadata,
    "imperative mapping": _fk_between_imperatively_mapped_tables,
    "table reused by a plain model": _fk_on_a_table_a_plain_model_mapped_first,
}


@pytest.mark.parametrize("define", FOREIGN_KEY_FORMS.values(), ids=FOREIGN_KEY_FORMS)
def test_a_foreign_key_that_leaves_the_tenant_out_is_refused_when_defined(
    define: Define,
) -> None:
    error = _refused(define)
    assert error.sandloom_code == "SANDLOOM-R020"
    assert "sandloom explain SANDLOOM-R020" in str(error)
    assert "composite key (tenant_id, " in str(error)


def test_the_error_names_the_composite_key_that_replaces_the_foreign_key() -> None:
    error = _refused(_fk_on_a_column)
    message = str(error)
    assert "ForeignKeyConstraint(['tenant_id', 'parent_id']" in message
    assert "UniqueConstraint('tenant_id', 'id')" in message
    assert message.count("tgap_c_") == 1  # one violation, named once


@pytest.mark.parametrize(
    "attach",
    [
        pytest.param(
            lambda table, parent: table.append_constraint(
                ForeignKeyConstraint(["a"], [f"{parent}.id"])
            ),
            id="constraint",
        ),
        pytest.param(
            lambda table, parent: table.append_column(
                Column("up_id", Integer, ForeignKey(f"{parent}.id"))
            ),
            id="column",
        ),
    ],
)
def test_a_foreign_key_added_to_the_table_afterwards_is_refused(
    attach: Callable[[Table, str], None],
) -> None:
    base, suffix = _fresh()
    _tenant_parent(base, suffix)
    table = _a_clean_tenant_model(base, suffix)
    table.append_column(Column("a", Integer))
    with pytest.raises(UnsafeTenantModelError) as caught:
        attach(table, f"tgap_p_{suffix}")
    assert caught.value.sandloom_code == "SANDLOOM-R020"


def test_the_composite_key_the_error_asks_for_is_not_refused_in_either_order() -> None:
    for parent_first in (True, False):
        base, suffix = _fresh()

        def child(base: Any = base, suffix: str = suffix) -> None:
            class Child(TenantScoped, base):
                __tablename__ = f"tgap_c_{suffix}"
                __table_args__ = (
                    ForeignKeyConstraint(
                        ["tenant_id", "parent_id"],
                        [f"tgap_p_{suffix}.tenant_id", f"tgap_p_{suffix}.id"],
                    ),
                )

                id: Mapped[int] = mapped_column(primary_key=True)
                parent_id: Mapped[int] = mapped_column(Integer)

        if parent_first:
            _tenant_parent(base, suffix)
            child()
        else:
            child()
            _tenant_parent(base, suffix)


def test_a_foreign_key_to_a_table_without_a_tenant_is_not_refused() -> None:
    """A global reference table (countries, currencies) is shared by every tenant."""
    base, suffix = _fresh()

    class Country(base):
        __tablename__ = f"tgap_country_{suffix}"

        code: Mapped[str] = mapped_column(String(2), primary_key=True)

    class Address(TenantScoped, base):
        __tablename__ = f"tgap_addr_{suffix}"

        id: Mapped[int] = mapped_column(primary_key=True)
        country: Mapped[str] = mapped_column(ForeignKey(f"tgap_country_{suffix}.code"))


def test_a_foreign_key_from_a_table_without_a_tenant_is_not_refused() -> None:
    base, suffix = _fresh()
    _tenant_parent(base, suffix)

    class Audit(base):
        __tablename__ = f"tgap_audit_{suffix}"

        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey(f"tgap_p_{suffix}.id"))


def test_joined_inheritance_is_not_refused() -> None:
    """The subclass table keys on the parent's id and carries no tenant of its own:
    the row it joins is the one the same flush wrote."""
    base, suffix = _fresh()

    class Parent(TenantScoped, base):
        __tablename__ = f"tgap_jp_{suffix}"

        id: Mapped[int] = mapped_column(primary_key=True)

    class Child(Parent):
        __tablename__ = f"tgap_jc_{suffix}"

        id: Mapped[int] = mapped_column(
            ForeignKey(f"tgap_jp_{suffix}.id"), primary_key=True
        )


# ------------------------------- SANDLOOM-R021: a tenant column type that processes it


def _type_on_mapped_column(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t1_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(_Prefixed(), nullable=False)


def _type_that_wraps_uuid(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t2_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(_OverUuid(), nullable=False)


def _type_variant_on_one_dialect(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t3_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(
            Uuid().with_variant(_Prefixed(), "sqlite"), nullable=False
        )


def _type_that_subclasses_uuid(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t4_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(_SubclassedUuid(), nullable=False)


def _type_that_is_user_defined(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t5_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(_Opaque(), nullable=False)


def _type_that_is_a_string(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t6_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)


def _type_in_a_mixin(base: Any, t: str) -> None:
    class Mixin:
        tenant_id = mapped_column(_Prefixed(), nullable=False)

    class M(Mixin, TenantScoped, base):
        __tablename__ = f"tgap_t7_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)


def _type_on_a_classic_column(base: Any, t: str) -> None:
    class M(TenantScoped, base):
        __tablename__ = f"tgap_t8_{t}"

        id = Column(Integer, primary_key=True)
        tenant_id = Column(_Prefixed(), nullable=False)


def _type_on_an_imperatively_mapped_table(base: Any, t: str) -> None:
    table = Table(
        f"tgap_t9_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", _Prefixed(), nullable=False),
    )

    class M(TenantScoped):
        pass

    registry().map_imperatively(M, table)


def _type_coerced_by_a_column_property(base: Any, t: str) -> None:
    """The table is plain; the attribute the criterion binds through is not."""
    table = Table(
        f"tgap_t10_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
    )

    class M(TenantScoped):
        pass

    registry().map_imperatively(
        M,
        table,
        exclude_properties=["tenant_id"],
        properties={
            "tenant_id": column_property(type_coerce(table.c.tenant_id, _Prefixed()))
        },
    )


def _type_on_the_table_of_a_joined_subclass(base: Any, t: str) -> None:
    class Parent(TenantScoped, base):
        __tablename__ = f"tgap_t11p_{t}"

        id: Mapped[int] = mapped_column(primary_key=True)

    class Child(Parent):
        __tablename__ = f"tgap_t11c_{t}"

        id: Mapped[int] = mapped_column(
            ForeignKey(f"tgap_t11p_{t}.id"), primary_key=True
        )
        tenant_id = Column("tenant_id", _Prefixed(), nullable=False, key="child_tenant")


def _type_on_a_table_a_plain_model_mapped_first(base: Any, t: str) -> None:
    table = Table(
        f"tgap_t12_{t}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", _Prefixed(), nullable=False),
    )

    class Plain:
        pass

    class Scoped(TenantScoped):
        pass

    reg = registry()
    reg.map_imperatively(Plain, table)
    reg.map_imperatively(Scoped, table)


TYPE_FORMS: dict[str, Define] = {
    "mapped_column TypeDecorator": _type_on_mapped_column,
    "TypeDecorator over Uuid": _type_that_wraps_uuid,
    "variant on one dialect": _type_variant_on_one_dialect,
    "Uuid subclass": _type_that_subclasses_uuid,
    "UserDefinedType": _type_that_is_user_defined,
    "String": _type_that_is_a_string,
    "mixin": _type_in_a_mixin,
    "classic Column": _type_on_a_classic_column,
    "imperative mapping": _type_on_an_imperatively_mapped_table,
    "column_property over type_coerce": _type_coerced_by_a_column_property,
    "joined subclass table": _type_on_the_table_of_a_joined_subclass,
    "table reused by a plain model": _type_on_a_table_a_plain_model_mapped_first,
}


@pytest.mark.parametrize("define", TYPE_FORMS.values(), ids=TYPE_FORMS)
def test_a_tenant_column_type_that_processes_the_value_is_refused_when_defined(
    define: Define,
) -> None:
    error = _refused(define)
    assert error.sandloom_code == "SANDLOOM-R021"
    assert "sandloom explain SANDLOOM-R021" in str(error)
    assert "tenant_id" in str(error)


def test_a_tenant_column_replaced_on_the_table_afterwards_is_refused() -> None:
    base, suffix = _fresh()
    table = _a_clean_tenant_model(base, suffix)
    with pytest.raises(UnsafeTenantModelError) as caught:
        table.append_column(
            Column("tenant_id", _Prefixed(), nullable=False), replace_existing=True
        )
    assert caught.value.sandloom_code == "SANDLOOM-R021"


@pytest.mark.parametrize(
    "column_type",
    [
        Uuid(),
        Uuid(native_uuid=False),
        Uuid(as_uuid=False),
        PgUuid(as_uuid=True),
        Uuid().with_variant(PgUuid(as_uuid=True), "postgresql"),
    ],
    ids=["Uuid", "Uuid text form", "Uuid strings", "postgresql UUID", "plain variant"],
)
def test_the_plain_uuid_spellings_are_not_refused(column_type: Any) -> None:
    base, suffix = _fresh()

    class M(TenantScoped, base):
        __tablename__ = f"tgap_plain_{suffix}"

        id: Mapped[int] = mapped_column(primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(column_type, nullable=False)


# ------------------------- what the refusals close, on SQLite and on PostgreSQL


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[AsyncEngine, None]:
    if request.param == "sqlite":
        built = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gaps.db'}")

        @event.listens_for(built.sync_engine, "connect")
        def _enforce_foreign_keys(connection: Any, record: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

    else:
        url = settings.database_url
        if not url.startswith("postgresql"):
            pytest.fail(f"these tests need a PostgreSQL URL, got {url[:12]!r}")
        built = create_async_engine(url)
    install_write_admission(built)  # the cursor rule every kernel engine carries
    try:
        yield built
    finally:
        await built.dispose()


async def _create(engine: AsyncEngine, metadata: MetaData) -> None:
    with unscoped():  # schema DDL names every table: system work (ADR-0018)
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)


async def _drop(engine: AsyncEngine, metadata: MetaData) -> None:
    with unscoped():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)


@pytest.mark.asyncio
async def test_replace_deletes_the_row_of_another_owner_on_a_plain_table(
    engine: AsyncEngine,
) -> None:
    """The hazard of R019, on a table that is no tenant model: on SQLite the
    insert of the second owner deletes the first owner's row (nothing else in the
    statement mentions it); on PostgreSQL the option is not one and the same
    insert is a unique violation."""
    metadata = MetaData()
    plain = Table(
        "tgap_hazard_replace",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner", Text, nullable=False),
        Column("email", Text, nullable=False),
        UniqueConstraint("email", sqlite_on_conflict="REPLACE"),
    )
    await _create(engine, metadata)
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(plain).values(id=1, owner="b", email="x"))
        if engine.dialect.name == "sqlite":
            async with engine.begin() as conn:
                await conn.execute(insert(plain).values(id=2, owner="a", email="x"))
                rows = (await conn.execute(select(plain.c.owner))).scalars().all()
            assert rows == ["a"]  # b's row is gone
        else:
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(insert(plain).values(id=2, owner="a", email="x"))
    finally:
        await _drop(engine, metadata)


@pytest.mark.asyncio
async def test_a_foreign_key_without_the_tenant_takes_the_parent_of_another_owner(
    engine: AsyncEngine,
) -> None:
    """The hazard of R020, on tables that are no tenant models: a child row of
    owner ``a`` references the parent row of owner ``b`` when the key leaves the
    owner out, and the composite key the error asks for refuses it."""
    metadata = MetaData()
    parents = Table(
        "tgap_hazard_parents",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner", Text, nullable=False),
        UniqueConstraint("owner", "id"),
    )
    plain = Table(
        "tgap_hazard_plain",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner", Text, nullable=False),
        Column("parent_id", Integer, ForeignKey("tgap_hazard_parents.id")),
    )
    composite = Table(
        "tgap_hazard_composite",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner", Text, nullable=False),
        Column("parent_id", Integer),
        ForeignKeyConstraint(
            ["owner", "parent_id"],
            ["tgap_hazard_parents.owner", "tgap_hazard_parents.id"],
        ),
    )
    await _create(engine, metadata)
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(parents).values(id=7, owner="b"))
            await conn.execute(insert(plain).values(id=1, owner="a", parent_id=7))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    insert(composite).values(id=1, owner="a", parent_id=7)
                )
    finally:
        await _drop(engine, metadata)


class _FixBase(DeclarativeBase):
    pass


class _FixParent(TenantScoped, _FixBase):
    """The parent the error's fix names: unique on ``(tenant_id, id)``."""

    __tablename__ = "tgap_fix_parents"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)


class _FixChild(TenantScoped, _FixBase):
    """The composite key the error asks for."""

    __tablename__ = "tgap_fix_children"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["tgap_fix_parents.tenant_id", "tgap_fix_parents.id"],
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(Integer)


@pytest_asyncio.fixture
async def fixed(engine: AsyncEngine) -> AsyncGenerator[AsyncEngine, None]:
    await _create(engine, _FixBase.metadata)
    try:
        yield engine
    finally:
        await _drop(engine, _FixBase.metadata)


@pytest.mark.asyncio
async def test_the_composite_key_holds_under_the_guard(fixed: AsyncEngine) -> None:
    """The ORM flush of a tenant bound to ``a`` cannot reference the parent of ``b``,
    and references its own."""
    maker = async_sessionmaker(fixed, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with maker() as session:
        with unscoped():
            session.add(_FixParent(id=7, tenant_id=tenant_b))
            session.add(_FixParent(id=8, tenant_id=tenant_a))
            await session.commit()
    with pytest.raises(IntegrityError):
        async with maker() as session:
            with bind_tenant(tenant_a):
                session.add(_FixChild(id=1, parent_id=7))
                await session.commit()
    async with maker() as session:
        with bind_tenant(tenant_a):
            session.add(_FixChild(id=2, parent_id=8))
            await session.commit()
    async with maker() as session:
        with unscoped():
            rows = (await session.execute(select(_FixChild.tenant_id))).scalars().all()
    assert rows == [tenant_a]


@pytest.mark.asyncio
async def test_a_tenant_column_type_that_processes_the_value_rewrites_the_tenant(
    engine: AsyncEngine,
) -> None:
    """The hazard of R021, on a table that is no tenant model: the value the guard
    binds is not the value the database stores."""
    metadata = MetaData()
    plain = Table(
        "tgap_hazard_type",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", _Prefixed(), nullable=False),
    )
    await _create(engine, metadata)
    tenant = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(plain).values(id=1, tenant_id=tenant))
            raw = await conn.execute(text("SELECT tenant_id FROM tgap_hazard_type"))
            assert raw.scalars().all() == ["t-" + tenant.hex]
    finally:
        await _drop(engine, metadata)
