"""KB-49 fixture module (outside src/): a secret, a bounded ops setting, a
semantic setting, a namespaced risk, a production check, a removed setting."""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI, settings_of

from .settings import InventorySettings


class SupplierClient:
    """Built in setup, connects lazily (ADR-0015 activation rule)."""

    def __init__(self, token: str | None, warehouse: str) -> None:
        self.token = token
        self.warehouse = warehouse


CLIENT: SupplierClient | None = None


def _setup(core: ModuleAPI) -> None:
    global CLIENT
    cfg = settings_of(InventorySettings)
    token = cfg.api_token.get_secret_value() if cfg.api_token else None
    CLIENT = SupplierClient(token, cfg.warehouse)


register = define_module(
    __name__, version="0.1.0", settings=InventorySettings, setup=_setup
)
