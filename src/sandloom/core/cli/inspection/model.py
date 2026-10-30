"Versioned data, diagnostics and deterministic bytes shared by both tools."

from __future__ import annotations

import hashlib
import json
from typing import Any

Doc = dict[str, Any]
INDEX = ".sandloom/inspection.json"
RECEIPT = ".sandloom/cache/inspection-receipt.json"
LIMIT = 131072


def encode(value: Any) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def digest(value: Any) -> str:
    return sha(encode(value).encode())


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class InspectionError(ValueError):
    "A safe diagnostic: never put rejected source contents in the message."

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        code: int = 1,
        actions: list[Doc] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.exit_code = code
        self.actions = actions or []

    def diagnostic(self) -> Doc:
        return {
            "kind": self.kind,
            "severity": "error",
            "code": "SANDLOOM-T015",
            "message": str(self),
            "next_actions": self.actions,
        }


def regenerate() -> list[Doc]:
    return [
        {
            "action": "generate",
            "argv": ["sandloom", "generate"],
            "side_effects": ["writes_generated_files", "loads_application"],
        }
    ]


def response(
    command: str,
    kind: str,
    data: Any,
    *,
    selection: Doc | None = None,
    freshness: Doc | None = None,
    diagnostics: list[Doc] | None = None,
) -> Doc:
    return {
        "schema": "sandloom.tool_response",
        "schema_version": 1,
        "command": command,
        "ok": kind != "error",
        "kind": kind,
        "selection": selection or {},
        "freshness": freshness,
        "data": data,
        "diagnostics": diagnostics or [],
    }
