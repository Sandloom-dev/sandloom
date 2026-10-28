"""Event payload validation: jsonschema-rs and the pure-Python engine agree.

Both engines sit behind ``compile_payload_validator``; the bus picks the Rust
one when the ``fast`` extra is installed. Verdicts must be identical (draft
detection, formats as annotations), non-JSON payload values and schemas the
Rust engine cannot compile fall back to ``jsonschema``.
"""

from __future__ import annotations

import uuid
from typing import Any

import jsonschema
import pytest

from sandloom.core.contracts import payload_validation as pv

SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "object",
        "required": ["id", "amount"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "amount": {"type": "integer", "minimum": 0},
            "tags": {"type": "array", "items": {"type": "string"}},
            "kind": {"enum": ["a", "b"]},
        },
        "additionalProperties": False,
    },
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"email": {"type": "string", "format": "email"}},
    },
]
PAYLOADS: list[Any] = [
    {"id": str(uuid.uuid4()), "amount": 1},
    {"id": "not-a-uuid", "amount": 1},  # formats are annotations: valid
    {"id": "x", "amount": -1},
    {"amount": 1},
    {"id": "x", "amount": 1, "extra": True},
    {"id": "x", "amount": 1, "tags": ["a", 2]},
    {"id": "x", "amount": 1, "kind": "c"},
    {"email": "nope"},
    {"email": 5},
    [],
]


def _engines() -> list[str]:
    engines = ["python"]
    if pv._rust() is not None:
        engines.append("rust")
    return engines


def _compile(engine: str, schema: dict[str, Any]) -> pv.PayloadValidator:
    if engine == "python":
        return pv._PythonValidator(schema)
    return pv._RustValidator(schema)


@pytest.mark.parametrize("schema", SCHEMAS)
@pytest.mark.parametrize("payload", PAYLOADS)
def test_engines_agree_on_validity(schema: dict[str, Any], payload: Any) -> None:
    verdicts = {
        engine: _compile(engine, schema).error(payload) is None for engine in _engines()
    }
    assert len(set(verdicts.values())) == 1, verdicts


def test_rust_engine_is_used_when_installed() -> None:
    if pv._rust() is None:
        pytest.skip("jsonschema-rs not installed (sandloom[fast])")
    assert pv.compile_payload_validator(SCHEMAS[0]).backend == "jsonschema-rs"
    assert pv.payload_validator_backend() == "jsonschema-rs"


def test_non_json_payload_values_fall_back_to_python() -> None:
    validator = pv.compile_payload_validator(SCHEMAS[0])
    # A UUID object is not a JSON string: invalid for both engines.
    assert validator.error({"id": uuid.uuid4(), "amount": 1}) is not None


def test_invalid_schema_is_reported_like_jsonschema() -> None:
    with pytest.raises(jsonschema.SchemaError):
        pv.compile_payload_validator({"type": 5})


def test_error_message_names_the_failure() -> None:
    message = pv.compile_payload_validator(SCHEMAS[0]).error({"amount": 1})
    assert message is not None and "id" in message


@pytest.mark.asyncio
async def test_post_commit_dispatch_skips_revalidation_on_framework_buses() -> None:
    """Events were preflighted in their transaction: dispatch passes
    ``prevalidated=PREFLIGHTED`` to a framework bus, the plain call to any other."""
    from types import SimpleNamespace

    from sandloom.core.eventing.memory_bus import InMemoryEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry
    from sandloom.core.unit_of_work import dispatch as uow_dispatch

    seen: list[dict[str, Any]] = []

    class _Spy(InMemoryEventBus):
        async def publish_event(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)

    class _Plain:
        async def publish_event(
            self, channel: str, payload: Any, version: str, event_id: str
        ) -> None:
            seen.append({"plain": True})

    event = SimpleNamespace(
        channel="a.b.v1",
        payload={},
        schema_version="v1",
        handle=SimpleNamespace(id=uuid.uuid4()),
    )
    await uow_dispatch._publish_in_order(_Spy(InMemorySchemaRegistry()), [event])  # type: ignore[list-item]
    await uow_dispatch._publish_in_order(_Plain(), [event])  # type: ignore[list-item]
    # PREFLIGHTED: the very snapshot this bus preflighted, size check included.
    assert seen[0]["prevalidated"] == "preflighted"
    assert seen[1] == {"plain": True}
