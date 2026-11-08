"""A6: the app-local module named ``identity`` as the issuer and authority."""

from sandloom.sdk.app import create_app

from . import M

app = create_app(
    claim_authorities={
        "issuer": f"{M}identity",
        "roles": f"{M}identity",
        "permissions": f"{M}identity",
    }
)
core = app.state.core
