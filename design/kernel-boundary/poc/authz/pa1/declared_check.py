"""PA-1 control: a consumer-owned declarative base (the ~20-line PoC base).

A consumer class names the provider's contract, method and the arguments the
request supplies; the base does the call. In PA-1 the kernel would ship this
base; tier, category and risk stay the consumer's word.
"""

from typing import Any, ClassVar

from sandloom.sdk.authz import (  # sandloom: advanced PA-1 base
    TIER_LIVE,
    AuthzContext,
    Requirement,
)


class DeclaredCheck(Requirement):
    tier = TIER_LIVE
    client: ClassVar[Any]
    method: ClassVar[str]
    subject_arg: ClassVar[str | None] = None

    def __init__(self, **params: Any) -> None:
        self.params = params

    async def evaluate(self, ctx: AuthzContext) -> bool:
        args = dict(self.params)
        if self.subject_arg:
            args[self.subject_arg] = ctx.subject.user_id
        return bool(await getattr(self.client, self.method)(**args))

    def contracts(self) -> frozenset[str]:
        return frozenset({self.client.contract_name})
