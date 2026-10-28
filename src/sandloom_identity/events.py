"""Identity module events: typed producers of the ``identity.*.v1`` channels.

Identity publishes the whole :class:`BaseEvent` envelope as the bus payload
(the per-event differences live in the nested ``payload`` object and are
documented on each class). Additive-only within v1.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sandloom.sdk import Event


class EventActor(BaseModel):
    """Event actor information."""

    user_id: UUID | None = None
    ip: str | None = Field(default=None, exclude=True)


class BaseEvent(Event):
    """The identity event envelope: every identity channel carries it.

    An abstract base (no channel): each subclass is the producer of its
    channel and is published with ``await event.publish()``.
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    schema_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: EventActor
    payload: dict[str, Any]
    trace_id: UUID | None = None


# User events
class UserRegisteredEvent(BaseEvent, channel="identity.user_registered.v1"):
    """User registered event."""

    event_type: str = "identity.user_registered.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id (no PII: consumers resolve details via "
        "identity's read contracts)",
    )


class UserEmailConfirmedEvent(BaseEvent, channel="identity.user_email_confirmed.v1"):
    """User email confirmed event."""

    event_type: str = "identity.user_email_confirmed.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id",
    )


class UserLoggedInEvent(BaseEvent, channel="identity.user_logged_in.v1"):
    """User logged in event."""

    event_type: str = "identity.user_logged_in.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id, method (password|sso), and optional provider",
    )


class UserUpdatedEvent(BaseEvent, channel="identity.user_updated.v1"):
    """User updated event."""

    event_type: str = "identity.user_updated.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and fields list",
    )


class UserDeletedEvent(BaseEvent, channel="identity.user_deleted.v1"):
    """User deleted event."""

    event_type: str = "identity.user_deleted.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and mode (soft)",
    )


# Role events
class UserRoleAssignedEvent(BaseEvent, channel="identity.user_role_assigned.v1"):
    """User role assigned event."""

    event_type: str = "identity.user_role_assigned.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and role_code",
    )


class UserRoleRevokedEvent(BaseEvent, channel="identity.user_role_revoked.v1"):
    """User role revoked event."""

    event_type: str = "identity.user_role_revoked.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and role_code",
    )


# 2FA events
class TwoFAEnabledEvent(BaseEvent, channel="identity.twofa_enabled.v1"):
    """2FA enabled event."""

    event_type: str = "identity.twofa_enabled.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and type (totp)",
    )


class TwoFADisabledEvent(BaseEvent, channel="identity.twofa_disabled.v1"):
    """2FA disabled event."""

    event_type: str = "identity.twofa_disabled.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id",
    )


class TwoFABackupCodesRegeneratedEvent(
    BaseEvent, channel="identity.twofa_backup_codes_regenerated.v1"
):
    """2FA backup codes regenerated event (security-sensitive)."""

    event_type: str = "identity.twofa_backup_codes_regenerated.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and count",
    )


# SSO events
class SSOLinkedEvent(BaseEvent, channel="identity.sso_linked.v1"):
    """SSO linked event."""

    event_type: str = "identity.sso_linked.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and provider",
    )


class SSOUnlinkedEvent(BaseEvent, channel="identity.sso_unlinked.v1"):
    """SSO unlinked event."""

    event_type: str = "identity.sso_unlinked.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id and provider",
    )


# Session and security events
class UserPasswordResetEvent(BaseEvent, channel="identity.user_password_reset.v1"):
    """User password reset event."""

    event_type: str = "identity.user_password_reset.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id",
    )


class UserLoggedOutEvent(BaseEvent, channel="identity.user_logged_out.v1"):
    """User logged out event."""

    event_type: str = "identity.user_logged_out.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id",
    )


class UserSessionsRevokedEvent(BaseEvent, channel="identity.user_sessions_revoked.v1"):
    """All sessions of a user were revoked (deactivation, role revoke, reset)."""

    event_type: str = "identity.user_sessions_revoked.v1"
    payload: dict[str, Any] = Field(
        ...,
        description="Contains user_id, reason and tokens_valid_after (unix ts)",
    )


# Event factory functions
def create_user_registered_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserRegisteredEvent:
    """Create user registered event.

    It carries no email: event payloads are durable (outbox) and fan out to
    every consumer, so they must not carry PII.
    """
    return UserRegisteredEvent(
        actor=actor,
        payload={"user_id": str(user_id)},
        trace_id=trace_id,
    )


def create_user_email_confirmed_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserEmailConfirmedEvent:
    """Create user email confirmed event."""
    return UserEmailConfirmedEvent(
        actor=actor,
        payload={"user_id": str(user_id)},
        trace_id=trace_id,
    )


def create_user_logged_in_event(
    user_id: UUID,
    method: str,
    provider: str | None = None,
    actor: EventActor = EventActor(),
    trace_id: UUID | None = None,
) -> UserLoggedInEvent:
    """Create user logged in event."""
    payload = {"user_id": str(user_id), "method": method}
    if provider:
        payload["provider"] = provider

    return UserLoggedInEvent(
        actor=actor,
        payload=payload,
        trace_id=trace_id,
    )


def create_user_updated_event(
    user_id: UUID,
    fields: list[str],
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserUpdatedEvent:
    """Create user updated event."""
    return UserUpdatedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "fields": fields},
        trace_id=trace_id,
    )


def create_user_deleted_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserDeletedEvent:
    """Create user deleted event."""
    return UserDeletedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "mode": "soft"},
        trace_id=trace_id,
    )


def create_user_role_assigned_event(
    user_id: UUID,
    role_code: str,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserRoleAssignedEvent:
    """Create user role assigned event."""
    return UserRoleAssignedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "role_code": role_code},
        trace_id=trace_id,
    )


def create_user_role_revoked_event(
    user_id: UUID,
    role_code: str,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserRoleRevokedEvent:
    """Create user role revoked event."""
    return UserRoleRevokedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "role_code": role_code},
        trace_id=trace_id,
    )


def create_twofa_enabled_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> TwoFAEnabledEvent:
    """Create 2FA enabled event."""
    return TwoFAEnabledEvent(
        actor=actor,
        payload={"user_id": str(user_id), "type": "totp"},
        trace_id=trace_id,
    )


def create_twofa_disabled_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> TwoFADisabledEvent:
    """Create 2FA disabled event."""
    return TwoFADisabledEvent(
        actor=actor,
        payload={"user_id": str(user_id)},
        trace_id=trace_id,
    )


def create_sso_linked_event(
    user_id: UUID,
    provider: str,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> SSOLinkedEvent:
    """Create SSO linked event."""
    return SSOLinkedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "provider": provider},
        trace_id=trace_id,
    )


def create_sso_unlinked_event(
    user_id: UUID,
    provider: str,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> SSOUnlinkedEvent:
    """Create SSO unlinked event."""
    return SSOUnlinkedEvent(
        actor=actor,
        payload={"user_id": str(user_id), "provider": provider},
        trace_id=trace_id,
    )


def create_user_password_reset_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserPasswordResetEvent:
    """Create user password reset event."""
    return UserPasswordResetEvent(
        actor=actor,
        payload={"user_id": str(user_id)},
        trace_id=trace_id,
    )


def create_user_logged_out_event(
    user_id: UUID,
    actor: EventActor,
    trace_id: UUID | None = None,
) -> UserLoggedOutEvent:
    """Create user logged out event."""
    return UserLoggedOutEvent(
        actor=actor,
        payload={"user_id": str(user_id)},
        trace_id=trace_id,
    )
