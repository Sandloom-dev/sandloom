"""ASGI entry point (the composition root): tenancy + billing, no identity.

``acme_idp`` (``oidc_identity.modules.acme_idp``) takes the place of
``sandloom_identity``: it verifies the id_token of an external OpenID
provider and mints this application's access token. The map below is the
only place that says so: ``acme_idp`` is the token ``issuer`` and the
authority of the rights (``roles`` / ``permissions``); tenancy and billing
keep their own keys. ``email`` / ``email_verified`` are not named: a map that
names an issuer assigns them to it.

One root for every process of every topology. ``SANDLOOM_MODULES`` picks the
modules a process loads, and the map stays whole: a process that loads no
issuer checks none of it, the issuing one needs every authority loaded or
bound (``sandloom explain SANDLOOM-B035``).

    uvicorn oidc_identity.asgi:app --reload
"""

from sandloom.sdk.app import (  # sandloom: advanced composition root
    Authority,
    app_modules,
    create_app,
)

BUNDLED: list[str] = ["tenancy", "billing"]

IDP = "oidc_identity.modules.acme_idp"

#: Billing's keys are fail-safe (ADR-0014 decision 7): declared here too, for
#: the process that binds billing remotely ``"critical": false``, where
#: billing's own declaration does not run.
BILLING = Authority("sandloom_billing", fail_safe=True)

#: Who may write each protected token claim, and the module that issues
#: tokens (ADR-0014): code, never a setting.
CLAIM_AUTHORITIES: dict[str, str | Authority] = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": "sandloom_tenancy",
    "subscription_status": BILLING,
    "plan_code": BILLING,
    "features": BILLING,
    "limits": BILLING,
}

app = create_app(
    title="oidc_identity",
    default_modules=[*BUNDLED, *app_modules("oidc_identity.modules")],
    claim_authorities=CLAIM_AUTHORITIES,
)
