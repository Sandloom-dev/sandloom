"""A6 name squat: the same test issuer as ``test_identity``, under the short
name ``identity`` (app-local, NOT sandloom_identity). Used only to show that the
platform gates trust the NAME: shared/guards/platform.py, core/admin/api/platform.py."""

import os

from sandloom.sdk import define_module


async def contribute(user_id: object) -> dict[str, list[str]]:
    admins = {u for u in os.environ.get("POC_ADMINS", "").split(",") if u}
    if str(user_id) in admins:
        return {"roles": ["admin"], "permissions": ["perm.poc.write"]}
    return {"roles": [], "permissions": []}


def _setup(api: object) -> None:
    api.add_claims_contributor(contribute, critical=True)  # type: ignore[attr-defined]


# A stateless issuer: it mints access tokens only and holds no refresh
# sessions an emergency block would have to end (ADR-0012 decision 5,
# SANDLOOM-B035).
register = define_module(
    __name__,
    version="0.1.0",
    issues_tokens=True,
    refresh_sessions=False,
    setup=_setup,
)
