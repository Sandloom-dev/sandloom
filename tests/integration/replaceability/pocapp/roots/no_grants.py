"""A composition without a tenant-grant provider: ``tenant_ids`` unassigned."""

from sandloom.sdk.app import create_app

from . import ISSUER

app = create_app(claim_authorities=ISSUER)
core = app.state.core
