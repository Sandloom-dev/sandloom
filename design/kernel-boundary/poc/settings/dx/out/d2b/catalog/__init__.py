"""The catalog module (DX task)."""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI

from catalog.settings import CatalogSettings


def _setup(core: ModuleAPI) -> None:
    CatalogSettings.current().api_key


register = define_module(__name__, version="0.1.0", setup=_setup, settings=CatalogSettings)
