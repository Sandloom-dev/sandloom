"""The fixture module's declared settings (KB-49, D2a form)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator

from sandloom.sdk.module import RemovedSetting, Risk, SemanticSetting


class InventorySettings(BaseModel):
    api_token: SecretStr | None = Field(default=None, alias="INVENTORY_API_TOKEN")
    reserve_ttl_seconds: int = Field(
        default=300, ge=1, le=3600, alias="INVENTORY_RESERVE_TTL_SECONDS"
    )
    allow_oversell: bool = Field(default=False, alias="INVENTORY_ALLOW_OVERSELL")
    warehouse: str = Field(default="main", alias="INVENTORY_WAREHOUSE")

    semantic: ClassVar[tuple[SemanticSetting, ...]] = (
        SemanticSetting(
            "INVENTORY_ALLOW_OVERSELL",
            "Orders may reserve more stock than is on hand.",
            profile=dict.fromkeys(("dev", "test", "prod", "prod_strict"), False),
            strength=(True, False),
            refused=frozenset({"prod", "prod_strict"}),
            risk="oversell_in_prod",
        ),
    )
    risks: ClassVar[tuple[Risk, ...]] = (
        Risk(
            "oversell_in_prod",
            "INVENTORY_ALLOW_OVERSELL=true in prod: orders can exceed stock.",
            "inventory's production check",
        ),
    )
    removed: ClassVar[tuple[RemovedSetting, ...]] = (
        RemovedSetting(
            "INVENTORY_LEGACY_SYNC",
            "0.2",
            "stock sync is always event-driven now",
        ),
    )
    shared: ClassVar[frozenset[str]] = frozenset({"INVENTORY_WAREHOUSE"})
    console: ClassVar[tuple[str, ...]] = ("INVENTORY_WAREHOUSE",)
    backup: ClassVar[dict[str, str]] = {"INVENTORY_API_TOKEN": "the supplier API"}

    @model_validator(mode="after")
    def _no_oversell_in_prod(self, info: ValidationInfo) -> InventorySettings:
        ctx: dict[str, Any] = info.context or {}
        if (
            ctx.get("environment") == "prod"
            and self.allow_oversell
            and not ctx["accepts_risk"]("oversell_in_prod")
        ):
            raise ValueError(
                "INVENTORY_ALLOW_OVERSELL=true is not allowed with ENVIRONMENT=prod; "
                'accept the risk: SANDLOOM_ACCEPTED_RISKS=["inventory.oversell_in_prod"]'
            )
        return self
