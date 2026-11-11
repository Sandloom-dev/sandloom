"""Authorization measurement routes using provider-generated atoms."""

from sandloom.sdk import define_module
from sandloom.sdk.module import ContractRequirement

register = define_module(
    __name__,
    version="0.1.0",
    consumes=[
        ContractRequirement(
            "billing.entitlements.v1",
            methods=("check_subscription_active", "has_credits_at_least"),
        ),
        ContractRequirement(
            "tenancy.membership.v1",
            methods=("has_role_at_least",),
        ),
    ],
)
