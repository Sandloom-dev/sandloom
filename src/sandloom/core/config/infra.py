"""Named infrastructure instances (``INFRA``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InfraConfig(BaseModel):
    """Named infrastructure instances (``INFRA``)."""

    # Infrastructure plugin layer (Wave 6).
    # Optional JSON declaring additional named infrastructure instances or
    # overriding the synthesized defaults. Shape:
    #   {"database": {"analytics": {"driver": "sqlalchemy", "url": "..."}},
    #    "cache": {"sessions": {"driver": "redis", "url": "..."}}}
    # When empty, one "default" instance per capability is synthesized from the
    # flat settings below. See sandloom.core.infra.config.
    infra: dict[str, dict[str, dict[str, object]]] = Field(
        default_factory=dict, alias="INFRA"
    )
