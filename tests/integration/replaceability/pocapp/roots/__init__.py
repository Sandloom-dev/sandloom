"""The fixture's composition roots, one per claim-authority map.

The map is the composition root's code, never a setting (ADR-0014 decision 1,
answer #9), so each topology names the root it boots: ``pocapp.roots.<name>:app``.
Each imports ``sandloom.sdk`` only and builds the app at import, like an
application's generated ``asgi.py``.
"""

M = "pocapp.modules."

#: The fixture's issuer and its rights.
ISSUER = {
    "issuer": f"{M}test_identity",
    "roles": f"{M}test_identity",
    "permissions": f"{M}test_identity",
}
#: The standard topology's map: identity replaced by ``test_identity``,
#: tenancy by ``grants``.
TRUST = {**ISSUER, "tenant_ids": f"{M}grants"}
