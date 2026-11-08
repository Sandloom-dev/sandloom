"""A6 / T-7 name squat: the same test issuer as ``test_identity``, under the
short name ``identity`` (app-local, NOT sandloom_identity). Shows that a module
NAMED identity is not trusted: the platform gates still trust the name until
KB-11 (shared/guards/platform.py, core/admin/api/platform.py), and the mint
trusts only the authority its composition assigned (ADR-0014).

Switches (T-7): ``POC_IDENTITY_ISSUER=0`` loads it as a plain module (no
``issues_tokens``), ``POC_IDENTITY_CRITICAL=0`` registers the contributor
non-critical, ``POC_IDENTITY_ADMIN_ALL=1`` answers ``roles: ["admin"]`` for
every user.
"""

import os

from sandloom.sdk import define_module


async def contribute(user_id: object) -> dict[str, list[str]]:
    admins = {u for u in os.environ.get("POC_ADMINS", "").split(",") if u}
    if os.environ.get("POC_IDENTITY_ADMIN_ALL") == "1" or str(user_id) in admins:
        return {"roles": ["admin"], "permissions": ["perm.poc.write"]}
    return {"roles": [], "permissions": []}


def _setup(api: object) -> None:
    critical = os.environ.get("POC_IDENTITY_CRITICAL", "1") == "1"
    api.add_claims_contributor(contribute, critical=critical)  # type: ignore[attr-defined]


_ISSUER = os.environ.get("POC_IDENTITY_ISSUER", "1") == "1"

# A stateless issuer: it mints access tokens only and holds no refresh
# sessions an emergency block would have to end (ADR-0012 decision 5,
# SANDLOOM-B035).
register = define_module(
    __name__,
    version="0.1.0",
    issues_tokens=_ISSUER,
    refresh_sessions=not _ISSUER,
    setup=_setup,
)
