"""acme_idp's settings: the external issuer it trusts and the role mapping."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sandloom.sdk.module import settings_of  # sandloom: advanced module settings


class AcmeIdpSettings(BaseModel):
    """Which OIDC provider signs the id_tokens this application accepts."""

    issuer: str = Field(
        alias="ACME_IDP_ISSUER",
        description="The external issuer's URL (its discovery document's 'issuer').",
    )
    client_id: str = Field(
        alias="ACME_IDP_CLIENT_ID",
        description="This application's client id: the 'aud' an id_token must carry.",
    )
    admin_group: str = Field(
        default="platform-admins",
        alias="ACME_IDP_ADMIN_GROUP",
        description="The provider group whose members get the platform role 'admin'.",
    )
    jwks_ttl_seconds: int = Field(
        default=300,
        ge=0,
        alias="ACME_IDP_JWKS_TTL_SECONDS",
        description="How long the provider's signing keys are cached.",
    )


def acme_idp_settings() -> AcmeIdpSettings:
    """The validated settings of the running application."""
    return settings_of(AcmeIdpSettings)
