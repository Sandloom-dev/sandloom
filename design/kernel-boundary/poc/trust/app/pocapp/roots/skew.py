"""Composition root: map B: roles re-assigned (T-10 verifier)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "pocapp.modules.grants", "permissions": "pocapp.modules.test_identity", "tenant_ids": "pocapp.modules.grants"})
core = app.state.core
