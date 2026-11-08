"""T-5: a typo in an authority's import path."""

from sandloom.sdk.app import create_app

from . import TRUST, M

app = create_app(claim_authorities={**TRUST, "roles": f"{M}test_identty"})
core = app.state.core
