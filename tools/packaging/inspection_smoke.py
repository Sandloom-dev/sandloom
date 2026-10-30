"""Installed-wheel cold-tool smoke with a recorded, environment-free module."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def check(python: Path, work: Path) -> None:
    root = work / "cold-inspection"
    module = root / "probe/modules/notes"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text(
        'raise AssertionError("application imported")\n'
    )
    (module / "services.py").write_text("from sandloom.sdk import db\n")
    (root / "AGENTS.md").write_text("Use the public SDK; keep modules isolated.\n")
    policy = (
        b"root: .\nsource_root: .\nmodules_path: probe/modules\n"
        b"modules_package: probe.modules\nshared_package: probe.shared\n"
    )
    (root / "architecture_rules.yml").write_bytes(policy)
    node = {
        "id": "module:notes",
        "name": "notes",
        "kind": "module",
        "import_path": "probe.modules.notes",
    }
    doc = {
        "schema": "sandloom.inspection",
        "schema_version": 1,
        "producer_protocol": 1,
        "layout": {
            "source_root": ".",
            "modules_path": "probe/modules",
            "modules_package": "probe.modules",
            "shared_package": "probe.shared",
            "shared_dir": "probe/shared",
            "role_contract": "AGENTS.md",
            "policy_sha256": hashlib.sha256(policy).hexdigest(),
        },
        "scope": {"kind": "authoring_composition", "module_ids": [node["id"]]},
        "nodes": [node],
        "edges": [],
        "wire_documents": {},
        "event_schemas": {},
        "unresolved": [],
        "processes": [
            {
                "id": "route:notes%3AGET%20%2Fnotes",
                "owner": "module:notes",
                "name": "GET /notes",
                "evidence_kind": "resolved_route_policy",
                "handler": "list_notes",
                "policy": {"effect": "read", "session": "live", "idempotency": "none"},
            }
        ],
        "modules": [
            {
                **node,
                "source": "probe/modules/notes",
                "summary": "Notes purpose.",
                "version": "1",
                "optional": False,
                "wire_paths": [],
                "event_views": [],
                "models": [],
                "sagas": [],
            }
        ],
    }
    channel = "stats.changed.v1"
    schema = {"type": "object", "properties": {"note_id": {"type": "string"}}}
    wire = "probe/shared/events/stats.changed.v1.json"
    (root / wire).parent.mkdir(parents=True)
    (root / wire).write_text(json.dumps(schema))
    doc["nodes"] += [
        {"id": "event:" + channel, "kind": "event", "name": channel},
        {"id": "consumer:notes.changed", "kind": "consumer", "name": "notes.changed"},
    ]
    doc["edges"] = [
        {
            "id": "edge:owns",
            "kind": "owns_consumer",
            "owner": node["id"],
            "source": node["id"],
            "target": "consumer:notes.changed",
        },
        {
            "id": "edge:input",
            "kind": "consumes_event",
            "owner": node["id"],
            "source": "event:" + channel,
            "target": "consumer:notes.changed",
        },
    ]
    doc["modules"][0]["wire_paths"] = [wire]
    doc["event_schemas"] = {channel: schema}
    (root / ".sandloom").mkdir()
    (root / ".sandloom/inspection.json").write_text(json.dumps(doc))
    (root / "before.json").write_text(json.dumps(doc))
    code = """
import sys, socket, json
class Deny:
    def find_spec(self, fullname, path=None, target=None):
        prefixes = ('probe', 'yaml', 'sandloom.sdk', 'sandloom.core.config',
                    'sandloom.core.cli.generated')
        if fullname.startswith(prefixes):
            raise AssertionError('cold import: ' + fullname)
sys.meta_path.insert(0, Deny())
socket.socket = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from sandloom.core.cli.inspection.cli import main
raise SystemExit(main(sys.argv[1], sys.argv[2:]))
"""
    for command, args in (
        ("context", ["notes", "--format", "json"]),
        ("graph", ["notes"]),
        ("graph", ["impact", "module:notes"]),
        ("graph", []),
        ("graph", ["--format", "text"]),
        ("graph", ["sequence",]),
        ("graph", ["sequence", "route:notes%3AGET%20%2Fnotes"]),
        ("graph", ["diff", "before.json"]),
        ("graph", ["impact", "module:notes"]),
        ("graph", ["--format", "html"]),
    ):
        if command == "graph" and "--format" not in args:
            args = [*args, "--format", "json"]
        done = subprocess.run(  # noqa: S603 - static argv; interpreter belongs to the isolated wheel venv
            [str(python), "-c", code, command, *args],
            cwd=root,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "html" in args:
            valid = "Architecture explorer" in done.stdout
        elif "text" in args:
            valid = "notes: Notes purpose." in done.stdout
        else:
            valid = json.loads(done.stdout)["ok"]
        if done.returncode or not valid:
            raise RuntimeError(
                f"Installed inspection {command}: {done.stderr} {done.stdout}"
            )
    print(
        "Installed inspection OK: cold context, text/JSON graph and offline viewer",
        flush=True,
    )
