"""The local account of an external identity (issuer + subject)."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from sandloom.sdk import Base, CreatedAt, UtcDateTime, new_id


class ExternalAccount(Base):
    """One person the external issuer signs in, under a stable local id.

    ``roles`` is the platform role set acme_idp derived from the provider's
    groups at the last sign-in: acme_idp is the claim authority of
    ``roles`` / ``permissions`` (and, as the token issuer, of ``email`` /
    ``email_verified``), so its contributor reads them from here.
    """

    __tablename__ = "acme_idp_accounts"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_acme_idp_account_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[CreatedAt]
    signed_out_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


__all__ = ["ExternalAccount"]
