"""L2 testing primitives of ``sandloom.sdk.testing`` (spec §2.3).

``as_user`` is today's ``make_user_headers`` and ``deliver`` today's
``deliver_event`` under their 1.0 names (identical parameters and results;
``deliver`` also accepts an ``Event`` instance). ``fake_contract`` serves a
contract from a test double for a block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from uuid import UUID

from sandloom.core.testing import (
    deliver_event,
    make_user_headers,
    register_event_schemas,
)
from sandloom.core.testing.services import override_active_service
from sandloom.sdk._prim.contracts import _override


def as_user(
    user_id: UUID | None = None,
    tenant_id: UUID | None = None,
    *,
    email: str | None = "",
    email_verified: bool | None = True,
    tenant: bool = True,
    **claims: Any,
) -> tuple[UUID, dict[str, str]]:
    """``(user_id, headers)`` with a freshly minted access token.

    Today's ``make_user_headers``: ``user_id`` / ``tenant_id`` default to
    random UUIDs; the token carries ``tenant_id`` and the signed
    ``tenant_ids`` allowlist; ``tenant=False`` mints a tenant-less token; any
    extra keyword becomes a claim (``roles=["admin"]``)::

        _, headers = as_user(tenant_id=uuid4())
        await client.post("/api/v1/notes/", json={...}, headers=headers)
    """
    uid, headers = make_user_headers(
        user_id,
        tenant_id,
        email=email,
        email_verified=email_verified,
        tenant=tenant,
        **claims,
    )
    return uid, dict(headers)


def _event_payload(event: Any) -> tuple[str, dict[str, Any]] | None:
    channel = getattr(type(event), "channel", None)
    dump = getattr(event, "model_dump", None)
    if isinstance(channel, str) and callable(dump):
        return channel, dump(mode="json", by_alias=True)
    return None


async def deliver(
    event: Any,
    payload: dict[str, Any] | None = None,
    *,
    times: int = 1,
    event_id: str | None = None,
    session: Any = None,
    core: Any = None,
) -> int:
    """Deliver one event to every consumer of its channel; returns how many.

    ``event`` is an ``Event`` instance (its channel and payload) or a channel
    name with ``payload`` (today's ``deliver_event``). The envelope id is
    stable across ``times`` (a redelivery): a ``durable=True`` consumer
    applies it once::

        await deliver(NoteCreated(note_id=n, tenant_id=t), times=2)
    """
    typed = _event_payload(event)
    if typed is not None:
        if payload is not None:
            raise TypeError("deliver(event_instance) takes no payload")
        channel, payload = typed
    elif isinstance(event, str):
        if payload is None:
            raise TypeError("deliver(channel, payload): the payload is required")
        channel = event
    else:
        raise TypeError(f"deliver() expects an Event or a channel name, not {event!r}")
    try:
        return await deliver_event(
            channel,
            payload,
            times=times,
            event_id=event_id,
            session=session,
            core=core,
        )
    except ValueError as exc:
        # A consumer module tested without the producer module: its typed
        # view stands in for the producer's schema (test-only, never when the
        # producer registered one).
        if typed is None or "Missing schema for versioned channel" not in str(exc):
            raise
    wire = getattr(type(event), "wire_schema", None)
    schema = dict(wire() if callable(wire) else type(event).model_json_schema())
    schema.pop("additionalProperties", None)
    register_event_schemas({channel: schema}, target=core)
    return await deliver_event(
        channel,
        payload,
        times=times,
        event_id=event_id,
        session=session,
        core=core,
    )


def fake_contract(ref: Any, impl: object) -> AbstractContextManager[None]:
    """Serve contract ``ref`` (a ``ContractRef`` or a name) from ``impl``::

    with fake_contract(STATS_QUOTA_V1, AllowAll()):
        response = await client.post(...)
    """
    name = ref if isinstance(ref, str) else getattr(ref, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError(f"fake_contract() expects a ContractRef or a name, not {ref!r}")
    return _fake(name, impl)


@contextmanager
def _fake(name: str, impl: object) -> Iterator[None]:
    # Module code resolves Contract(REF) through the SDK resolver; kernel code
    # (the billing / membership authz atoms, access facts) through
    # core.get_service of the active application: fake both.
    with _override(name, impl), override_active_service(name, impl):
        yield


__all__ = ["as_user", "deliver", "fake_contract"]
