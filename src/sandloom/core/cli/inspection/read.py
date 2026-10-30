"Cold validated snapshot reader and local verification. Standard library only."

from __future__ import annotations

import ast
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

from sandloom.core.cli.inspection import files
from sandloom.core.cli.inspection.model import (
    INDEX,
    RECEIPT,
    Doc,
    InspectionError,
    digest,
    encode,
    regenerate,
    sha,
)

CATALOG = Path(__file__).with_name("sdk-catalog.json")
RECEIPT_SCHEMA = "sandloom.inspection_receipt"


def _unique(pairs: list[tuple[str, Any]]) -> Doc:
    result: Doc = {}
    for key, value in pairs:
        if key in result:
            raise InspectionError(
                "invalid_artifact", "Duplicate JSON keys in inspection input"
            )
        result[key] = value
    return result


def parse(text: str) -> Doc:
    value = json.loads(text, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise InspectionError("invalid_artifact", "Expected a JSON object")
    return value


def _hashes(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
        for k, v in value.items()
    )


def _receipt_basis(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("semantic_digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["semantic_digest"]) is not None
        and _hashes(value.get("source"))
        and _hashes(value.get("wire"))
        and isinstance(value.get("distributions"), dict)
        and all(
            isinstance(k, str) and (v is None or isinstance(v, str))
            for k, v in value["distributions"].items()
        )
    )


def _read_receipt(root: Path) -> Doc:
    if not (root / RECEIPT).is_file():
        return {}
    receipt = parse(files.read(root, RECEIPT))
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
    ):
        raise InspectionError(
            "unsupported_receipt",
            "Unsupported verification receipt; regenerate",
            actions=regenerate(),
        )
    modules, issues = receipt.get("modules"), receipt.get("issues")
    if (
        not isinstance(modules, dict)
        or not all(isinstance(k, str) and _receipt_basis(v) for k, v in modules.items())
        or not isinstance(issues, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in issues.items())
    ):
        raise InspectionError(
            "invalid_receipt",
            "Invalid verification receipt; regenerate",
            actions=regenerate(),
        )
    return receipt


class Reader:
    def __init__(self, root: Path, document: Doc | None = None) -> None:
        self.root = root.resolve()
        if document is None and not (self.root / INDEX).is_file():
            raise InspectionError(
                "missing_index",
                "No inspection index; run sandloom generate",
                actions=regenerate(),
            )
        self.doc = (
            document if document is not None else parse(files.read(self.root, INDEX))
        )
        if (
            self.doc.get("schema") != "sandloom.inspection"
            or self.doc.get("schema_version") != 1
        ):
            raise InspectionError(
                "unsupported_schema",
                "Unsupported inspection schema; regenerate with this framework",
                actions=regenerate(),
            )
        self.layout: Doc = self.doc["layout"]
        for key in ("source_root", "modules_path", "shared_dir"):
            files.safe(self.root, self.layout[key])
        policy = sha(files.read(self.root, "architecture_rules.yml").encode())
        if policy != self.layout["policy_sha256"]:
            raise InspectionError(
                "layout_changed",
                (
                    "Architecture layout changed; regenerate before resolving source "
                    "ownership"
                ),
                actions=regenerate(),
            )
        self.modules: list[Doc] = self.doc["modules"]
        ids = [node["id"] for node in self.doc["nodes"]]
        if len(ids) != len(set(ids)) or len({m["id"] for m in self.modules}) != len(
            self.modules
        ):
            raise InspectionError("invalid_artifact", "Duplicate semantic identities")
        for edge in self.doc["edges"]:
            if edge["source"] not in ids or edge["target"] not in ids:
                raise InspectionError(
                    "invalid_artifact", "Graph edge references an absent node"
                )
        self._validate_processes()
        self._validate_graph()
        self._validate_sources()
        self.catalog = parse(CATALOG.read_text(encoding="utf-8"))

    def _validate_processes(self) -> None:
        processes = self.doc.get("processes", [])
        ids = [p["id"] for p in processes]
        owners = {m["id"] for m in self.modules}
        if len(ids) != len(set(ids)) or any(
            p["owner"] not in owners
            or not p["id"].startswith(("saga:", "route:"))
            or not isinstance(p["name"], str)
            for p in processes
        ):
            raise InspectionError(
                "invalid_artifact", "Invalid process ownership/identity"
            )

    def _validate_graph(self) -> None:
        node_kinds = {
            "module",
            "contract",
            "event",
            "consumer",
            "projection",
            "capability",
        }
        edge_kinds = {
            "provides_contract",
            "requires_contract",
            "produces_event",
            "consumes_event",
            "owns_consumer",
            "projects_event",
            "owns_projection",
            "requires_capability",
            "declares_ordering",
        }
        if any(
            n["kind"] not in node_kinds or not isinstance(n["name"], str)
            for n in self.doc["nodes"]
        ):
            raise InspectionError(
                "unsupported_schema", "Unknown required graph node variant"
            )
        if any(e["kind"] not in edge_kinds for e in self.doc["edges"]):
            raise InspectionError(
                "unsupported_schema", "Unknown required graph edge variant"
            )
        if any(
            e["kind"] == "requires_contract" and not isinstance(e["required"], bool)
            for e in self.doc["edges"]
        ):
            raise InspectionError(
                "invalid_artifact", "Contract requiredness must be boolean"
            )

    def _validate_sources(self) -> None:
        for module in self.modules:
            source = module["source"]
            if not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module["import_path"]
            ):
                raise InspectionError(
                    "invalid_artifact", "Invalid module import identity"
                )
            if source is not None:
                path = files.safe(self.root, source)
                expected = files.safe(self.root, self.layout["source_root"]) / Path(
                    *module["import_path"].split(".")
                )
                if path != expected or not path.is_relative_to(
                    files.safe(self.root, self.layout["modules_path"])
                ):
                    raise InspectionError(
                        "unsafe_path",
                        "Module source does not match its declared package ownership",
                    )
                if not path.is_relative_to(
                    files.safe(self.root, self.layout["source_root"])
                ):
                    raise InspectionError(
                        "unsafe_path",
                        "Module source is outside the declared source root",
                    )
        roots = [m["source"] for m in self.modules if m["source"]]
        if len(set(roots)) != len(roots) or any(
            Path(a).is_relative_to(Path(b)) for a in roots for b in roots if a != b
        ):
            raise InspectionError(
                "invalid_artifact", "Two modules claim one source root"
            )

    def module(self, selector: str) -> Doc:
        matches = [
            m
            for m in self.modules
            if selector in (m["id"], m["name"], m["import_path"])
        ]
        if len(matches) != 1:
            raise InspectionError(
                "unknown_module" if not matches else "ambiguous_module",
                "Choose an unambiguous module ID from sandloom graph --view modules",
                actions=[
                    {
                        "action": "list_modules",
                        "argv": ["sandloom", "graph", "--view", "modules"],
                        "side_effects": [],
                    }
                ],
            )
        return matches[0]

    def owned_paths(self, module: Doc) -> list[str]:
        if not module["source"]:
            raise InspectionError(
                "installed_only_module",
                (
                    "Context requires a checkout-owned business module; installed "
                    "dependencies contribute public interfaces only"
                ),
            )
        if not files.safe(self.root, module["source"]).is_dir():
            raise InspectionError(
                "missing_source", "Selected module source directory is absent"
            )
        return files.candidates(self.root, module["source"])

    def rules(self, module: Doc) -> list[str]:
        return (
            files.instructions(
                self.root, module["source"], self.layout.get("role_contract")
            )
            if module["source"]
            else []
        )

    def _import_path(self, name: str) -> str | None:
        package = self.layout["shared_package"]
        if name == package or name.startswith(package + "."):
            tail = name[len(package) :].lstrip(".").split(".")
            path = (
                Path(self.layout["shared_dir"])
                .joinpath(*tail)
                .with_suffix(".py")
                .as_posix()
            )
            if (self.root / path).is_file():
                return path
            if (self.root / path.removesuffix(".py") / "__init__.py").is_file():
                return path.removesuffix(".py") + "/__init__.py"
        return None

    def wire_paths(self, module: Doc) -> list[str]:
        "Whole wire modules preserve recursive/alias/default closure without eval."
        pending = list(module["wire_paths"])
        seen: set[str] = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            if not path.startswith(self.layout["shared_dir"] + "/"):
                raise InspectionError(
                    "unsafe_wire", "Wire input is outside the generated shared package"
                )
            if not (self.root / path).is_file():
                channel = Path(path).stem
                if path.endswith(".json") and any(
                    v["channel"] == channel for v in module["event_views"]
                ):
                    continue  # The explicit local view remains mandatory context.
                raise InspectionError(
                    "missing_wire",
                    f"Mandatory wire input missing: {path}",
                    actions=regenerate(),
                )
            seen.add(path)
            text = files.read(self.root, path)
            if path.endswith(".json"):
                self._schema_refs(parse(text))
            else:
                pending += self._wire_imports(path, text)
        return sorted(seen)

    def _wire_imports(self, path: str, text: str) -> list[str]:
        result = []
        shared = self.layout["shared_package"]
        for node in ast.walk(ast.parse(text, filename=path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            name = node.module or ""
            if node.level:
                package = (
                    shared
                    + "."
                    + ".".join(
                        Path(path).relative_to(self.layout["shared_dir"]).parts[:-1]
                    )
                )
                parts = package.rstrip(".").split(".")
                name = ".".join(
                    parts[: len(parts) - node.level + 1] + ([name] if name else [])
                )
            allowed_external = (
                name.split(".")[0] in sys.stdlib_module_names
                or name.split(".")[0] == "pydantic"
                or name == "sandloom.core.descriptors.contract_refs"
            )
            if not (
                name == shared or name.startswith(shared + ".") or allowed_external
            ):
                raise InspectionError(
                    "unsupported_wire_import",
                    f"External wire definitions need a local catalog: {name}",
                )
            if name == shared or name.startswith(shared + "."):
                dependency = self._import_path(name)
                if dependency:
                    result.append(dependency)
                else:
                    submodules = [
                        self._import_path(name + "." + alias.name)
                        for alias in node.names
                    ]
                    if not submodules or any(p is None for p in submodules):
                        raise InspectionError(
                            "incomplete_wire", f"Cannot resolve wire import in {path}"
                        )
                    result += [p for p in submodules if p is not None]
        return result

    def _schema_refs(self, value: Any, root: Doc | None = None) -> None:
        if root is None:
            root = value
        if isinstance(value, dict):
            ref = value.get("$ref")
            if ref is not None:
                if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
                    raise InspectionError(
                        "unsupported_schema_ref",
                        "External schema references require a local catalog; "
                        "no URLs are fetched",
                    )
                current: Any = root
                for part in [] if ref == "#" else ref[2:].split("/"):
                    if (
                        not isinstance(current, dict)
                        or part.replace("~1", "/").replace("~0", "~") not in current
                    ):
                        raise InspectionError(
                            "incomplete_wire", "Unresolved local schema reference"
                        )
                    current = current[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                self._schema_refs(child, root)
        elif isinstance(value, list):
            for child in value:
                self._schema_refs(child, root)

    def basis(self, module: Doc) -> Doc:
        source_paths = self.owned_paths(module) if module["source"] else []
        paths = source_paths + self.rules(module)
        paths += [
            p
            for p in (
                "architecture_rules.yml",
                "pyproject.toml",
                "uv.lock",
                ".sandloomignore",
            )
            if (self.root / p).is_file()
        ]
        wire_paths = self.wire_paths(module)
        if module["source"]:
            wire_paths += [
                str(Path(module["source"]) / name)
                for name in (
                    "module.snapshot.json",
                    "routes.snapshot.json",
                    "contracts.snapshot.json",
                )
                if (self.root / module["source"] / name).is_file()
            ]
        related = {e["target"] for e in self.doc["edges"] if e["owner"] == module["id"]}
        related |= {
            e["source"] for e in self.doc["edges"] if e["owner"] == module["id"]
        }
        semantic = {
            "module": module,
            "edges": [e for e in self.doc["edges"] if e["owner"] == module["id"]],
            "layout": self.layout,
            "processes": [
                p for p in self.doc.get("processes", []) if p["owner"] == module["id"]
            ],
            "contract_descriptions": {
                k: v
                for k, v in self.doc.get("contract_descriptions", {}).items()
                if "contract:" + k in related
            },
            "catalog": digest(self.catalog),
            "providers": [
                e
                for e in self.doc["edges"]
                if e["kind"] in {"provides_contract", "produces_event"}
                and e["target"] in related
            ],
            "event_schemas": {
                k: v
                for k, v in self.doc.get("event_schemas", {}).items()
                if "event:" + k in related
            },
        }
        return {
            "semantic_digest": digest(semantic),
            "source": files.fingerprints(self.root, paths),
            "wire": files.fingerprints(self.root, wire_paths),
            "distributions": self._distributions(module, related),
        }

    def _distributions(self, module: Doc, related: set[str]) -> Doc:
        owners = {
            e["source"]
            for e in self.doc["edges"]
            if e["kind"] == "provides_contract" and e["target"] in related
        } | {module["id"]}
        names = {
            name
            for m in self.modules
            if m["id"] in owners
            for name in m.get("distributions", {})
        }
        result: Doc = {}
        for name in sorted(names):
            try:
                result[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                result[name] = None
        return result

    def freshness(
        self,
        selected: list[Doc],
        *,
        require: bool = False,
        full: bool = False,
        tolerate_missing: bool = False,
    ) -> Doc:
        receipt = _read_receipt(self.root)
        states = {}
        bases: dict[str, str | None] = {}
        issues: dict[str, str] = {}
        for module in selected:
            try:
                basis = self.basis(module)
            except InspectionError as failure:
                if not tolerate_missing or failure.kind not in {
                    "missing_wire",
                    "incomplete_wire",
                    "unsupported_schema_ref",
                    "unsupported_wire_import",
                    "missing_source",
                }:
                    raise
                states[module["id"]] = "partial"
                bases[module["id"]] = None
                issues[module["id"]] = failure.kind
                continue
            bases[module["id"]] = basis["semantic_digest"]
            old = receipt.get("modules", {}).get(module["id"])
            state = "missing_receipt"
            if old is not None:
                state = "verified"
                if old.get("source") != basis["source"]:
                    state = "source_changed"
                elif (
                    old.get("wire") != basis["wire"]
                    or old.get("semantic_digest") != basis["semantic_digest"]
                    or old.get("distributions") != basis["distributions"]
                ):
                    state = "dependency_changed"
            expected = self.doc["wire_documents"]
            if any(
                p in expected and expected[p]["sha256"] != h
                for p, h in basis["wire"].items()
            ):
                state = "dependency_changed"
            states[module["id"]] = state
        unique = set(states.values())
        state = next(iter(unique)) if len(unique) == 1 else "partial"
        result = {
            "state": state,
            "modules": states,
            "semantic_digest": digest(bases),
            "basis": "enumerated authoring source; not live deployment",
        }
        result["issues"] = issues
        if full:
            result["evidence"] = {
                m["id"]: self.basis(m) for m in selected if m["id"] not in issues
            }
        if require and state != "verified":
            raise InspectionError(
                "verification_unavailable",
                "Selected scope is not verified; review inputs and explicitly generate",
                code=3,
                actions=regenerate(),
            )
        return result

    def receipt(self) -> str:
        modules: Doc = {}
        issues: Doc = {}
        for module in self.modules:
            try:
                modules[module["id"]] = self.basis(module)
            except InspectionError as failure:
                issues[module["id"]] = failure.kind
        return encode(
            {
                "schema": RECEIPT_SCHEMA,
                "schema_version": 1,
                "modules": modules,
                "issues": issues,
            }
        )
