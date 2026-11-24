"""t6_team_overview — cross-module read model (projection)."""

import re

from _dx import module_dir, module_tests, routes, text

TEAM = module_dir("team_overview")


def test_fn_list_endpoint() -> None:
    assert any(r.method == "GET" for r in routes(TEAM))


def test_fn_tests_written() -> None:
    assert module_tests(TEAM)


def test_dev_projection_not_rpc_join() -> None:
    """A read spanning modules is a projection built from events."""
    src = text(TEAM)
    assert "Projection(" in src or "register_projection_consumer" in src


def test_dev_version_guarded_writes() -> None:
    """Rows converge under reordering: a version per aggregate guards each write
    (a projection's ``version_field=``, ``monotonic_upsert``, or the shared
    tenancy membership guard ``member_event_action(`` from
    ``sandloom.shared.membership``)."""
    src = text(TEAM)
    assert (
        "version_field=" in src
        or "monotonic_upsert" in src
        or "member_event_action(" in src
    )


def test_dev_rebuildable() -> None:
    src = text(TEAM)
    assert "rebuild_from=" in src or "register_projection_rebuilder" in src


def reads_billing(src: str) -> bool:
    """The billing side of the overview comes from billing's own data: a
    projection over ``billing.subscription`` events, or a live read of the
    plan through the ``billing.entitlements.v1`` contract at request time (the
    recipe ``projection-from-platform-events``: the subscription events carry
    no version, so the plan is read live). Only an import of the generated
    contract stub counts, not a comment that names it."""
    return "billing.subscription" in src or bool(
        re.search(
            r"^\s*from\s+\S*billing_entitlements_v1\s+import\b", src, re.MULTILINE
        )
    )


def test_dev_sources_both_modules() -> None:
    src = text(TEAM)
    assert "tenancy.member" in src
    assert reads_billing(src)
