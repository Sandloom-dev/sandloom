"""Composition root: map B under the TA-0 control (T-18 / T-10)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "pocapp.modules.grants", "permissions": "pocapp.modules.test_identity", "tenant_ids": "pocapp.modules.grants"}, claim_trust_mode="ta0")
core = app.state.core
