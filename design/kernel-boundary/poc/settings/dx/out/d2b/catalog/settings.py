from pydantic import SecretStr

from sandloom_d2b import CheckContext, ModuleSettings, RemovedSetting, Risk, Semantic, prod_check, setting


class CatalogSettings(ModuleSettings):
    api_key: SecretStr | None = setting(None, env="CATALOG_API_KEY")
    page_size: int = setting(50, env="CATALOG_PAGE_SIZE", ge=1, le=500)
    show_unpublished: bool = setting(
        False,
        env="CATALOG_SHOW_UNPUBLISHED",
        semantic=Semantic(why="drafts become visible", weaker=True),
    )
    risks = (
        Risk("show_unpublished_in_prod", "unpublished drafts become visible", "catalog queries"),
    )
    removed = (RemovedSetting("CATALOG_LEGACY_INDEX", "0.2", "the index is always rebuilt from events"),)

    @prod_check
    def _no_unpublished(self, ctx: CheckContext) -> str | None:
        if self.show_unpublished and not ctx.accepts_risk("show_unpublished_in_prod"):
            return "CATALOG_SHOW_UNPUBLISHED=true is not allowed with ENVIRONMENT=prod"
        return None
