"""PA-1 control: the consumer's ``QuotaAvailable`` on the declarative base."""

from dxquota.shared.contracts.quota_limits_v1 import QUOTA_LIMITS_V1
from sandloom.sdk import Contract
from sandloom.sdk.authz import Category  # sandloom: advanced PA-1 atom

from ._declared import DeclaredCheck

limits = Contract(QUOTA_LIMITS_V1)


class QuotaAvailable(DeclaredCheck):
    client = limits
    method = "available"
    subject_arg = "owner_id"
    category = Category.QUOTA_EXCEEDED
