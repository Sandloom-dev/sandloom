"""ASGI entry point (the composition root).

Loads the installed identity, tenancy and billing modules plus every module of
``helpdesk.modules`` (found statically: each subpackage whose
``__init__.py`` defines ``register``), so adding a module edits nothing
here. ``SANDLOOM_MODULES`` selects a subset.

    uvicorn helpdesk.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced composition root
    app_modules,
    create_app,
)

BUNDLED: list[str] = ["identity", "tenancy", "billing"]

#: Who may write each protected token claim, and the module that issues
#: tokens (ADR-0014): code, never a setting. A contributor writing a protected
#: claim it is not assigned loses its whole result; startup refuses a map the
#: composition cannot honour (`sandloom explain SANDLOOM-B035`).
CLAIM_AUTHORITIES: dict[str, str] = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
    "email": "sandloom_identity",
    "email_verified": "sandloom_identity",
    "tenant_ids": "sandloom_tenancy",
    "subscription_status": "sandloom_billing",
    "plan_code": "sandloom_billing",
    "features": "sandloom_billing",
    "limits": "sandloom_billing",
}

app = create_app(
    default_modules=[*BUNDLED, *app_modules("helpdesk.modules")],
    claim_authorities=CLAIM_AUTHORITIES,
)
