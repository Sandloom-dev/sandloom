"""First-run state: whether a platform administrator exists yet."""

from fastapi import APIRouter, HTTPException

from sandloom.sdk.infra import settings
from sandloom.sdk.web import DbSession, public
from sandloom_identity.services.setup_state import platform_admin_exists

setup_router = APIRouter(prefix="/api/v1/identity", tags=["setup"], dependencies=[])


@setup_router.get(
    "/setup-state",
    # A hint for the console's sign-in page, not part of the typed public API.
    include_in_schema=False,
    dependencies=[
        public(reason="first-run hint: tells only whether an administrator exists"),
    ],
)
async def setup_state(db: DbSession) -> dict[str, bool]:
    """``{"has_admin": bool}``; 404 with ``ENVIRONMENT=prod``.

    The console's sign-in page shows the ``sandloom admin create`` command
    while no administrator exists. Production does not tell.
    """
    if settings.is_prod:
        raise HTTPException(404, "Not found")
    return {"has_admin": await platform_admin_exists(db)}
