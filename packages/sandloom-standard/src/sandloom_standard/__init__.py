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

Billing's keys are declared fail-safe here (``Authority(...,
fail_safe=True)``, ADR-0014 decision 7) as billing declares them itself: an
issuer that binds billing remotely ``"critical": false`` (the microservices
profile) has no other declaration of them, and a billing outage still signs
a user in.

Each authority is declared with its public name and its import path
(``Authority("identity", module="sandloom_identity")``), so a process of a
split that does not install a battery still knows the name its contracts are
served under (``identity.claims.v1``, ``identity.security.v1``): the last
import segment is not it.
"""

from sandloom.sdk.app import Authority  # sandloom: advanced composition root

#: Identity, the token issuer and the authority of the rights and the
#: issuer fields.
_IDENTITY = Authority("identity", module="sandloom_identity")
#: Tenancy, the authority of ``tenant_ids``.
_TENANCY = Authority("tenancy", module="sandloom_tenancy")
#: Billing, the authority of its fail-safe keys.
_BILLING = Authority("billing", module="sandloom_billing", fail_safe=True)

#: The standard batteries' claim authorities and the issuer.
STANDARD_CLAIM_AUTHORITIES: dict[str, str | Authority] = {
    "issuer": _IDENTITY,
    "roles": _IDENTITY,
    "permissions": _IDENTITY,
    "email": _IDENTITY,
    "email_verified": _IDENTITY,
    "tenant_ids": _TENANCY,
    "subscription_status": _BILLING,
    "plan_code": _BILLING,
    "features": _BILLING,
    "limits": _BILLING,
}

__all__ = ["STANDARD_CLAIM_AUTHORITIES"]
