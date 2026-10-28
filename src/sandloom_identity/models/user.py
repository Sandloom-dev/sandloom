"""User model."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Index, String, false, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sandloom.sdk import Base, CreatedAt, new_id
from sandloom.sdk.data import UpdatedAt

if TYPE_CHECKING:
    from .external_identity import ExternalIdentity
    from .refresh_token import RefreshToken
    from .twofa_backup_code import TwoFABackupCode
    from .twofa_secret import TwoFASecret
    from .user_role import UserRole


class User(Base):
    """User model with extended identity features."""

    __tablename__ = "users"

    # Primary identification
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(
        String(50), unique=True, nullable=True, index=True
    )

    # Personal information
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Authentication
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Nonce of the single outstanding password-reset token. Rotated on every
    # reset request (a newer link invalidates older ones) and cleared when a
    # reset/change succeeds (a link is single-use).
    password_reset_nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set by an operator (``sandloom security force-password-reset``) after a
    # credential leak: password sign-in is refused until a reset / change.
    password_reset_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Status flags
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Email verification
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="UserRole.user_id",
    )
    external_identities: Mapped[list["ExternalIdentity"]] = relationship(
        "ExternalIdentity", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    twofa_secret: Mapped["TwoFASecret | None"] = relationship(
        "TwoFASecret", back_populates="user", uselist=False
    )
    twofa_backup_codes: Mapped[list["TwoFABackupCode"]] = relationship(
        "TwoFABackupCode", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_created_at", "created_at"),
        Index("ix_users_last_login_at", "last_login_at"),
        # Admin user search (``UserService.list_users``): substring ILIKE on
        # these three columns. pg_trgm GIN serves ``%term%`` for terms of 3+
        # characters; the email is indexed (and searched) as text because a
        # text opclass cannot serve citext's own ILIKE operator.
        Index(
            "ix_users_search_trgm",
            text("full_name gin_trgm_ops"),
            text("(email::text) gin_trgm_ops"),
            text("username gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )
