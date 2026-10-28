"""``authorizes`` (spec §2.3): a custom authorization dependency is recorded
in the route snapshot (``authz``), so dropping it is a snapshot diff."""

from __future__ import annotations

from fastapi import Depends, FastAPI

from sandloom.core.guard_order import authorizes
from sandloom.core.route_snapshots import route_policy
from sandloom.shared.authz import current_context


@authorizes
async def owns_the_ticket() -> None:
    return None


def test_the_snapshot_records_custom_authorization_dependencies() -> None:
    app = FastAPI()

    @app.get("/t", dependencies=[Depends(current_context), Depends(owns_the_ticket)])
    async def checked() -> None:
        return None

    @app.get("/u", dependencies=[Depends(current_context)])
    async def plain() -> None:
        return None

    routes = {r.path: r for r in app.routes if getattr(r, "path", "") in ("/t", "/u")}
    assert route_policy(routes["/t"], "GET")["authz"] == [f"{__name__}:owns_the_ticket"]
    assert "authz" not in route_policy(routes["/u"], "GET")
