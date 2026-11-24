"""The declarative ``Base`` every kernel and module model shares.

Re-exported by the public :mod:`sandloom.core.orm` (models import it from there);
defined at the bottom of the relational layer so the kernel's own tables
(:mod:`sandloom.core.sql_alchemy.models`) do not import the public ORM surface.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import DeclarativeBase, Mapper


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models.

    ``eager_defaults``: a flush reads back every value the database set
    (``CreatedAt`` on insert, ``UpdatedAt`` on update) with ``RETURNING``, so a
    model's ``created_at`` / ``updated_at`` are loaded right after
    ``flush()``: no ``await db().refresh(row)``. It holds for every model,
    also one that declares its own ``__mapper_args__`` (polymorphic
    inheritance); a model that names ``eager_defaults=False`` there keeps
    that choice.
    """


@event.listens_for(Base, "instrument_class", propagate=True)
def _eager_defaults(mapper: Mapper[Any], class_: type) -> None:
    """Turn SQLAlchemy's unset ``eager_defaults`` (``"auto"``: read back the
    insert only) into ``True`` for every mapped model. A listener, not a
    ``__mapper_args__`` on ``Base``: a model's own ``__mapper_args__`` replaces
    one inherited from ``Base`` and would lose the setting."""
    if mapper.eager_defaults == "auto":
        mapper.eager_defaults = True


__all__ = ["Base"]
