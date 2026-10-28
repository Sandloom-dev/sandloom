"""Boot check of a split deployment, run by ``check_p5_scale_switch.py`` with
the APP's interpreter (``<app>/.venv/bin/python _boot_services.py``), never
imported by the checker itself.

stdin: ``{"<service>": ["<dotted module>", ...], ...}`` (each HTTP service's
``SANDLOOM_MODULES``). For each service it loads exactly those modules into a
fresh, I/O-free facade (registration only, as the framework's own
deploy-profile test does) and reports the contracts they consume but do not
provide (each must be bound in that service's ``SERVICE_BINDINGS``).

stdout: ``{"<service>": {"ok": bool, "error": str, "consumed": [...]}}``.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any


def boot(modules: list[str]) -> dict[str, Any]:
    from fastapi import FastAPI

    from sandloom.core.config.settings import settings
    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader.plugin_loader import load_module_topology
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    settings.module_load_entrypoints = False
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    summary = load_module_topology(core, modules)
    loaded = {d.name for d in summary.loaded}
    missing = [m for m in modules if m not in loaded]
    if missing or summary.failed:
        failed = [f"{f.name}: {f.error}" for f in summary.failed]
        return {
            "ok": False,
            "error": f"not loaded: {missing}; failed: {failed}"[:800],
            "consumed": [],
        }
    consumed = sorted(
        {
            entry["contract"]
            for entries in summary.consumes.values()
            for entry in entries
            if entry["resolution"] != "local"
        }
    )
    return {"ok": True, "error": "", "consumed": consumed}


def main() -> None:
    services: dict[str, list[str]] = json.load(sys.stdin)
    out: dict[str, Any] = {}
    for name, modules in services.items():
        try:
            out[name] = boot(modules)
        except Exception as exc:  # reported, the checker fails the service
            tail = traceback.format_exc().splitlines()[-3:]
            out[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc} | {' / '.join(tail)}"[:800],
                "consumed": [],
            }
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
