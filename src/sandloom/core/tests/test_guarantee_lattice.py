"""The guarantee lattice and the labels derived from mechanisms (ADR-0010 §6;
spec §6.2, §6.3): the closure and meet laws, and one soundness scenario per
row of the label table (C-MSTORE-03), the cache-fallback and expired-window
cases included."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sandloom.core.guarantee_lattice import (
    ATOMIC,
    CALLER_RETRIES,
    FULL,
    NO_DUP,
    NO_LOSS,
    PARKS_ON_EXPLICIT_POISON,
    PARKS_ON_POISON_OR_BUDGET,
    Guarantee,
    Mechanism,
    effect_label,
)

guarantees = st.builds(
    Guarantee,
    st.frozensets(st.sampled_from([ATOMIC, NO_LOSS, NO_DUP])),
    st.sampled_from(["immediate", "eventual"]),
    st.frozensets(st.sampled_from(["q1", "q2"])),
)


@given(guarantees)
def test_atomic_implies_no_loss_and_no_dup(g: Guarantee) -> None:
    if ATOMIC in g.delivery:
        assert {NO_LOSS, NO_DUP} <= g.delivery


@given(guarantees, guarantees, guarantees)
def test_the_meet_is_a_semilattice_and_never_strengthens(
    a: Guarantee, b: Guarantee, c: Guarantee
) -> None:
    assert a.meet(b) == b.meet(a)
    assert a.meet(b).meet(c) == a.meet(b.meet(c))
    assert a.meet(a) == a
    assert a.meet(b) <= a and a.meet(b) <= b
    assert a.meet(FULL).delivery == a.delivery


def test_a_label_is_shown_only_when_it_deviates() -> None:
    assert FULL.deviation() is None
    assert effect_label("write").deviation() is None
    label = effect_label("command").deviation()
    assert label is not None and "no dup" in label


def test_unknown_properties_are_refused() -> None:
    with pytest.raises(ValueError):
        Guarantee(frozenset({"exactly_once"}))
    with pytest.raises(ValueError):
        effect_label("teleport")  # type: ignore[arg-type]


PROD = Mechanism(
    durable_bus=True,
    durable_consumer=True,
    inbox_in_effect_database=True,
    inbox_outlives_stream=True,
    dead_letter=True,
    command_log=True,
    dedupe_covers_retries=True,
)


def test_each_row_of_the_label_table() -> None:
    # write in the write store: always atomic, immediate
    assert effect_label("write", Mechanism()) == FULL
    # outbox event: at-least-once (never no_dup); no_loss iff durable bus
    event = effect_label("outbox_event", PROD)
    assert event.delivery == {NO_LOSS} and event.visibility == "eventual"
    assert PARKS_ON_POISON_OR_BUDGET in event.qualifiers
    assert (
        effect_label("outbox_event", replace(PROD, durable_bus=False)).delivery == set()
    )
    # durable consumer effect: no_dup iff inbox in the same database AND the
    # inbox outlives the stream; no_loss always qualified
    consumer = effect_label("consumer_effect", PROD)
    assert consumer.delivery == {NO_LOSS, NO_DUP}
    for broken in (
        replace(PROD, inbox_in_effect_database=False),
        replace(PROD, inbox_outlives_stream=False),  # expired window
        replace(PROD, durable_consumer=False),
    ):
        assert NO_DUP not in effect_label("consumer_effect", broken).delivery
    no_dlq = effect_label("consumer_effect", replace(PROD, dead_letter=False))
    assert PARKS_ON_EXPLICIT_POISON in no_dlq.qualifiers
    # consumer effect in another store: no_dup only from a conditional write
    other = Mechanism(durable_bus=True, conditional_write=True)
    assert NO_DUP in effect_label("consumer_effect_other_store", other).delivery
    assert (
        NO_DUP
        not in effect_label(
            "consumer_effect_other_store", replace(other, conditional_write=False)
        ).delivery
    )
    # command: no_dup iff a database command log AND the TTL covers retries;
    # the cache fallback is never no_dup
    command = effect_label("command", PROD)
    assert (
        command.delivery == {NO_LOSS, NO_DUP} and CALLER_RETRIES in command.qualifiers
    )
    assert (
        NO_DUP not in effect_label("command", replace(PROD, command_log=False)).delivery
    )
    assert (
        NO_DUP
        not in effect_label(
            "command", replace(PROD, dedupe_covers_retries=False)
        ).delivery
    )
    # on_commit: never re-run, never retried
    assert effect_label("on_commit").delivery == {NO_DUP}
    # opted-out secondary: no_dup only keyed / version-guarded, never no_loss
    assert effect_label("opted_out_secondary").delivery == set()
    assert effect_label(
        "opted_out_secondary", Mechanism(keyed_or_versioned=True)
    ).delivery == {NO_DUP}
    # nothing but the write store's write is labelled atomic
    for kind in (
        "outbox_event",
        "consumer_effect",
        "consumer_effect_other_store",
        "command",
        "on_commit",
        "opted_out_secondary",
    ):
        assert ATOMIC not in effect_label(kind, PROD).delivery  # type: ignore[arg-type]
