"""Routes of the links module: ``/api/v1/links/...``, a fully public service.

``module_router()`` derives the prefix and tags; the router-level
``public(reason=...)`` declares every route unauthenticated (no auth module
is loaded at all), so the router injects no authentication. Persistence
stays out of this layer; the service holds the logic.
"""

from fastapi import status
from fastapi.responses import RedirectResponse

from sandloom.sdk import module_router, public
from sandloom.sdk.data import PlannedReads  # sandloom: advanced read-only planner route
from sandloom.sdk.web import DbSession  # sandloom: advanced db() lands with H1

from ..schemas import LinkCreateIn, LinkOut, LinkStatsOut
from ..services import LinkService

router = module_router(
    dependencies=[
        public(reason="linkly is a fully public shortener (no auth module loaded)"),
    ],
)


@router.post("", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
async def create_link(data: LinkCreateIn, db: DbSession) -> LinkOut:
    """Create a shortlink. Public — anyone can shorten a URL."""
    link = await LinkService.create(data, db)
    return LinkOut.model_validate(link)


@router.get("/{code}/stats", response_model=LinkStatsOut)
async def link_stats(code: str) -> LinkStatsOut:
    """Live click count for a code (served from the cache counter)."""
    return LinkStatsOut(code=code, clicks=await LinkService.clicks(code))


@router.get("/{code}", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
async def resolve_link(code: str, reads: PlannedReads) -> RedirectResponse:
    """Resolve a short code and redirect to its target URL (read-only route)."""
    target = await LinkService.resolve(code, reads)
    return RedirectResponse(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
