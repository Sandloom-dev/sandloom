"""Notifications API: the caller's own inbox (authenticated, tenant-bound)."""

from sandloom.sdk import CurrentUser, module_router
from sandloom.sdk.web import DbSession  # sandloom: advanced db() lands with H1

from ..schemas import MessageOut
from ..services import Outbox

router = module_router()


@router.get(
    "/inbox",
    response_model=list[MessageOut],
)
async def inbox(db: DbSession, user: CurrentUser) -> list[MessageOut]:
    return [MessageOut.model_validate(m) for m in await Outbox.inbox(db, user.user_id)]
