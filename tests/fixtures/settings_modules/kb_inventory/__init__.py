"""Inventory: a fixture module that declares its own settings (ADR-0015)."""

from kb_inventory.settings import InventorySettings
from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI, settings_of  # sandloom: advanced settings


def _setup(core: ModuleAPI) -> None:
    # A setup reader: the module's own settings are bound while it registers.
    ttl = settings_of(InventorySettings).reserve_ttl_seconds
    core.logger.info("kb_inventory reserve ttl %ss", ttl)


register = define_module(
    __name__,
    version="0.1.0",
    optional=True,
    settings=InventorySettings,
    setup=_setup,
)
