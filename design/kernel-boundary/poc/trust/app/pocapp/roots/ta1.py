"""Composition root: TA-1, the fixture's authorities."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "pocapp.modules.test_identity", "permissions": "pocapp.modules.test_identity", "tenant_ids": "pocapp.modules.grants"})
core = app.state.core
