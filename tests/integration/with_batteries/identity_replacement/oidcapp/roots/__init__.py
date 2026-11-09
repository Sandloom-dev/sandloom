"""The prototype's composition root data: its claim-authority map.

acme_idp replaces sandloom_identity as the issuer and the authority of the
rights; tenancy and billing keep their keys. ``email`` / ``email_verified``
are not named: a map with an issuer assigns them to it.
"""

IDP = "oidcapp.modules.acme_idp"

CLAIM_AUTHORITIES: dict[str, str] = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": "sandloom_tenancy",
    "subscription_status": "sandloom_billing",
    "plan_code": "sandloom_billing",
    "features": "sandloom_billing",
    "limits": "sandloom_billing",
}
