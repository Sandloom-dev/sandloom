"""t7_credits_event_v2 — roll an event channel from v1 to v2.

Framework mode (a kernel-contributor task): ``billing.credits.spent``. App
mode: the seeded app's own ``invites.invitation.sent`` (``seeds/teamhub``),
consumed by its ``activity`` module — the same mechanism: dual publish, the
consumer moved to v2, v1 kept while unknown readers remain."""

import re

import pytest
from _dx import (
    app_mode,
    consumers,
    event_classes,
    module_dir,
    module_tests,
    pick,
    text,
)

PRODUCER = module_dir(pick(framework="billing", app="invites"))
CHANNEL = pick(framework="billing.credits.spent", app="invites.invitation.sent")
NEW_FIELD = pick(framework="unit", app="recipient")
CONSUMER = module_dir("activity")


def test_fn_v2_event_class() -> None:
    """A producer class on <channel>.v2 carrying the new field."""
    assert f"{CHANNEL}.v2" in event_classes(PRODUCER).values()
    assert NEW_FIELD in text(PRODUCER)


def test_fn_tests_written() -> None:
    tests = "\n".join(p.read_text() for p in module_tests(PRODUCER))
    assert f"{CHANNEL.split('.', 1)[1]}.v2" in tests or "V2" in tests


def _consumed_channels() -> list[str]:
    """Channels of the consumer module's ``@on(E)`` handlers (its view
    classes resolved to their ``channel=``)."""
    views = event_classes(CONSUMER)
    out: list[str] = []
    for _, deco in consumers(CONSUMER):
        m = re.match(r"on\(\s*([\w.]+)", deco)
        if not m:
            continue
        name = m.group(1).rsplit(".", 1)[-1]
        out.append(views.get(name, m.group(1)))
    return out


def test_fn_consumer_moved_to_v2() -> None:
    """App mode: the activity module reads the v2 event."""
    if not app_mode():
        pytest.skip("app mode: the seeded consumer module")
    assert f"{CHANNEL}.v2" in _consumed_channels(), _consumed_channels()


def test_dev_dual_publish() -> None:
    """Old consumers keep working: v1 is still published during the rollout."""
    assert "publish_event_versions(" in text(PRODUCER)


def test_dev_v1_kept() -> None:
    """The v1 class stays declared (no in-place breaking edit, no early removal)."""
    assert f"{CHANNEL}.v1" in event_classes(PRODUCER).values()


def test_dev_consumer_not_doubled() -> None:
    """App mode: while both versions are published, the moved consumer reads
    only one of them (on both, every invitation is recorded twice)."""
    if not app_mode():
        pytest.skip("app mode: the seeded consumer module")
    channels = _consumed_channels()
    assert not (f"{CHANNEL}.v1" in channels and f"{CHANNEL}.v2" in channels), channels
