from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator

from sandloom.sdk.module import RemovedSetting, Risk, SemanticSetting


class CatalogSettings(BaseModel):
    api_key: SecretStr | None = Field(default=None, alias="CATALOG_API_KEY")
    page_size: int = Field(default=50, ge=1, le=500, alias="CATALOG_PAGE_SIZE")
    show_unpublished: bool = Field(default=False, alias="CATALOG_SHOW_UNPUBLISHED")

    semantic: ClassVar[tuple[SemanticSetting, ...]] = (
        SemanticSetting(
            "CATALOG_SHOW_UNPUBLISHED",
            "drafts become visible to readers",
            profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
            strength=(True, False),
        ),
    )
    risks: ClassVar[tuple[Risk, ...]] = (
        Risk(
            "show_unpublished_in_prod",
            "unpublished drafts are visible in production",
            "the production check of catalog settings",
        ),
    )
    removed: ClassVar[tuple[RemovedSetting, ...]] = (
        RemovedSetting("CATALOG_LEGACY_INDEX", "0.2", "the index is always rebuilt from events"),
    )

    @model_validator(mode="after")
    def _prod_check(self, info: ValidationInfo) -> CatalogSettings:
        ctx = info.context or {}
        if (
            ctx.get("environment") == "prod"
            and self.show_unpublished
            and not ctx["accepts_risk"]("show_unpublished_in_prod")
        ):
            raise ValueError("CATALOG_SHOW_UNPUBLISHED=true is not allowed with ENVIRONMENT=prod")
        return self
