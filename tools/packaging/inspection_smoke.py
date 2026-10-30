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
        ("graph", ["--view", "modules"]),
        ("graph", ["--view", "sequence"]),
        ("graph", ["--view", "sequence", "--process", "route:notes%3AGET%20%2Fnotes"]),
        ("graph", ["--view", "diff", "--before", "before.json"]),
        ("graph", ["--view", "impact", "--item", "module:notes"]),
        ("context", ["notes", "--focus", "module:notes", "--format", "json"]),
        ("context", ["notes", "--focus", "consumer:notes.changed", "--format", "json"]),
        ("graph", ["--format", "html", "--output", "map.html"]),
    ):
        done = subprocess.run(  # noqa: S603 - static argv; interpreter belongs to the isolated wheel venv
            [str(python), "-c", code, command, *args],
            cwd=root,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        if done.returncode or not json.loads(done.stdout)["ok"]:
            raise RuntimeError(
                f"Installed inspection {command}: {done.stderr} {done.stdout}"
            )
    if "Architecture explorer" not in (root / "map.html").read_text():
        raise RuntimeError("Offline viewer asset missing from installed wheel")
    print(
        "Installed inspection OK: cold context, catalog, graph and offline viewer",
        flush=True,
    )
