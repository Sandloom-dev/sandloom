"Build the shipped cold-reader SDK catalog during authoring, never at build time."

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from tools.docs import docs_reference

TARGET = (
    Path(__file__).resolve().parents[2]
    / "src/sandloom/core/cli/inspection/sdk-catalog.json"
)


def render() -> str:
    definitions: dict[str, Any] = {}
    for module in docs_reference._public_modules():
        for name in sorted(module.__all__):
            obj = getattr(module, name)
            opaque = docs_reference._opaque(obj)
            signature = None if opaque else docs_reference._signature(obj)
            doc = (
                "Lazy framework settings; read through the SDK facade."
                if opaque
                else (inspect.getdoc(obj) or "")
            )
            tier = (
                "beginner"
                if module.__name__ in {"sandloom.sdk", "sandloom.sdk.testing"}
                and name in module.__beginner__
                else "advanced"
            )
            definitions[f"{module.__name__}.{name}"] = {
                "tier": tier,
                "signature": name
                + (signature or " (see sandloom docs for this value/type)"),
                "summary": doc.split("\n\n")[0],
            }
    return (
        json.dumps(
            {
                "schema": "sandloom.sdk_catalog",
                "schema_version": 1,
                "definitions": definitions,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
