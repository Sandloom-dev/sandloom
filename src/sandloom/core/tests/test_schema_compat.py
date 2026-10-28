from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.contracts import ContractValidationError
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry


def _make_core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


@pytest.mark.asyncio
async def test_add_required_field_same_version_rejected() -> None:
    core = _make_core()
    channel = "compat.test.event.v1"
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(ContractValidationError):
        core.register_event_schema(
            channel=channel,
            name="base",
            schema={
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        )


@pytest.mark.asyncio
async def test_add_optional_field_allowed() -> None:
    core = _make_core()
    channel = "compat.optional.add.v1"
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )
    # Add optional field c (not in required) -> allowed
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "c": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )


@pytest.mark.asyncio
async def test_remove_required_field_rejected() -> None:
    core = _make_core()
    channel = "compat.remove.required.v1"
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(ContractValidationError):
        core.register_event_schema(
            channel=channel,
            name="base",
            schema={
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        )
