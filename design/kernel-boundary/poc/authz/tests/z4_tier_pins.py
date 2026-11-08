"""Z-4: the tier pins with generated atoms (G-AUTHZ-02): a claims-only policy
never depends on get_db; a policy with a live atom does (today's rule, and
with KB_POC_K6=1 only when an atom says it needs the request session)."""

import inspect

from sandloom.core.di import get_db
from sandloom.sdk.authz import Account, Perm, Role, require
from sandloom.shared.contracts.billing_entitlements_v1 import (
    Credits,
    Subscribed,
    SubscriptionActive,
)
from sandloom.shared.contracts.tenancy_membership_v1 import OrgRole


def depends_on_db(policy) -> bool:
    guard = require(policy)
    return any(
        getattr(p.default, "dependency", None) is get_db
        for p in inspect.signature(guard).parameters.values()
    )


for label, policy in [
    ("Subscribed(of=Account.user())", Subscribed(of=Account.user())),
    ("Role('admin') & Subscribed(of=Account.user())", Role("admin") & Subscribed(of=Account.user())),
    ("Perm('x') | Subscribed(of=Account.user())", Perm("x") | Subscribed(of=Account.user())),
    ("Subscribed(of=Account.bound_tenant())", Subscribed(of=Account.bound_tenant())),
    ("SubscriptionActive(of=Account.user())", SubscriptionActive(of=Account.user())),
    ("Role('admin') | Credits(10, of=Account.user())", Role("admin") | Credits(10, of=Account.user())),
    ("OrgRole('admin') & SubscriptionActive(of=Account.bound_tenant())", OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())),
]:
    print(f"tier={policy.tier} get_db={depends_on_db(policy)!s:5} {label}")
