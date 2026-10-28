"""Shortlink ORM model.

Table names are prefixed ``links_``. No cross-module foreign keys (this app
has no other modules anyway) — entities owned elsewhere would be referenced by
plain UUID.
"""

import uuid

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, new_id


class Shortlink(Base):
    """A short code that redirects to a target URL."""

    __tablename__ = "links_shortlinks"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, index=True
    )
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    clicks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[CreatedAt]
