from __future__ import annotations

from typing import TYPE_CHECKING, Any

"""Core baseline event schemas & registration helper (Phase J).

Provides reference schemas for system-level and audit style events. Registered
early during startup; all channels are versioned (".vN").
"""

if TYPE_CHECKING:  # pragma: no cover - import only for typing
    from sandloom.core.facade import CoreFacade

# Individual schema dicts (kept small & stable)
HEARTBEAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instance_id": {"type": "string"},
        "ts": {"type": "string", "format": "date-time"},
    },
    "required": ["instance_id", "ts"],
    "additionalProperties": False,
}

MODULE_LOADED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "module": {"type": "string"},
        "required": {"type": "boolean"},
        "status": {"type": "string", "enum": ["ok", "skipped", "failed"]},
    },
    "required": ["module", "required", "status"],
    "additionalProperties": False,
}

AUDIT_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "actor": {"type": "string"},
        "action": {"type": "string", "enum": ["login", "logout", "update"]},
        "at": {"type": "string"},
        "meta": {"type": "object"},
    },
    "required": ["id", "action", "at"],
    "additionalProperties": True,
}

NOTIFICATION_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": ["email", "system"]},
        "recipient": {"type": "string"},
        "payload": {"type": "object"},
    },
    "required": ["id", "kind", "recipient"],
    "additionalProperties": True,
}

USER_BLOCKED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        "actor_id": {"type": ["string", "null"]},
        "blocked_at": {"type": "integer"},
    },
    "required": ["user_id", "blocked_at"],
    "additionalProperties": False,
}

TOKENS_REVOKED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        # unix seconds, sub-second precision (the mark, skew margin included)
        "valid_after": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["user_id", "valid_after"],
    "additionalProperties": False,
}

_REGISTRATION_PLAN: list[tuple[str, str, dict[str, Any]]] = [
    ("core.system.heartbeat.v1", "heartbeat", HEARTBEAT_SCHEMA),
    ("core.system.module_loaded.v1", "module_loaded", MODULE_LOADED_SCHEMA),
    ("core.audit.v1", "core_audit_event", AUDIT_EVENT_SCHEMA),
    ("core.notification.v1", "core_notification_event", NOTIFICATION_EVENT_SCHEMA),
    # Emitted by sandloom.core.auth.blocks.block_user (emergency user block).
    ("core.auth.user_blocked.v1", "core_auth_user_blocked", USER_BLOCKED_SCHEMA),
    # Durable record of a token revocation (sandloom.core.auth.revocation
    # .revoke_user_tokens_on_commit), applied by the session owner's consumer.
    ("core.auth.tokens_revoked.v1", "core_auth_tokens_revoked", TOKENS_REVOKED_SCHEMA),
]


def register_core_event_schemas(core: CoreFacade) -> None:
    """Register baseline core schemas.

    Registration is idempotent: re-registering an identical schema returns the
    existing entry without error. We deliberately do NOT swallow exceptions —
    a breaking change or an invalid core channel is a framework bug that must
    surface at startup, not be masked.
    """
    for channel, name, schema in _REGISTRATION_PLAN:
        core.register_event_schema(channel=channel, name=name, schema=schema)


__all__ = [
    "register_core_event_schemas",
    "HEARTBEAT_SCHEMA",
    "MODULE_LOADED_SCHEMA",
    "AUDIT_EVENT_SCHEMA",
    "NOTIFICATION_EVENT_SCHEMA",
    "TOKENS_REVOKED_SCHEMA",
]
