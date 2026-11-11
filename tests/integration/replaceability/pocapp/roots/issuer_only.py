"""T-6: the issuer named, no protected claim assigned."""

from sandloom.sdk.app import create_app

from . import ISSUER

app = create_app(claim_authorities={"issuer": ISSUER["issuer"]})
core = app.state.core
