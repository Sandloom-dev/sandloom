"""Pins exactly what ``sandloom_identity.register(core)`` registers.

Run against a buffering registration draft (nothing reaches the live app), so
the declared surface — routers and their order, event schemas, the critical
claims contributor, the 2FA-key startup guard and the descriptor — is
compared as a whole.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.sdk.module import CapabilityRequirement, ModuleAPI, ModuleDescriptor
from sandloom.sdk.testing import ModuleRegistrationDraft
from sandloom_identity import register
from sandloom_identity.api import (
    auth_router,
    roles_router,
    sso_router,
    twofa_router,
    user_roles_router,
    user_safety_router,
    users_router,
)
from sandloom_identity.claims import contribute_identity_claims
from sandloom_identity.contracts import (
    CONTACT_SERVICE_V1,
    SECURITY_SERVICE_V1,
    ContactContract,
    SecurityOperationsContract,
)
from sandloom_identity.events import BaseEvent
from sandloom_identity.services import auth_service, twofa_service

EXPECTED_DESCRIPTOR = ModuleDescriptor(
    name="sandloom_identity",
    version="1.0.0",
    capabilities=[
        CapabilityRequirement("database"),
        CapabilityRequirement("cache"),
        CapabilityRequirement("eventbus"),
    ],
    issues_tokens=True,
)


def _register(sandloom_app: Any) -> tuple[Any, ModuleRegistrationDraft]:
    draft = ModuleRegistrationDraft()
    descriptor = register(ModuleAPI(sandloom_app.state.core, draft=draft))
    return descriptor, draft


def test_register_declares_the_identity_surface(sandloom_app: Any) -> None:
    descriptor, draft = _register(sandloom_app)

    assert descriptor == EXPECTED_DESCRIPTOR
    # Seven routers, mounted in this order, with no include kwargs.
    assert draft.routers == [
        (auth_router, {}),
        (roles_router, {}),
        (sso_router, {}),
        (twofa_router, {}),
        (user_roles_router, {}),
        (user_safety_router, {}),
        (users_router, {}),
    ]
    # Public API 1.0: each channel's producer class, registered by discovery
    # under its class name with its generated schema.
    producers = BaseEvent.__subclasses__()
    assert draft.event_schemas == [
        (cls.channel, cls.__name__, cls.wire_schema()) for cls in producers
    ]
    assert len(draft.event_schemas) == len(producers) == 15
    # Critical: a failing contributor aborts minting (no privilege-less token).
    assert draft.claims_contributors == [(contribute_identity_claims, True)]
    # Outside prod the 2FA key falls back to the JWT secret: no guard task.
    # Best-effort: the unknown-email timing equalizer's dummy argon2 hash is
    # made before serving. Critical: the declared reads (GET /auth/me) are
    # planned, so a declaration that does not match the schema fails the boot.
    assert [(t.__name__, c) for t, c in draft.startup] == [
        ("warm_dummy_password_hash", False),
        ("prepare_declared_operations", True),
    ]
    assert draft.shutdown == [auth_service.drain_emails_on_shutdown]
    # A user's contact details (identity.contact.v1) and the operator
    # incident commands (sandloom security revoke-sessions / ...).
    assert sorted(draft.services) == sorted(
        [
            (CONTACT_SERVICE_V1, ContactContract),
            (SECURITY_SERVICE_V1, SecurityOperationsContract),
        ]
    )
    assert draft.health == []
    # The emergency block (core.auth.user_blocked.v1) ends refresh sessions;
    # the durable applier writes revocation marks that failed after commit.
    assert [(c.channel, c.name, c.required) for c in draft.event_consumers] == [
        ("core.auth.user_blocked.v1", "sessions@user_blocked", True),
        ("core.auth.tokens_revoked.v1", "sessions@tokens_revoked", True),
    ]
    assert draft.event_schema_objs == []
    assert draft.query_projections == []
    assert draft.projection_consumers == []
    assert draft.projection_rebuilders == []
    assert draft.access_facts == []


async def test_missing_twofa_key_registers_a_critical_refusal(
    sandloom_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        twofa_service, "twofa_configuration_error", lambda: "TWOFA key missing"
    )
    descriptor, draft = _register(sandloom_app)

    assert descriptor == EXPECTED_DESCRIPTOR
    assert [t.__name__ for t, _ in draft.startup] == [
        "_refuse_missing_twofa_key",
        "warm_dummy_password_hash",
        "prepare_declared_operations",
    ]
    task, critical = draft.startup[0]
    assert critical is True
    with pytest.raises(RuntimeError, match="TWOFA key missing"):
        await task()
    # Everything else is registered unchanged.
    assert len(draft.routers) == 7
    assert draft.claims_contributors == [(contribute_identity_claims, True)]
