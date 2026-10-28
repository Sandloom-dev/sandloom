"""t4_resend_invitation — admin-only write endpoint in an existing module.

Framework mode (a kernel-contributor task): the bundled ``tenancy``. App mode:
the seeded app's own ``invites`` module (``seeds/teamhub``), the same
mechanism: an org-admin gate on a write to the module's tenant table, audited
or evented."""

import pytest
from _dx import (
    app_mode,
    audited_or_evented,
    changed_under,
    expanded_head,
    keeps_route_defaults,
    module_dir,
    module_prefix,
    mutating_routes,
    pick,
    reaches,
    route_leaves_trace,
    text,
)

NAME = pick(framework="tenancy", app="invites")
MODULE = module_dir(NAME)
ADMIN_GATES = ("require_org_role", "ROLE_ADMIN", 'OrgRole("admin"', "OrgRole('admin'")


def _resend() -> list:
    return [
        r for r in mutating_routes(MODULE) if "resend" in (r.path + r.function).lower()
    ]


def test_fn_resend_route() -> None:
    assert _resend()


def test_fn_tests_changed() -> None:
    assert changed_under(module_prefix(NAME, "tests"))


def test_fn_new_link_delivered() -> None:
    """App mode: the resend replaces the stored link token (the old link stops
    working) and announces the new link (the mail service delivers what the
    module publishes)."""
    if not app_mode():
        pytest.skip("app mode: the seeded invites module")
    routes = _resend()
    assert routes
    assert all(reaches(r, MODULE, ("token_hash",)) for r in routes)
    assert all(reaches(r, MODULE, (".publish(", "publish_event")) for r in routes)


def test_dev_keeps_the_route_defaults() -> None:
    """Authenticated, with automatic retry dedupe (model E)."""
    assert all(keeps_route_defaults(r) for r in _resend())


def test_dev_admin_only() -> None:
    """The admin role, inline or through a module-level policy constant."""
    for r in _resend():
        blob = r.text + "\n" + expanded_head(r, MODULE)
        assert any(g in blob for g in ADMIN_GATES), r.function


def test_dev_audited_or_evented() -> None:
    """A security-relevant membership action leaves an audit row or an event:
    explicit, or the framework's automatic ``sandloom.sensitive_write`` row (the
    route's resolved policy has a risk, ``"audit": "automatic"``)."""
    assert "resend" in text(MODULE).lower()
    if app_mode():
        # The seeded module already publishes on send: the trace must be on
        # the resend route's own code path.
        assert _resend() and all(route_leaves_trace(r, MODULE) for r in _resend())
    else:
        assert audited_or_evented(_resend(), MODULE)
