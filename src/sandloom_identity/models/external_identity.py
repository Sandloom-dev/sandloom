"""External identity model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt
from sandloom_identity.providers import Provider

if TYPE_CHECKING:
    from .user import User


class ExternalIdentity(Base):
    """External identity providers integration."""

    __tablename__ = "external_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    provider: Mapped[Provider] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="external_identities")

    __table_args__ = (
        Index("ix_external_identities_user_id", "user_id"),
        Index("ix_external_identities_provider", "provider"),
        Index(
            "uq_external_identities_provider_external_id",
            "provider",
            "external_id",
            unique=True,
        ),
    )


__all__ = ["ExternalIdentity", "Provider"]
