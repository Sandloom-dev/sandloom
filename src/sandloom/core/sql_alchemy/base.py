"""The declarative ``Base`` every kernel and module model shares.

Re-exported by the public :mod:`sandloom.core.orm` (models import it from there);
defined at the bottom of the relational layer so the kernel's own tables
(:mod:`sandloom.core.sql_alchemy.models`) do not import the public ORM surface.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


__all__ = ["Base"]
