"""Guarantees as a lattice, labels derived from mechanisms (ADR-0010 §6,
spec §6.2 / §6.3; amends ADR-0005's level table).

A guarantee is a delivery set over ``{atomic, no_loss, no_dup}``, closed
under ``atomic ⇒ no_loss ∧ no_dup``, times a visibility ``immediate >
eventual``, plus qualifiers that say how a property holds (``parks_*``: a
delivery may be parked in the DLQ). The meet of two guarantees (what a
combination of effects keeps) is the intersection of the delivery sets, the
minimum visibility and the union of the qualifiers. ``replay-only`` is not a
delivery property: it moved to the response axis (idempotency modes).

Labels are COMPUTED per effect from its mechanism (:func:`effect_label`),
never declared: the configured provider (does the database keep a command
log? is the bus durable?), its windows (does the dedupe TTL cover the
caller's retry horizon? does the inbox outlive stream retention?) and the
effect's boundary (same database as the inbox? a conditional write?).
Nothing is labelled unconditionally. A label is shown only where it deviates
from the full guarantee (:meth:`Guarantee.deviation`).

Pure data: no I/O, no kernel imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ATOMIC = "atomic"
NO_LOSS = "no_loss"
NO_DUP = "no_dup"
DELIVERY = frozenset({ATOMIC, NO_LOSS, NO_DUP})

Visibility = Literal["immediate", "eventual"]
_RANK = {"eventual": 0, "immediate": 1}

#: Qualifiers of ``no_loss`` for consumer delivery (spec §6.3).
PARKS_ON_POISON_OR_BUDGET = "parks_on_poison_or_budget"
PARKS_ON_EXPLICIT_POISON = "parks_on_explicit_poison"
#: ``no_loss`` of a command holds while the caller retries with its key.
CALLER_RETRIES = "caller_retries_with_the_same_key"


@dataclass(frozen=True)
class Guarantee:
    """One point of the lattice (closed on construction)."""

    delivery: frozenset[str]
    visibility: Visibility = "immediate"
    qualifiers: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        unknown = set(self.delivery) - DELIVERY
        if unknown:
            raise ValueError(f"unknown delivery properties {sorted(unknown)}")
        if self.visibility not in _RANK:
            raise ValueError(f"unknown visibility {self.visibility!r}")
        if ATOMIC in self.delivery:  # atomic ⇒ no_loss ∧ no_dup
            object.__setattr__(
                self, "delivery", frozenset(self.delivery | {NO_LOSS, NO_DUP})
            )

    def meet(self, other: Guarantee) -> Guarantee:
        """What holds for both (∩ of deliveries, min visibility, ∪ qualifiers)."""
        visibility: Visibility = (
            self.visibility
            if _RANK[self.visibility] <= _RANK[other.visibility]
            else other.visibility
        )
        return Guarantee(
            self.delivery & other.delivery,
            visibility,
            self.qualifiers | other.qualifiers,
        )

    def __le__(self, other: Guarantee) -> bool:
        """Lattice order: ``self`` is at most as strong as ``other``."""
        return (
            self.delivery <= other.delivery
            and _RANK[self.visibility] <= _RANK[other.visibility]
        )

    def deviation(self) -> str | None:
        """The label shown for this effect, ``None`` for the full guarantee
        (labels are shown only when they deviate, multistore §3 item 6)."""
        missing = sorted(DELIVERY - self.delivery)
        parts = [f"no {name.replace('no_', '')}" for name in missing if name != ATOMIC]
        if ATOMIC in missing and not parts:
            parts.append("not atomic")
        if self.visibility != "immediate":
            parts.append(self.visibility)
        parts.extend(sorted(self.qualifiers))
        return ", ".join(parts) or None


FULL = Guarantee(frozenset({ATOMIC}))


EffectKind = Literal[
    "write",
    "outbox_event",
    "consumer_effect",
    "consumer_effect_other_store",
    "command",
    "on_commit",
    "opted_out_secondary",
]


@dataclass(frozen=True)
class Mechanism:
    """The facts a label is derived from (the configured provider, its
    windows and the effect's boundary). Unset facts are the unsafe answer."""

    #: the event bus is durable (Redis Streams in production), not in-memory
    durable_bus: bool = False
    #: the consumer is registered with ``durable=True`` (inbox claim)
    durable_consumer: bool = False
    #: the inbox lives in the SAME database as the consumer's effect
    inbox_in_effect_database: bool = False
    #: inbox retention >= stream retention + 1 day (G-WINDOW-03 holds)
    inbox_outlives_stream: bool = False
    #: ``dead_letter=True`` on the consumer
    dead_letter: bool = True
    #: the target write of an effect in another store is conditional
    #: (``conditional_write``: version-guarded)
    conditional_write: bool = False
    #: the provider has the ``command_idempotency`` feature (database log)
    command_log: bool = False
    #: the command dedupe TTL covers the caller's retry horizon (G-WINDOW-01)
    dedupe_covers_retries: bool = False
    #: an opted-out secondary write is keyed or version-guarded
    keyed_or_versioned: bool = False
    #: the publish is the intent recorded with the write (always, through
    #: the transactional outbox)
    outbox: bool = True


def _consumer_no_loss(m: Mechanism) -> tuple[set[str], set[str]]:
    delivery: set[str] = set()
    qualifiers: set[str] = set()
    if m.outbox and m.durable_bus:
        delivery.add(NO_LOSS)
        qualifiers.add(
            PARKS_ON_POISON_OR_BUDGET if m.dead_letter else PARKS_ON_EXPLICIT_POISON
        )
    return delivery, qualifiers


def _write(_m: Mechanism) -> Guarantee:
    return FULL  # a write in the write store commits with the unit


def _outbox_event(m: Mechanism) -> Guarantee:
    delivery, qualifiers = _consumer_no_loss(m)  # at-least-once: never no_dup
    return Guarantee(frozenset(delivery), "eventual", frozenset(qualifiers))


def _consumer_effect(m: Mechanism) -> Guarantee:
    delivery, qualifiers = _consumer_no_loss(m)
    if m.durable_consumer and m.inbox_in_effect_database and m.inbox_outlives_stream:
        delivery.add(NO_DUP)
    return Guarantee(frozenset(delivery), "eventual", frozenset(qualifiers))


def _consumer_effect_other_store(m: Mechanism) -> Guarantee:
    delivery, qualifiers = _consumer_no_loss(m)
    if m.conditional_write:
        delivery.add(NO_DUP)
    return Guarantee(frozenset(delivery), "eventual", frozenset(qualifiers))


def _command(m: Mechanism) -> Guarantee:
    delivery = {NO_LOSS}
    if m.command_log and m.dedupe_covers_retries:
        delivery.add(NO_DUP)  # the cache fallback is never no_dup
    return Guarantee(frozenset(delivery), "immediate", frozenset({CALLER_RETRIES}))


def _on_commit(_m: Mechanism) -> Guarantee:
    return Guarantee(frozenset({NO_DUP}), "immediate")  # never re-run nor retried


def _opted_out_secondary(m: Mechanism) -> Guarantee:
    return Guarantee(frozenset({NO_DUP} if m.keyed_or_versioned else set()))


_LABELS = {
    "write": _write,
    "outbox_event": _outbox_event,
    "consumer_effect": _consumer_effect,
    "consumer_effect_other_store": _consumer_effect_other_store,
    "command": _command,
    "on_commit": _on_commit,
    "opted_out_secondary": _opted_out_secondary,
}


def effect_label(kind: EffectKind, mechanism: Mechanism | None = None) -> Guarantee:
    """The guarantee of one effect, derived from its mechanism (spec §6.3)."""
    derive = _LABELS.get(kind)
    if derive is None:
        raise ValueError(f"unknown effect kind {kind!r}")
    return derive(mechanism if mechanism is not None else Mechanism())


__all__ = [
    "ATOMIC",
    "CALLER_RETRIES",
    "DELIVERY",
    "FULL",
    "NO_DUP",
    "NO_LOSS",
    "PARKS_ON_EXPLICIT_POISON",
    "PARKS_ON_POISON_OR_BUDGET",
    "Guarantee",
    "Mechanism",
    "effect_label",
]
