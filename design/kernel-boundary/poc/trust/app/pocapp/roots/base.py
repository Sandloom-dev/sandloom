"""Composition root for the base kernel (no claim_authorities argument)."""

from sandloom.sdk.app import create_app

app = create_app()
core = app.state.core
