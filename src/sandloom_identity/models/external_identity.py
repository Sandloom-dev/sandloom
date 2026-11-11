"""External identity model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt
from sandloom_identity.providers import Provider

if TYPE_CHECKING:
    from .user import User


class ExternalIdentity(Base):
    """External identity providers integration."""

    __tablename__ = "external_identities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
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
        # Covered by the unique (provider, external_id) index below, so the
        # kernel's redundant-index check (SANDLOOM-T012) would report it. Kept
        # until dropping it is measured and approved (it was outside the drop
        # set of revision 0007); test_redundant_index_ratchet.py only lets
        # this set shrink.
        Index(
            "ix_external_identities_provider",
            "provider",
            info={"sandloom_known_redundant": "not yet measured for a drop"},
        ),
        Index(
            "uq_external_identities_provider_external_id",
            "provider",
            "external_id",
            unique=True,
        ),
    )


__all__ = ["ExternalIdentity", "Provider"]
