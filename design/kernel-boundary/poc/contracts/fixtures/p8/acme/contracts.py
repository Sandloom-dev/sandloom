from sandloom.sdk import contract

from acme.schemas import Limit, Quota, QuotaV2


@contract("acme.quota.v1")
class QuotaV1Contract:
    """Quota, version 1."""

    async def read(self, account: str) -> Quota:
        return Quota(used=1, limit=Limit(name="notes", maximum=10))


@contract("acme.quota.v2")
class QuotaV2Contract:
    """Quota, version 2."""

    async def read(self, account: str) -> QuotaV2:
        return QuotaV2(used=2, limits=[Limit(name="notes", maximum=10)], note="v2")
