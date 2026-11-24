"""The id default and the timestamp columns every model shares.

A model maps them with one line each::

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

``new_id()`` returns a UUIDv7 (RFC 9562): its leading 48 bits are the Unix
time in milliseconds and the next 42 bits a counter that one process keeps
increasing within a millisecond, so ids sort in creation order (no tie, and
B-tree inserts land at the right edge of the index).

``CreatedAt`` / ``UpdatedAt`` are stamped by the database's clock at the
statement (:class:`~sandloom.core.sql_alchemy.portable.WallClock`), not at the
start of the transaction: with the transaction's time every row one
transaction writes would get the same time and a list ordered by it would
tie. PostgreSQL writes ``clock_timestamp()`` (and Alembic autogenerate renders
``server_default=sa.text('clock_timestamp()')``); SQLite the UTC time to the
millisecond, where ties are ordered by the UUIDv7 id. Both read back as
timezone-aware UTC times (:class:`~sandloom.core.sql_alchemy.portable.UtcDateTime`).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
import weakref
from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import BigInteger, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapped, Mapper, configure_mappers, mapped_column
from sqlalchemy.sql.schema import CallableColumnDefault

from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.portable import UtcDateTime, WallClock

#: ``created_at: Mapped[CreatedAt]``: a ``timestamptz NOT NULL`` the database
#: sets with its clock (``clock_timestamp()`` on PostgreSQL) when the row is
#: inserted.
CreatedAt = Annotated[
    datetime,
    mapped_column(
        UtcDateTime(),
        server_default=WallClock(),
        nullable=False,
    ),
]

#: ``updated_at: Mapped[UpdatedAt]``: as ``CreatedAt``, and set again to the
#: database's clock by every ORM update of the row.
UpdatedAt = Annotated[
    datetime,
    mapped_column(
        UtcDateTime(),
        server_default=WallClock(),
        onupdate=WallClock(),
        nullable=False,
    ),
]

CreatedAt.__doc__ = """A ``timestamptz NOT NULL`` column the database stamps on insert::

    created_at: Mapped[CreatedAt]

Its default is the database's clock at the statement (``clock_timestamp()``
on PostgreSQL, the UTC time to the millisecond on SQLite), not the
transaction's start: rows one transaction inserts get ordered times, and ties
(SQLite, within a millisecond) are ordered by the UUIDv7 id. It reads back as
a timezone-aware UTC time. Newest first: ``order_by(M.created_at.desc(),
M.id.desc())``. Combine with column options as usual:
``created_at: Mapped[CreatedAt] = mapped_column(index=True)``.
"""

UpdatedAt.__doc__ = """As ``CreatedAt``, and set again by every ORM update::

    updated_at: Mapped[UpdatedAt]

Both the insert default and the update value are the database's clock at
the statement (``clock_timestamp()`` on PostgreSQL).
"""


class Versioned:
    """A ``version: bigint`` column the database counts up: ``1`` on insert,
    plus one by every ORM update of the row, so a module never writes
    ``item.version += 1``::

        class Project(TenantScoped, Versioned, Base):
            ...

        await ProjectSaved(project_id=p.id, version=p.version).publish()

    Read the version after ``flush()``: the new value comes back with the
    UPDATE (``eager_defaults``), so no ``refresh`` is needed. Events carry it
    so a consumer can drop a stale or repeated one. A statement that updates
    no column leaves the version as it was; ``version`` itself is never set by
    hand.
    """

    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, onupdate=text("version + 1")
    )


# --------------------------------------------------------------------------- #
# UUIDv7 (RFC 9562 §5.7, counter method 1 of §6.2: a 42-bit counter seeded at
# random each new millisecond, as CPython 3.14's uuid.uuid7 does).
# --------------------------------------------------------------------------- #

_VERSION_7_FLAGS = (0x7 << 76) | (0x2 << 62)
_COUNTER_MAX = (1 << 42) - 1
_lock = threading.Lock()
_last_ms = -1
_last_counter = 0


def _seed() -> tuple[int, int]:
    """A fresh counter (top bit clear: room to grow) and a 32-bit tail."""
    rand = int.from_bytes(os.urandom(10), "big")
    return (rand >> 32) & (_COUNTER_MAX >> 1), rand & 0xFFFF_FFFF


def _uuid7() -> uuid.UUID:
    global _last_ms, _last_counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            counter, tail = _seed()
        else:
            # The same millisecond, or the clock stepped back: keep counting
            # from the last id so this one still sorts after it.
            ms = _last_ms
            counter = _last_counter + 1
            tail = int.from_bytes(os.urandom(4), "big")
            if counter > _COUNTER_MAX:
                ms += 1
                counter, tail = _seed()
        _last_ms, _last_counter = ms, counter
    value = (ms & 0xFFFF_FFFF_FFFF) << 80
    value |= (counter >> 30) << 64  # rand_a: the counter's 12 high bits
    value |= (counter & 0x3FFF_FFFF) << 32  # rand_b: its 30 low bits, the tail
    value |= tail
    return uuid.UUID(int=value | _VERSION_7_FLAGS)


if sys.version_info >= (3, 14):
    _generate = uuid.uuid7
else:
    _generate = _uuid7


def new_id() -> uuid.UUID:
    """A new time-ordered id (UUIDv7, RFC 9562) for a primary key::

        id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)

    Ids sort in creation order: by time across processes, and strictly
    increasing within one process (a counter orders a millisecond's burst).
    So ``order_by(Model.id)`` is the creation order, with no tie.

    The model has its id the moment it is constructed (``Item(...).id`` is
    set before ``add`` or ``flush``), so an event or a link can name it at
    once; an ``id=`` argument wins.
    """
    return _generate()


# --------------------------------------------------------------------------- #
# The id at construction
# --------------------------------------------------------------------------- #

#: ``{model class: attribute names of its primary key columns whose default is
#: new_id}``, read once per class.
_ID_ATTRIBUTES: weakref.WeakKeyDictionary[type, tuple[str, ...]] = (
    weakref.WeakKeyDictionary()
)


def _id_attributes(cls: type) -> tuple[str, ...]:
    names = _ID_ATTRIBUTES.get(cls)
    if names is None:
        # This listener runs before the mapper's own `init` hook, which is
        # what configures the mappers on a first construction: without this
        # the class has no attribute implementation to set yet.
        configure_mappers()
        mapper: Mapper[Any] = sa_inspect(cls)
        names = tuple(
            mapper.get_property_by_column(column).key
            for column in mapper.primary_key
            # SQLAlchemy wraps a callable default; `__wrapped__` is the function
            # the model named, so only a `default=new_id` column is matched
            if isinstance(column.default, CallableColumnDefault)
            and getattr(column.default.arg, "__wrapped__", None) is new_id
        )
        _ID_ATTRIBUTES[cls] = names
    return names


@event.listens_for(Base, "init", propagate=True)
def _assign_ids(target: object, args: tuple[object, ...], kwargs: object) -> None:
    """Set ``default=new_id`` primary keys before the constructor's own
    arguments, so ``Item(id=...)`` still wins."""
    for name in _id_attributes(type(target)):
        setattr(target, name, _generate())


__all__ = ["CreatedAt", "UpdatedAt", "Versioned", "new_id"]
