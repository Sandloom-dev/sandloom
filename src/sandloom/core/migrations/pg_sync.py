"""The sync PostgreSQL driver (psycopg2), imported when a tool first uses it.

psycopg2 serves the PostgreSQL ops tools only (``sandloom migrate``, backup,
extract-module, ``sandloom dev``) and the test harness. The application never
touches it, so a process that boots the app, or only parses a command line,
imports none of it. A tool names the driver through the lazy modules below
(``psycopg2``, ``errors``, ``extras``, ``sql``): the real module loads on the
first attribute read (``psycopg2.connect``, ``sql.SQL``), never at import.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any


class LazyModule:
    """A module that imports on its first attribute read."""

    __slots__ = ("_load", "_module")

    def __init__(self, load: Callable[[], ModuleType]) -> None:
        self._load = load
        self._module: ModuleType | None = None

    def __getattr__(self, attribute: str) -> Any:
        module = self._module
        if module is None:
            module = self._module = self._load()
        return getattr(module, attribute)


# One literal import per module: the backend-import ratchet
# (tests/architecture/test_kernel_backend_imports.py) counts them here.
psycopg2 = LazyModule(lambda: importlib.import_module("psycopg2"))
errors = LazyModule(lambda: importlib.import_module("psycopg2.errors"))
extras = LazyModule(lambda: importlib.import_module("psycopg2.extras"))
sql = LazyModule(lambda: importlib.import_module("psycopg2.sql"))

__all__ = ["LazyModule", "errors", "extras", "psycopg2", "sql"]
