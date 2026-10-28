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
from sandloom.sdk.module import ModuleAPI

from .claims import contribute_identity_claims
from .consumers import revoke_refresh_tokens_on_block
from .services import twofa_service
from .services.auth_service import AuthService, drain_emails_on_shutdown
from .services.operations import OPERATIONS
from .sessions import apply_token_revocation


def _setup(core: ModuleAPI) -> None:
    # Production must encrypt TOTP secrets with a dedicated key: the JWT-derived
    # fallback ties 2FA secrets to the signing key's lifecycle (rotation would
    # make them undecryptable) and is refused in prod. A CRITICAL startup task
    # aborts the lifespan instead of serving without one.
    twofa_error = twofa_service.twofa_configuration_error()
    if twofa_error is not None:
        core.logger.error("identity: %s", twofa_error)

        async def _refuse_missing_twofa_key() -> None:
            raise RuntimeError(twofa_error)

        core.add_startup_task(_refuse_missing_twofa_key, critical=True)

    # identity keys its emailed tokens / fingerprints with JWT_SECRET, which
    # is optional in an asymmetric JWT deployment: refuse to serve without it.
    if not settings.jwt_secret:

        async def _refuse_missing_identity_secret() -> None:
            raise RuntimeError(
                "identity requires JWT_SECRET (issuer-only with an asymmetric "
                "JWT_ALGORITHM): it keys emailed verification/reset tokens"
            )

        core.add_startup_task(_refuse_missing_identity_secret, critical=True)

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

    # Durable token-revocation applier: every revoke_user_tokens_on_commit
    # (identity's own, tenancy's member removal, block_user — in any service)
    # writes core.auth.tokens_revoked.v1 into its transaction; a mark that
    # could not be written after commit (cache outage) is written here,
    # redelivered until it succeeds — FOREVER (dead_letter=False: capped
    # backoff, never the DLQ; only a malformed payload is dead-lettered, as
    # CRITICAL). Idempotent (marks are monotonic).
    core.register_event_consumer(
        "core.auth.tokens_revoked.v1",
        apply_token_revocation,
        name="sessions@tokens_revoked",
        dead_letter=False,
    )

    # Account emails are delivered in the background after commit: give the
    # in-flight ones a bounded chance to finish before the process stops.
    core.add_shutdown_task(drain_emails_on_shutdown)


register = define_module(
    __name__,
    version="1.0.0",
    capabilities=["database", "cache", "eventbus"],
    # identity mints the access tokens: with an asymmetric JWT_ALGORITHM its
    # process holds JWT_PRIVATE_KEY, every other service only public keys.
    issues_tokens=True,
    setup=_setup,
)

__all__ = ["register"]
