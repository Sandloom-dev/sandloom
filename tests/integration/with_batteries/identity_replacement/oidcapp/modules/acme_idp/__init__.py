"""acme_idp: a third-party token issuer in front of an external OIDC provider.

It replaces ``sandloom_identity`` in a composition with tenancy and billing:
users sign in at the external provider, acme_idp verifies the id_token it
signed (discovery + JWKS), maps the subject to a local account and mints
this application's access token. It is the claim authority of ``roles`` /
``permissions`` and, as the map's ``issuer``, of ``email`` /
``email_verified``; its contributor is critical (a sign-in whose rights
cannot be read is refused, never minted without them).

Stateless: it keeps no refresh sessions (the client signs in again), so an
emergency block needs no consumer here (``refresh_sessions=False``,
SANDLOOM-B035).
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI

from .claims import contribute
from .config import AcmeIdpSettings


def _setup(api: ModuleAPI) -> None:
    api.add_claims_contributor(contribute, critical=True)


register = define_module(
    __name__,
    version="0.1.0",
    capabilities=["database"],
    issues_tokens=True,
    refresh_sessions=False,
    login_entry="/api/v1/acme_idp/login",
    settings=AcmeIdpSettings,
    setup=_setup,
)
