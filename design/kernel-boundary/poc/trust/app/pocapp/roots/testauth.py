"""Composition root: the kernel test authority assigned outside a test composition (T-12)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={"roles": "sandloom-test", "permissions": "pocapp.modules.test_identity"})
core = app.state.core
