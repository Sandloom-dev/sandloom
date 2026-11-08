"""Composition root: a typo in the authority path (T-5)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "pocapp.modules.test_identty", "permissions": "pocapp.modules.test_identity"})
core = app.state.core
