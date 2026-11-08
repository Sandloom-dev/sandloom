"""The catalog module (DX task)."""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI


def _setup(core: ModuleAPI) -> None:
    pass


register = define_module(__name__, version="0.1.0", setup=_setup)
