"""Composition root: no protected claim assigned (T-6)."""

from sandloom.sdk.app import create_app

app = create_app(claim_authorities={})
core = app.state.core
