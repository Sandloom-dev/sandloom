"""The inventory module's settings: plain pydantic, aliased to env names."""

import secrets
from collections.abc import Callable, Mapping
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator

from sandloom.sdk.module import (  # sandloom: advanced settings metadata
    RemovedSetting,
    Risk,
    SemanticSetting,
)


class InventorySettings(BaseModel):
    api_token: SecretStr | None = Field(None, alias="KB_INVENTORY_API_TOKEN")
    reserve_ttl_seconds: int = Field(
        300, ge=1, le=3600, alias="KB_INVENTORY_RESERVE_TTL_SECONDS"
    )
    allow_oversell: bool = Field(False, alias="KB_INVENTORY_ALLOW_OVERSELL")
    warehouse: str = Field("main", alias="KB_INVENTORY_WAREHOUSE")

    semantic: ClassVar[tuple[SemanticSetting, ...]] = (
        SemanticSetting(
            "KB_INVENTORY_ALLOW_OVERSELL",
            "Reservations may exceed the stock on hand.",
            profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
            strength=(True, False),
            risk="oversell_in_prod",
        ),
    )
    risks: ClassVar[tuple[Risk, ...]] = (
        Risk(
            "oversell_in_prod",
            "KB_INVENTORY_ALLOW_OVERSELL=true in prod: orders may reserve stock "
            "that does not exist.",
            "InventorySettings production check",
        ),
    )
    removed: ClassVar[tuple[RemovedSetting, ...]] = (
        RemovedSetting(
            "KB_INVENTORY_LEGACY_SYNC",
            "0.2",
            "stock sync is always event-driven now",
        ),
    )

    # What the kernel's tools read from the declaration (KB-55, P14).
    console: ClassVar[tuple[str, ...]] = ("warehouse",)
    shared: ClassVar[tuple[str, ...]] = ("KB_INVENTORY_WAREHOUSE",)
    backup: ClassVar[tuple[tuple[str, str], ...]] = (
        (
            "KB_INVENTORY_API_TOKEN",
            "losing KB_INVENTORY_API_TOKEN: stock sync stops until it is re-issued",
        ),
    )
    deploy: ClassVar[tuple[str, ...]] = (
        "# inventory: the warehouse it reserves from, and its sync token.",
        "KB_INVENTORY_WAREHOUSE: ${KB_INVENTORY_WAREHOUSE:-main}",
        "KB_INVENTORY_API_TOKEN: ${KB_INVENTORY_API_TOKEN:?set KB_INVENTORY_API_TOKEN}",
    )
    dev_env: ClassVar[Mapping[str, str | Callable[[], str]]] = {
        "KB_INVENTORY_API_TOKEN": lambda: "dev-" + secrets.token_hex(8)
    }
    dev_risks: ClassVar[tuple[str, ...]] = ("oversell_in_prod",)

    @model_validator(mode="after")
    def _production(self, info: ValidationInfo) -> "InventorySettings":
        context = info.context or {}
        if (
            context.get("environment") == "prod"
            and self.allow_oversell
            and not context["accepts_risk"]("oversell_in_prod")
        ):
            raise ValueError(
                "KB_INVENTORY_ALLOW_OVERSELL=true is not allowed with "
                "ENVIRONMENT=prod (risk kb_inventory.oversell_in_prod)"
            )
        return self
