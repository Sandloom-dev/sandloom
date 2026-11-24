"""The declarative ``Base`` every kernel and module model shares.

Re-exported by the public :mod:`sandloom.core.orm` (models import it from there);
defined at the bottom of the relational layer so the kernel's own tables
(:mod:`sandloom.core.sql_alchemy.models`) do not import the public ORM surface.
"""

from typing import Any, ClassVar

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models.

    ``eager_defaults``: a flush reads back every value the database set
    (``CreatedAt`` on insert, ``UpdatedAt`` on update) with ``RETURNING``, so a
    model's ``created_at`` / ``updated_at`` are loaded right after
    ``flush()``: no ``await db().refresh(row)``. A model that declares its own
    ``__mapper_args__`` (polymorphic inheritance) names ``eager_defaults`` there
    too.
    """

    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}


__all__ = ["Base"]
