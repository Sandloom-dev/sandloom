"""Differential check of the event-schema compatibility rule (G-DECL-01).

On the same ``.vN`` producers and consumers deploy independently, so
``schema_problems(old, new) == []`` claims BOTH directions: a reader of
``new`` accepts every payload a producer of ``old`` may still send (an old
producer, a new consumer), and a reader of ``old`` accepts every payload a
producer of ``new`` may send (a new producer, an old consumer). These tests
hold it to that claim: a corpus of schema pairs -- real pydantic-emitted
wire schemas, each mutated with every narrowing and loosening keyword at
every subschema position and under every applicator, field removals and
models open to extra keys included -- is compared, and for each pair the
rule calls compatible, payloads sampled from ``old`` are validated by the
framework validator (``compile_payload_validator``) of ``new`` and payloads
sampled from ``new`` by that of ``old``: one a producer may send and the
other side's reader rejects is an escape.

The producer model the rule encodes is part of the oracle: a DTO producer
sends only its declared fields (so an added optional field is compatible),
while a dictionary, a model open to extra keys (``extra="allow"``:
``additionalProperties`` true or a schema), or a node below an overlaying
applicator, may hold any key. The spec's one deliberate exception -- a new
defaulted field (literal or factory, marked ``DEFAULTED``) is listed as
``required`` by the serialization schema, yet the reader fills it -- is not
mutated here; it is checked with the real readers (``from_wire``) below and
in ``test_sdk_generated``.

Moved from ``tests/sdk/test_sdk_schema_differential.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations


def test_every_bundled_event_is_in_the_closed_set() -> None:
    """(round 10) the bundled modules' events define (their readers are
    built at definition); the example apps' are checked by their own
    suites in CI (examples job)."""
    import sandloom_billing.events
    import sandloom_identity.events
    import sandloom_tenancy.events
    from sandloom.sdk import Event
    from sandloom.sdk._decl.events import _READERS, is_concrete

    modules = (
        sandloom_billing.events,
        sandloom_identity.events,
        sandloom_tenancy.events,
    )
    events = [
        value
        for module in modules
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, Event) and is_concrete(value)
    ]
    assert len(events) > 30
    assert all(event in _READERS for event in events)
