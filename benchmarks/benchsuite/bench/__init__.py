"""The Sandloom benchmark module (``bench``), wired by convention via define_module.

Router from ``api/``, the S4 event schema from ``events.py``. Its tables
(``bench_items``, ``bench_writes``) are created by ``benchsuite.setup_db`` so
every implementation reads/writes the very same tables. With ``BENCH_S5=1`` the
module also declares its execution-planner operation (planned at startup, its
executor logged and listed in ``/diagnostics/core``).

DIAGNOSTIC stands (never production): ``BENCH_RPC_DIAG=1`` registers the RPC
cost-decomposition contract ``bench.rpcdiag.v1`` (``rpc_diag.py``);
``BENCH_WRITE_DIAG=1`` (with ``BENCH_S5=1``) registers the write-path stand's
command contract ``bench.writediag.v1`` (``write_diag.py``; routes in
``api/diag_write.py``). ``BENCH_HOTPATH_CONSUMER=1`` registers the hot-path
stand's durable event consumer (``hotpath.py``).
"""

import os
from typing import Any

from sandloom.core.module_api import define_module


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _setup(core: Any) -> None:
    if _flag("BENCH_S5"):
        from .operations import get_tenant_item, list_tenant_items

        core.register_operations(get_tenant_item, list_tenant_items)
        if _flag("BENCH_WRITE_DIAG"):
            from .write_diag import SERVICE as WRITE_SERVICE
            from .write_diag import WriteDiagContract

            core.register_service(WRITE_SERVICE, WriteDiagContract)
    if _flag("BENCH_RPC_DIAG"):
        from .rpc_diag import SERVICE as RPC_SERVICE
        from .rpc_diag import RpcDiagContract

        core.register_service(RPC_SERVICE, RpcDiagContract)
    if _flag("BENCH_HOTPATH_CONSUMER"):
        from . import hotpath

        hotpath.register(core)


register = define_module(__name__, version="0.1.0", setup=_setup)
