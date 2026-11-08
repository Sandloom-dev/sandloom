"""Composition root: the TA-0 control (M1, M2, one platform admission; no view, cpv or sealed mint)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "pocapp.modules.test_identity", "permissions": "pocapp.modules.test_identity", "tenant_ids": "pocapp.modules.grants"}, claim_trust_mode="ta0")
core = app.state.core
