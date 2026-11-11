"""A: a token issuer that is NOT sandloom_identity and is not named ``identity``.

It supplies roles/permissions through the ordinary claims-contributor mechanism.
``POC_ADMINS`` (comma-separated user ids) are given ``roles: ["admin"]``.
``POC_TEST_IDENTITY_CRITICAL=0`` registers the contributor as non-critical
(an issuer refuses the boot so, SANDLOOM-B035); ``POC_TEST_IDENTITY_SILENT=1``
makes it write no claim (T-6: a map assigning nothing but the issuer).
"""

import os

from sandloom.sdk import define_module

from . import state


async def contribute(user_id: object) -> dict[str, list[str]]:
    if state.DOWN["on"]:
        raise RuntimeError("test_identity claims source is down")
    if os.environ.get("POC_TEST_IDENTITY_SILENT") == "1":
        return {}
    admins = {u for u in os.environ.get("POC_ADMINS", "").split(",") if u}
    if str(user_id) in admins:
        return {"roles": ["admin"], "permissions": ["perm.poc.write"]}
    return {"roles": [], "permissions": []}


def _setup(api: object) -> None:
    critical = os.environ.get("POC_TEST_IDENTITY_CRITICAL", "1") == "1"
    api.add_claims_contributor(contribute, critical=critical)  # type: ignore[attr-defined]


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
