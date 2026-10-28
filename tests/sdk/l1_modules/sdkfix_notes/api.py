"""Routes of the fixture module."""

from __future__ import annotations

from sdkfix_shared.contracts.sdkfix_stats_quota_v1 import SDKFIX_STATS_QUOTA_V1

from sandloom.sdk import Contract, CurrentUser, module_router, public

quota = Contract(SDKFIX_STATS_QUOTA_V1)
router = module_router()


@router.post("/", status_code=201)
async def create_note(user: CurrentUser) -> dict[str, bool]:
    return {"allowed": await quota.may_create_note()}


@router.get("/health", dependencies=[public(reason="liveness probe")])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/settings", tenant="none")
async def note_settings(user: CurrentUser) -> dict[str, str]:
    return {"user": str(user.user_id)}
