"""The declarative testing kit (L1, spec §2.3).

``published(E)`` lists the ``E`` events the code under test published with
``E(...).publish()`` (``published("<channel>")`` every event of a channel),
in publish order, while the ``sandloom.sdk.testing`` plugin records (every test).
``deliver`` and ``fake_contract`` are the typed forms of the L2 testing
primitives (an ``Event`` instance, a generated ``ContractRef``).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, TypeVar

from sandloom.sdk._decl import _recording
from sandloom.sdk._decl.contracts import ContractRef
from sandloom.sdk._decl.events import Event, from_wire, to_wire
from sandloom.sdk._prim.testing import deliver as _deliver
from sandloom.sdk._prim.testing import fake_contract as _fake_contract

E = TypeVar("E", bound=Event)
P = TypeVar("P")


async def deliver(
    event: str | Event,
    payload: dict[str, Any] | None = None,
    *,
    times: int = 1,
    event_id: str | None = None,
    session: Any = None,
    core: Any = None,
) -> int:
    """Deliver one event to every consumer of its channel; returns how many.

    Today's ``deliver_event`` (same parameters and result) that also takes an
    ``Event`` instance. The envelope id is stable across ``times`` (a
    redelivery): a ``durable=True`` consumer applies it once::

        await deliver(NoteCreated(note_id=n, tenant_id=t), times=2)
    """
    return await _deliver(
        event,
        payload,
        times=times,
        event_id=event_id,
        session=session,
        core=core,
    )


def fake_contract(
    ref: ContractRef[P] | str, impl: object
) -> AbstractContextManager[None]:
    """Serve contract ``ref`` from ``impl`` for the block::

    with fake_contract(STATS_QUOTA_V1, AllowAll()):
        response = await client.post(...)
    """
    return _fake_contract(ref, impl)


def published(event: type[E] | str) -> list[E]:
    """The events of ``event``'s channel published by the code under test::

        assert len(published(NoteCreated)) == 1

    With a channel name, every recorded event of that channel.
    """
    recorded: list[Any] | None = _recording.active
    if recorded is None:
        raise RuntimeError(
            "published() reads what the sandloom.sdk.testing plugin records; "
            "activate it: addopts = ['-p', 'sandloom.sdk.testing.plugin']"
        )
    if isinstance(event, str):
        return [e for e in recorded if type(e).channel == event]
    out: list[E] = []
    for item in recorded:
        if type(item).channel != event.channel:
            continue
        if isinstance(item, event):
            out.append(item)
        else:  # another class of the channel (a view): decode like a delivery
            out.append(from_wire(event, to_wire(item)))
    return out


__all__ = ["deliver", "fake_contract", "published"]
