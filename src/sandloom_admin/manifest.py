"""The console's generated file: ``<app>/shared/admin-manifest.json``.

The console declares it on its registration (``__init__.py``); ``sandloom
generate`` writes it and ``sandloom test`` fails when it is stale
(SANDLOOM-T016). Only a process that composes every module can write it, and
every process then serves the same screens whichever modules it hosts.
"""

from __future__ import annotations

import json
from typing import Any

from .compiler import compile_manifest

#: The file name in the app's shared package (the console serves it at
#: ``/admin/admin-manifest.json``).
FILE_NAME = "admin-manifest.json"


def render(source: Any) -> str:
    """The manifest's text from the kernel's data (``GenerationInput``)."""
    return (
        json.dumps(compile_manifest(source), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
