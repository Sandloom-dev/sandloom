"""T-12: ``as_user(roles=[...])`` in a test app keeps working under TA-1.

Run with the TA-1 kernel's interpreter and the test kit plugin (README in
results.md): the composition root assigns roles/permissions to
``test_identity``; ``as_user`` mints through the kernel test authority, which
only a test composition may assign (the plugin marks this process as one).
"""

from __future__ import annotations

from typing import Any

from sandloom.sdk.testing import as_user


async def test_as_user_roles_pass_every_gate(client: Any) -> None:
    _, admin = as_user(roles=["admin"], permissions=["perm.poc.write"])
    for path in (
        "/api/v1/shop/role-admin",
        "/api/v1/shop/perm-write",
        "/api/v1/shop/role-and-perm",
        "/api/v1/shop/platform",
    ):
        r = await client.get(path, headers=admin)
        assert r.status_code == 200, (path, r.text)
    _, user = as_user()
    r = await client.get("/api/v1/shop/role-admin", headers=user)
    assert r.status_code == 403, r.text
    # the default tenant binding of as_user still works (tenant_id + tenant_ids)
    _, tenant_user = as_user()
    r = await client.get("/api/v1/shop/tenant", headers=tenant_user)
    assert r.status_code == 200, r.text
