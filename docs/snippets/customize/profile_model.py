"""One profile per user, keyed by the id identity puts in the token."""

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt


class Profile(Base):
    """``Base`` alone: a profile belongs to a user, not to a tenant."""

    __tablename__ = "profiles_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[CreatedAt]
