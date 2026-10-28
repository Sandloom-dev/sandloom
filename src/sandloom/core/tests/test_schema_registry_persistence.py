from __future__ import annotations

import json
from pathlib import Path

from sandloom.core.schema.registry import InMemorySchemaRegistry as SchemaRegistry


def test_schema_registry_persistence_round_trip(tmp_path: Path) -> None:
    """Full write -> load round trip produces identical registry state."""
    path = tmp_path / "schemas.json"
    reg = SchemaRegistry()
    reg.configure_persistence(str(path))

    reg.register(
        "identity.events.user_created.v1",
        "user_created",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )
    reg.register("identity.events.user_deleted.v1", "user_deleted", None)

    assert path.exists(), "Persistence file not created"
    data = json.loads(path.read_text())
    assert len(data) == 2
    channels = {item["channel"] for item in data}
    assert "identity.events.user_created.v1" in channels
    assert "identity.events.user_deleted.v1" in channels

    fresh = SchemaRegistry()
    fresh.configure_persistence(str(path))
    fresh.load_from_disk()

    loaded = {e.channel: e for e in fresh.list()}
    assert "identity.events.user_created.v1" in loaded
    assert loaded["identity.events.user_created.v1"].schema == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    assert "identity.events.user_deleted.v1" in loaded
    assert loaded["identity.events.user_deleted.v1"].schema is None


def test_schema_registry_persistence_ignores_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "schemas_invalid.json"
    # Malformed: second entry lacks channel key; first missing name (should fallback)
    path.write_text(
        json.dumps(
            [
                {"channel": "identity.events.bad.v1"},
                {"nope": 1},
            ]
        )
    )

    fresh = SchemaRegistry()
    fresh.configure_persistence(str(path))
    fresh.load_from_disk()  # Should not raise

    entries = fresh.list()
    assert len(entries) == 1
    assert entries[0].channel == "identity.events.bad.v1"
    assert entries[0].name == "identity.events.bad.v1"
