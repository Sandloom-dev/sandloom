"""The standard Sandloom composition (the ``sandloom-standard`` distribution).

``sandloom_standard.main`` is the composition root of the standard deployment
(ADR-0012 decision 3). Importing this package builds nothing; importing
``sandloom_standard.main`` builds the app. It registers no module: it composes
the module distributions installed beside it.

:data:`STANDARD_CLAIM_AUTHORITIES`, the standard root's claim-authority map,
lives here so it can be read without building the app: the distribution
registers it under the ``sandloom.composition_roots`` entry point as
``sandloom_standard.main``, and a root with no map that loads this map's
issuer is refused (SANDLOOM-B035) with this root as the hint. The kernel
names no module or root of its own accord.
"""

#: The standard batteries' claim authorities (import paths) and the issuer.
STANDARD_CLAIM_AUTHORITIES: dict[str, str] = {
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

__all__ = ["STANDARD_CLAIM_AUTHORITIES"]
