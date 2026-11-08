"""Deny-by-default (ADR-0009 item 1, spec §5.1, SANDLOOM-B008).

Every API route resolves an authenticator (``@authenticates`` for a user,
``@authenticates(kind="service")`` for a system credential) or declares
``public(reason=...)``; ``create_app`` refuses a route that is neither, or
both, and the message names the fix. These tests drive the check directly
and through ``create_app``, plus the composed platform (every route resolves).

Moved from ``sandloom/core/tests/test_route_policy_auth.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from typing import Any

from sandloom.core.route_policy import (
    resolve_route_policy,
)


def test_bundled_public_routes_are_exactly_the_reviewed_set() -> None:
    """Independent oracle (spec §10.1): the hand-kept list of the platform's
    public module routes; a new public route must be added here on purpose."""
    from sandloom.main import app

    expected = {
        ("GET", "/api/v1/identity/setup-state"),
        ("GET", "/auth/health"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/register"),
        ("POST", "/auth/request-password-reset"),
        ("POST", "/auth/resend-verification"),
        ("POST", "/auth/reset-password"),
        ("POST", "/auth/sso/mfa"),
        ("GET", "/auth/sso/providers"),
        ("GET", "/auth/sso/{provider}/callback"),
        ("GET", "/auth/sso/{provider}/redirect"),
        ("POST", "/auth/verify-email"),
        ("GET", "/api/v1/billing/plans"),
        ("POST", "/api/v1/billing/webhooks/stripe"),
    }
    issuers = {
        ("POST", "/auth/login"),
        ("POST", "/auth/refresh"),
        ("POST", "/auth/sso/mfa"),
        ("GET", "/auth/sso/{provider}/callback"),
    }
    found: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        if not getattr(route, "sandloom_module", None):
            continue
        for method in getattr(route, "methods", ()) or ():
            policy = resolve_route_policy(route, method)
            if policy.auth == "public" and method != "HEAD":
                found[(method, route.path)] = policy
    assert set(found) == expected
    assert {k for k, p in found.items() if p.public and p.public.issues_tokens} == (
        issuers
    )
    # a public route that reads a subject would be a contradiction
    assert all(p.authenticators == () for p in found.values())
