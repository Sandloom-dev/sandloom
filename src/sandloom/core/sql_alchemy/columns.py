"""The id default and the timestamp columns every model shares.

A model maps them with one line each::

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

``new_id()`` returns a UUIDv7 (RFC 9562): its leading 48 bits are the Unix
time in milliseconds and the next 42 bits a counter that one process keeps
increasing within a millisecond, so ids sort in creation order (no tie, and
B-tree inserts land at the right edge of the index).

``CreatedAt`` / ``UpdatedAt`` are stamped by the database with
``clock_timestamp()``, the time of the statement, not ``now()``: ``now()`` is
the start of the transaction, so every row one transaction writes would get
the same time and a list ordered by it would tie. Alembic autogenerate renders
the default as ``server_default=sa.text("clock_timestamp()")``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, func
from sqlalchemy.orm import mapped_column

#: ``created_at: Mapped[CreatedAt]``: a ``timestamptz NOT NULL`` the database
#: sets with ``clock_timestamp()`` when the row is inserted.
CreatedAt = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    ),
]

#: ``updated_at: Mapped[UpdatedAt]``: as ``CreatedAt``, and set again to
#: ``clock_timestamp()`` by every ORM update of the row.
UpdatedAt = Annotated[
    datetime,
    mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
        nullable=False,
    ),
]

CreatedAt.__doc__ = """A ``timestamptz NOT NULL`` column the database stamps on insert::

    created_at: Mapped[CreatedAt]

Its default is ``clock_timestamp()``, the time of the statement, not
``now()`` (the transaction's start): rows one transaction inserts get
distinct, ordered times. Newest first: ``order_by(M.created_at.desc(),
M.id.desc())``. Combine with column options as usual:
``created_at: Mapped[CreatedAt] = mapped_column(index=True)``.
"""

UpdatedAt.__doc__ = """As ``CreatedAt``, and set again by every ORM update::

    updated_at: Mapped[UpdatedAt]

Both the insert default and the update value are ``clock_timestamp()``.
"""

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
    """
    return _generate()


__all__ = ["CreatedAt", "UpdatedAt", "new_id"]
