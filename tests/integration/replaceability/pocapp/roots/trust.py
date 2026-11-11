"""The standard topology's root: ``test_identity`` and ``grants`` assigned."""

from sandloom.sdk.app import create_app

from . import TRUST

app = create_app(claim_authorities=TRUST)
core = app.state.core
