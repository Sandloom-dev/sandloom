"""Pins exactly what ``sandloom_identity.register(core)`` registers.

Run against a buffering registration draft (nothing reaches the live app), so
the declared surface — routers and their order, event schemas, the critical
claims contributor, the 2FA-key registration refusal and the descriptor — is
compared as a whole.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.sdk.infra import settings
from sandloom.sdk.module import CapabilityRequirement, ModuleAPI, ModuleDescriptor
from sandloom.sdk.testing import ModuleRegistrationDraft
from sandloom_identity import register
from sandloom_identity.api import (
    auth_router,
    roles_router,
    setup_router,
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
from sandloom_identity.services import auth_service

EXPECTED_DESCRIPTOR = ModuleDescriptor(
    name="sandloom_identity",
    version="1.0.0",
    capabilities=[
        CapabilityRequirement(
            "database", features=frozenset({"transaction_locks", "list_aggregates"})
        ),
        CapabilityRequirement("cache"),
        CapabilityRequirement("eventbus"),
        # Account mail must be delivered; the token cleanup is optional.
        CapabilityRequirement("notification", features=frozenset({"email"})),
        CapabilityRequirement(
            "taskqueue", features=frozenset({"periodic"}), required=False
        ),
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
    # Eight routers, mounted in this order, with no include kwargs.
    assert draft.routers == [
        (auth_router, {}),
        (roles_router, {}),
        (setup_router, {}),
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
    # identity holds refresh sessions (the declaration's default): its
    # durable block consumer below is the issuer's obligation (SANDLOOM-B035).
    assert descriptor.issues_tokens is True
    assert descriptor.refresh_sessions is True
    # The emergency block (core.auth.user_blocked.v1) ends refresh sessions
    # (the issuer's obligation); the durable revocation applier is the
    # kernel's, registered in every composition (ADR-0012 decision 5).
    assert [(c.channel, c.name, c.required) for c in draft.event_consumers] == [
        ("core.auth.user_blocked.v1", "sessions@user_blocked", True),
    ]
    assert draft.event_schema_objs == []
    assert draft.query_projections == []
    assert draft.projection_consumers == []
    assert draft.projection_rebuilders == []
    assert draft.access_facts == []


def test_missing_twofa_key_refuses_registration() -> None:
    """A missing TOTP key stops registration itself (not a lifespan task),
    so every process that builds the composition refuses with the reason;
    nothing of the module is registered. The check is identity's own settings
    model, validated before ``setup``; the subprocess matrix
    (``test_boot_configuration_checks_module_refusals``) proves every entry
    point."""
    from sandloom.sdk.testing import validate_settings
    from sandloom_identity.config import IdentitySettings

    with pytest.raises(SystemExit, match="TWOFA_ENCRYPTION_KEY is required"):
        validate_settings(IdentitySettings, {}, environment="prod")


def test_missing_jwt_secret_refuses_registration(
    sandloom_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """identity keys its emailed tokens with ``JWT_SECRET``: without it (an
    asymmetric deployment's verify-only processes) registration itself
    refuses, so no entry point builds past it; nothing is registered."""
    monkeypatch.setattr(settings, "jwt_secret", "")
    draft = ModuleRegistrationDraft()
    with pytest.raises(SystemExit, match="identity requires JWT_SECRET"):
        register(ModuleAPI(sandloom_app.state.core, draft=draft))
    assert draft.startup == []
    assert draft.claims_contributors == []
