"""Bench events (versioned channel; strict validation in prod)."""

from typing import Any

from benchsuite.common import EVENT_CHANNEL

EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    EVENT_CHANNEL: {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
        "additionalProperties": False,
    },
}
