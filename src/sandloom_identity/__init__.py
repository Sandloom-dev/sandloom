"""Identity module: users, JWT auth, RBAC, 2FA, SSO.

Declared with ``sandloom.sdk.define_module``. Discovered: the routers exported by
``sandloom_identity.api``, the producer classes of ``sandloom_identity.events`` and the
``identity.security.v1`` provider of ``sandloom_identity.contracts`` (operator
incident commands: bulk, audited, durably deduplicated). ``_setup`` adds what
has no declaration: startup and shutdown tasks, the claims contributor,
planner reads and the consumers of the kernel's auth channels.
"""

from sandloom.sdk import define_module
from sandloom.sdk.infra import settings
from sandloom.sdk.module import CapabilityRequirement, ModuleAPI

from .claims import contribute_identity_claims
from .config import IdentitySettings
from .consumers import revoke_refresh_tokens_on_block
from .services.auth_service import AuthService, drain_emails_on_shutdown
from .services.operations import OPERATIONS


def _setup(core: ModuleAPI) -> None:
    """Register identity's lifecycle, claims, reads and consumers.

    Raises ``SystemExit`` (the loader's configuration refusal) when
    ``JWT_SECRET`` is unset (it keys identity's emailed tokens). The TOTP key
    configuration is refused earlier, by ``IdentitySettings``.
    """
    # Production must encrypt TOTP secrets with a dedicated key: the JWT-derived
    # fallback ties 2FA secrets to the signing key's lifecycle (rotation would
    # make them undecryptable) and is refused in prod. Refused here, at
    # registration, so every process that builds the composition (the app,
    # worker, beat, ``sandloom migrate``, the operator CLIs) stops with the
    # kernel's INVALID_CONFIGURATION refusal, not only the API's lifespan.
    # The check is IdentitySettings' production validator (config.py): the
    # loader validates the model before this setup runs, so the refusal is the
    # same ``INVALID_CONFIGURATION`` in every entry point.

    # identity keys its emailed tokens / fingerprints with JWT_SECRET, which
    # is optional in an asymmetric JWT deployment (issuer-only there). Refused
    # at registration like the TOTP key, so no process that builds the
    # composition starts without it.
    if not settings.jwt_secret:
        raise SystemExit(
            "identity requires JWT_SECRET (issuer-only with an asymmetric "
            "JWT_ALGORITHM): it keys emailed verification/reset tokens"
        )

    # Timing equalizer for unknown-email logins: make the dummy argon2 hash
    # before serving, so the first unknown email costs one verify, not a
    # hash + verify.
    core.add_startup_task(AuthService.warm_dummy_password_hash)

    # Roles/permissions claims baked into minted access tokens. Critical: a
    # failure here must abort minting rather than issue a token with empty
    # roles/permissions (which would silently strip privileges).
    core.add_claims_contributor(contribute_identity_claims, critical=True)

    # Declared planner reads (GET /auth/me): planned, and their executor
    # logged, at boot.
    core.register_operations(*OPERATIONS)

    # Emergency user block (sandloom.core.di.block_user, called from any service):
    # end the user's refresh-token sessions so lifting the block does not
    # revive them. Durable: claimed once per event in this UoW.
    core.register_event_consumer(
        "core.auth.user_blocked.v1",
        revoke_refresh_tokens_on_block,
        name="sessions@user_blocked",
        dead_letter=False,  # a block must never be silently parked in a DLQ
    )

    # The durable token-revocation applier (core.auth.tokens_revoked.v1) is
    # the kernel's (ADR-0012 decision 5): every service applies every
    # revocation in its own group, whether identity is loaded there or not.

    # Account emails are delivered in the background after commit: give the
    # in-flight ones a bounded chance to finish before the process stops.
    core.add_shutdown_task(drain_emails_on_shutdown)


register = define_module(
    __name__,
    version="1.0.0",
    capabilities=[
        # RBAC administration serialises on a transaction lock (role_service);
        # token claims aggregate role and permission codes (claims).
        CapabilityRequirement(
            "database", features=frozenset({"transaction_locks", "list_aggregates"})
        ),
        "cache",
        "eventbus",
        # Verification, password-reset and security mail (integrations): a
        # notifier that cannot deliver email (the log driver in production)
        # is refused at startup instead of logging mail nobody receives.
        CapabilityRequirement("notification", features=frozenset({"email"})),
        # The token cleanup (tasks.py) is storage hygiene: validity is checked
        # on every use, so identity runs without a periodic driver (the
        # refresh_tokens table then only grows).
        CapabilityRequirement(
            "taskqueue", features=frozenset({"periodic"}), required=False
        ),
    ],
    # identity mints the access tokens: with an asymmetric JWT_ALGORITHM its
    # process holds JWT_PRIVATE_KEY, every other service only public keys.
    issues_tokens=True,
    setup=_setup,
    settings=IdentitySettings,
)

__all__ = ["register"]
