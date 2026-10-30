"""Exercise cold CLI exports, ownership, closure and generation through real files."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandloom.core.archcheck.manifest import ArchRules
from sandloom.core.cli.generated import Generated, apply
from sandloom.core.cli.inspection import collect, generation, graph
from sandloom.core.cli.inspection.cli import main
from sandloom.core.cli.inspection.model import INDEX, RECEIPT, encode
from sandloom.core.cli.inspection.read import Reader
from sandloom.core.descriptors import (
    CapabilityRequirement,
    ContractRequirement,
    ModuleDescriptor,
)
from sandloom.core.loader.manifest import (
    AppManifest,
    ConsumerEntry,
    ContractEntry,
    ContractUse,
    EventEntry,
    ModuleManifest,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path
    (root / "architecture_rules.yml").write_text(
        "root: .\nmodules_path: app/modules\nmodules_package: app.modules\nsource_root: .\nshared_package: app.shared\n"
    )
    (root / "AGENTS.md").write_text("Use the public SDK; modules stay isolated.\n")
    (root / "pyproject.toml").write_text('[project]\nname="app"\nversion="0.1.0"\n')
    for name in ("notes", "stats", "archive"):
        directory = root / "app/modules" / name
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(
            'raise RuntimeError("APPLICATION MUST NOT IMPORT")\n'
        )
        (directory / "services.py").write_text(
            "from sandloom.sdk import db\n# local implementation\n"
        )
        imported = types.ModuleType(f"app.modules.{name}")
        imported.__file__ = str(directory / "__init__.py")
        monkeypatch.setitem(sys.modules, imported.__name__, imported)
    (root / "app/modules/notes/AGENTS.md").write_text("Notes-specific scoped rule.\n")
    schema = {
        "type": "object",
        "properties": {"nested": {"$ref": "#/$defs/Value"}},
        "$defs": {
            "Value": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/Value"}},
            }
        },
    }
    manifest = AppManifest(
        (
            ModuleManifest(
                name="app.modules.notes",
                version="1",
                contracts_consumed=(
                    ContractUse("app.modules.notes", "stats.quota.v1"),
                ),
                consumers=(
                    ConsumerEntry("app.modules.notes", "stats.changed.v1", "changed"),
                ),
            ),
            ModuleManifest(
                name="app.modules.stats",
                version="1",
                contracts_provided=(
                    ContractEntry(
                        "app.modules.stats", "stats.quota.v1", methods=("read",)
                    ),
                ),
                events=(
                    EventEntry(
                        "app.modules.stats",
                        "stats.changed.v1",
                        "Changed",
                        schema=schema,
                    ),
                ),
            ),
            ModuleManifest(name="app.modules.archive", version="1"),
        )
    )
    descriptors = [
        ModuleDescriptor(
            name=m.name, version="1", capabilities=[CapabilityRequirement("database")]
        )
        for m in manifest.modules
    ]
    descriptors[0].consumes = [
        ContractRequirement("stats.quota.v1", reason="read quota")
    ]
    core = SimpleNamespace(
        module_descriptors=descriptors,
        list_event_schemas=lambda: [
            SimpleNamespace(channel="stats.changed.v1", schema=schema)
        ],
    )
    app = SimpleNamespace(state=SimpleNamespace(app_manifest=manifest, core=core))
    rules = ArchRules(
        root=root,
        modules_path=root / "app/modules",
        modules_package="app.modules",
        source_root=root,
        shared_package="app.shared",
    )
    wire = {
        "app/shared/contracts/stats_quota_v1.py": "from app.shared.contracts.stats_dto import Out\nclass Quota:\n    async def read(self) -> Out: ...\n",
        "app/shared/contracts/stats_dto.py": 'from pydantic import BaseModel, Field\nclass Out(BaseModel):\n    next: "Out | None" = None\n    aliases: dict[str, list[int]] = Field(default_factory=dict)\n',
        "app/shared/events/stats.changed.v1.json": encode(schema),
    }
    planned = [
        Generated(root / path, text, "SANDLOOM-T007") for path, text in wire.items()
    ]
    for item in planned:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content)
    document = collect.collect(app, rules, planned)
    (root / INDEX).parent.mkdir()
    (root / INDEX).write_text(encode(document))
    (root / RECEIPT).parent.mkdir()
    (root / RECEIPT).write_text(Reader(root).receipt())
    return SimpleNamespace(root=root, app=app, rules=rules, planned=planned)


def run(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    command: str,
    *args: str,
) -> tuple[int, str, str]:
    result = main(command, ["--root", str(project.root), *args])
    output = capsys.readouterr()
    return result, output.out, output.err


def test_context_has_rules_wire_closure_and_no_sibling_source(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, err = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0, err + text
    result = json.loads(text)
    assert result["freshness"]["state"] == "verified"
    paths = {s["path"] for s in result["data"]["sections"]}
    assert {
        "AGENTS.md",
        "app/modules/notes/AGENTS.md",
        "app/shared/contracts/stats_dto.py",
    } <= paths
    assert not any("modules/stats/" in p or "modules/archive/" in p for p in paths)
    assert '"Out | None"' in text.replace('\\"', '"')
    assert result["data"]["bytes"] == len(text.encode())
    assert "sandloom.sdk.db" in {s["id"] for s in result["data"]["sdk"]}


def test_markdown_default_deterministic_and_collision_safe(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/modules/notes/services.py").write_text("# ```\n# </script>\n")
    first = run(project, capsys, "context", "module:notes")
    second = run(project, capsys, "context", "app.modules.notes")
    assert first == second
    assert first[0] == 0
    assert "Freshness: source_changed" in first[1]
    assert "````python" in first[1]


def test_graph_hops_preserve_contract_and_event_witnesses(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(project, capsys, "graph", "--module", "notes", "--hops", "1")
    assert rc == 0
    data = json.loads(text)["data"]
    assert {
        "module:notes",
        "module:stats",
        "contract:stats.quota.v1",
        "event:stats.changed.v1",
    } <= {n["id"] for n in data["nodes"]}
    assert "module:archive" not in {n["id"] for n in data["nodes"]}
    ids = {e["id"] for e in data["edges"]}
    assert all(set(r["witnesses"]) <= ids for r in data["module_relations"])
    assert {r["kind"] for r in data["module_relations"]} == {
        "required_contract",
        "event_impact",
    }


def test_discovery_is_catalog_and_never_claims_empty_graph(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(project, capsys, "graph", "--view", "modules")
    data = json.loads(text)["data"]
    assert rc == 0 and data["relations_included"] is False
    assert len(data["modules"]) == 3
    assert "edges" not in data


@pytest.mark.parametrize(
    "extra,kind,code",
    [
        (["unknown"], "unknown_module", 1),
        (["notes", "--file", "../stats/services.py"], "unsafe_path", 1),
        (["notes", "--max-bytes", "20"], "budget_exceeded", 1),
        (["notes", "--max-bytes", "-1"], "usage", 2),
        (["notes", "--not-an-option"], "usage", 2),
    ],
)
def test_json_failures_are_one_structured_response(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    kind: str,
    code: int,
) -> None:
    rc, text, err = run(project, capsys, "context", "--format", "json", *extra)
    assert rc == code and not err
    result = json.loads(text)
    assert result["data"] is None and not result["ok"]
    assert result["diagnostics"][0]["kind"] == kind
    assert "APPLICATION MUST NOT IMPORT" not in text


def test_verification_source_membership_and_scoped_semantics(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/modules/archive/new.py").write_text("x = 1\n")
    assert run(project, capsys, "context", "notes", "--require-verified")[0] == 0
    doc = collect.collect(project.app, project.rules, project.planned)
    doc["modules"][-1]["version"] = (
        "2"  # stats affects wire semantics, archive is independent below.
    )
    archive = next(m for m in doc["modules"] if m["name"] == "archive")
    archive["version"] = "3"
    (project.root / INDEX).write_text(encode(doc))
    assert run(project, capsys, "context", "notes", "--require-verified")[0] == 0
    (project.root / "app/modules/notes/new.py").write_text("x = 1\n")
    assert (
        run(
            project,
            capsys,
            "context",
            "notes",
            "--require-verified",
            "--format",
            "json",
        )[0]
        == 3
    )
    assert run(project, capsys, "context", "notes")[0] == 0
    (project.root / "app/modules/notes/new.py").unlink()
    assert run(project, capsys, "context", "notes", "--require-verified")[0] == 0


def test_layout_change_fails_before_scope_resolution(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "architecture_rules.yml").write_text("modules_path: ../outside\n")
    rc, text, _ = run(project, capsys, "graph", "--view", "modules")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "layout_changed"


def test_full_detail_preserves_business_selection(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    compact = json.loads(
        run(project, capsys, "context", "notes", "--format", "json")[1]
    )
    full = json.loads(
        run(
            project, capsys, "context", "notes", "--format", "json", "--detail", "full"
        )[1]
    )
    assert [(s["path"], s["content"]) for s in compact["data"]["sections"]] == [
        (s["path"], s["content"]) for s in full["data"]["sections"]
    ]
    assert "sha256" not in compact["data"]["sections"][0]
    assert "sha256" in full["data"]["sections"][0]
    assert full["data"]["bytes"] > compact["data"]["bytes"]


def test_wire_missing_changed_and_external_refs(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    dto = project.root / "app/shared/contracts/stats_dto.py"
    dto.write_text(dto.read_text() + "# changed\n")
    assert run(project, capsys, "context", "notes", "--require-verified")[0] == 3
    dto.unlink()
    assert run(project, capsys, "context", "notes")[0] == 1
    dto.write_text("class Out: pass\n")
    (project.root / "app/shared/events/stats.changed.v1.json").write_text(
        '{"$ref":"https://example.com/schema"}'
    )
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert (
        rc == 1
        and json.loads(text)["diagnostics"][0]["kind"] == "unsupported_schema_ref"
    )


def test_symlink_and_sensitive_explicit_file_never_leak(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/modules/notes/leak.py").symlink_to(
        project.root / "app/modules/stats/services.py"
    )
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "unsafe_path"
    (project.root / "app/modules/notes/leak.py").unlink()
    (project.root / "app/modules/notes/.env").write_text("PASSWORD=never-print-this\n")
    rc, text, _ = run(
        project, capsys, "context", "notes", "--file", ".env", "--format", "json"
    )
    assert rc == 1 and "never-print-this" not in text


def test_output_atomic_protected_and_not_duplicated(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(project, capsys, "context", "notes", "--output", "bundle.md")
    assert rc == 0 and json.loads(text)["kind"] == "artifact"
    output = project.root / "bundle.md"
    before = output.read_bytes()
    assert before.startswith(b"# Sandloom context")
    assert run(project, capsys, "context", "notes", "--output", "bundle.md")[0] == 1
    assert output.read_bytes() == before
    assert (
        run(project, capsys, "context", "notes", "--output", "AGENTS.md", "--force")[0]
        == 1
    )
    assert (
        run(project, capsys, "context", "notes", "--output", INDEX, "--force")[0] == 1
    )


@pytest.mark.subprocess_probe
def test_cold_success_without_yaml_settings_or_imports(
    project: SimpleNamespace,
) -> None:
    script = """
import sys, socket
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('app', 'yaml', 'sandloom.sdk', 'sandloom.core.config', 'sandloom.core.cli.generated')):
            raise AssertionError('cold reader imported ' + fullname)
sys.meta_path.insert(0, Block())
socket.socket = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('socket opened'))
from sandloom.core.cli.inspection.cli import main
raise SystemExit(main('context', ['notes', '--format', 'json', '--require-verified']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project.root,
        env={},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["freshness"]["state"] == "verified"


def test_generation_planned_wire_bytes_and_no_write_on_break(
    project: SimpleNamespace,
) -> None:
    before = collect.capture(project.rules)
    dto = project.planned[1]
    dto.content += "# compatible planned content\n"
    result = generation.finish(
        rules=project.rules,
        app=project.app,
        selected=list(project.planned),
        modules=[],
        check=False,
        before=before,
        plan_all=lambda: project.planned,
        make_file=Generated,
        apply_files=apply,
    )
    assert result == 0
    assert (
        Reader(project.root).freshness([Reader(project.root).module("notes")])["state"]
        == "verified"
    )
    snapshots = {p: (project.root / p).read_bytes() for p in (INDEX, RECEIPT)}
    dto.problems.append("breaking field")
    assert (
        generation.finish(
            rules=project.rules,
            app=project.app,
            selected=list(project.planned),
            modules=[],
            check=False,
            before=before,
            plan_all=lambda: project.planned,
            make_file=Generated,
            apply_files=apply,
        )
        == 1
    )
    assert snapshots == {p: (project.root / p).read_bytes() for p in snapshots}


def test_partial_generation_retains_index_until_unselected_wires_current(
    project: SimpleNamespace,
) -> None:
    prior = (project.root / INDEX).read_bytes()
    prior_receipt = (project.root / RECEIPT).read_bytes()
    project.planned[1].content += "# unselected stale\n"
    assert (
        generation.finish(
            rules=project.rules,
            app=project.app,
            selected=[],
            modules=["archive"],
            check=False,
            before=collect.capture(project.rules),
            plan_all=lambda: project.planned,
            make_file=Generated,
            apply_files=apply,
        )
        == 0
    )
    assert (project.root / INDEX).read_bytes() == prior
    assert (project.root / RECEIPT).read_bytes() == prior_receipt


def test_cycles_are_typed_and_capability_sharing_is_not_a_call(
    project: SimpleNamespace,
) -> None:
    reader = Reader(project.root)
    assert not any(r["source"] == "module:archive" for r in graph.relations(reader.doc))
    links = [
        {"kind": "event_impact", "source": "a", "target": "b"},
        {"kind": "event_impact", "source": "b", "target": "a"},
    ]
    assert graph.cycles(links)[0]["module_ids"] == ["a", "b"]


def test_offline_viewer_escapes_hostile_labels(project: SimpleNamespace) -> None:
    reader = Reader(project.root)
    reader.doc["nodes"][0]["name"] = "</script><img src=x onerror=alert(1)>"
    exported = graph.viewer(
        graph.build(
            reader,
            selector=None,
            hops=1,
            discovery=False,
            detail="compact",
            require=False,
        )
    )
    assert "</script><img" not in exported
    assert "\\u003c/script>" in exported
    assert 'src="https://' not in exported
    assert 'aria-live="polite"' in exported


def test_discovery_survives_missing_wire_and_labels_uncertainty(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/shared/contracts/stats_dto.py").unlink()
    rc, text, _ = run(project, capsys, "graph", "--view", "modules")
    assert rc == 0
    result = json.loads(text)
    assert result["freshness"]["state"] == "partial"
    assert result["freshness"]["issues"]["module:notes"] == "incomplete_wire"
    assert (
        run(project, capsys, "graph", "--view", "modules", "--require-verified")[0] == 3
    )
    assert run(project, capsys, "context", "notes")[0] == 1


def test_advanced_alias_usage_and_ignore_policy(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "app/modules/notes/services.py"
    (project.root / path).write_text(
        "import sandloom.sdk.authz as authz\nx = authz.Account.user()\n"
    )
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    assert "sandloom.sdk.authz.Account" in {
        d["id"] for d in json.loads(text)["data"]["sdk"]
    }
    (project.root / ".sandloomignore").write_text(path + "\n")
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    data = json.loads(text)["data"]
    assert path not in {s["path"] for s in data["sections"]}
    assert {"path": path, "reason": "context_ignore"} in data["omissions"]
    assert run(project, capsys, "context", "notes", "--file", "services.py")[0] == 1


def test_unknown_graph_variant_and_owner_escape_fail_closed(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = json.loads((project.root / INDEX).read_text())
    original = encode(doc)
    doc["edges"][0]["kind"] = "injected --> malicious"
    (project.root / INDEX).write_text(encode(doc))
    assert run(project, capsys, "graph", "--format", "mermaid")[0] == 1
    (project.root / INDEX).write_text(original)
    doc = json.loads(original)
    doc["modules"][0]["source"] = "app/shared"
    (project.root / INDEX).write_text(encode(doc))
    rc, text, _ = run(project, capsys, "graph", "--view", "modules")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "unsafe_path"


def test_schema_root_recursion_and_unknown_dto_dependency(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/shared/events/stats.changed.v1.json").write_text(
        '{"type":"object","properties":{"next":{"$ref":"#"}}}'
    )
    assert run(project, capsys, "context", "notes")[0] == 0
    dto = project.root / "app/shared/contracts/stats_dto.py"
    dto.write_text("from unknown_common.types import Value\nclass Out: x: Value\n")
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert (
        rc == 1
        and json.loads(text)["diagnostics"][0]["kind"] == "unsupported_wire_import"
    )


def test_impl_only_edit_keeps_semantic_index_and_check_read_only(
    project: SimpleNamespace,
) -> None:
    before_index = (project.root / INDEX).read_bytes()
    before_receipt = (project.root / RECEIPT).read_bytes()
    (project.root / "app/modules/notes/services.py").write_text("x = 2\n")
    code = generation.finish(
        rules=project.rules,
        app=project.app,
        selected=list(project.planned),
        modules=[],
        check=True,
        before=collect.capture(project.rules),
        plan_all=lambda: project.planned,
        make_file=Generated,
        apply_files=apply,
    )
    assert code == 0
    assert (project.root / INDEX).read_bytes() == before_index
    assert (project.root / RECEIPT).read_bytes() == before_receipt


def test_incomplete_optional_provider_does_not_block_independent_receipt(
    project: SimpleNamespace,
) -> None:
    reader = Reader(project.root)
    next(m for m in reader.doc["modules"] if m["id"] == "module:archive")[
        "wire_paths"
    ] = ["app/shared/contracts/missing_optional_v1.py"]
    (project.root / INDEX).write_text(encode(reader.doc))
    (project.root / RECEIPT).write_text(Reader(project.root).receipt())
    reader = Reader(project.root)
    assert (
        reader.freshness([reader.module("notes")], require=True)["state"] == "verified"
    )
    assert (
        reader.freshness([reader.module("archive")], tolerate_missing=True)["state"]
        == "partial"
    )


def test_output_canonical_alias_still_protects_rules(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    prior = (project.root / "AGENTS.md").read_bytes()
    assert (
        run(project, capsys, "context", "notes", "--output", "./AGENTS.md", "--force")[
            0
        ]
        == 1
    )
    assert (project.root / "AGENTS.md").read_bytes() == prior


def _publish(project: SimpleNamespace, document: dict[str, object]) -> Reader:
    (project.root / INDEX).write_text(encode(document))
    reader = Reader(project.root)
    (project.root / RECEIPT).write_text(reader.receipt())
    return reader


def test_process_collector_reads_real_saga_without_executing_callbacks(
    project: SimpleNamespace,
) -> None:
    from datetime import timedelta

    from pydantic import BaseModel

    from sandloom.core.descriptors import Saga, local_step, step

    class Input(BaseModel):
        order: str

    async def forbidden(*_args: object) -> None:
        raise AssertionError("collector must not call business logic")

    def forbidden_args(_ctx: object) -> dict[str, object]:
        raise AssertionError("collector must not evaluate step args")

    definition = Saga(
        "checkout",
        version=1,
        input=Input,
        deadline=timedelta(minutes=5),
        steps=[
            local_step("reserve", apply=forbidden, compensate=forbidden),
            step(
                "charge",
                call="stats.quota.v1.charge",
                args=forbidden_args,
                kind="pivot",
                check_args=False,
            ),
            step(
                "confirm",
                call="stats.quota.v1.confirm",
                args=forbidden_args,
                check_args=False,
            ),
        ],
    )
    definition.bind("app.modules.notes")
    project.app.state.core.sagas = [(definition, "app.modules.notes")]
    document = collect.collect(project.app, project.rules, project.planned)
    process = document["processes"][0]
    assert process["id"] == "saga:notes.checkout%401"
    assert [s["kind"] for s in process["steps"]] == [
        "compensatable",
        "pivot",
        "retriable",
    ]
    assert process["steps"][0]["compensation_available"]
    assert (
        process["steps"][1]["transaction"] == "provider command unit; separate commit"
    )
    reader = _publish(project, document)
    from sandloom.core.cli.inspection import sequences

    result = sequences.build(reader, selector=process["id"], module=None, require=True)
    sequence = result["data"]["process"]
    assert [s["label"] for s in sequence["steps"]] == ["reserve", "charge", "confirm"]
    assert [s["label"] for s in sequence["alternatives"]] == ["Undo reserve"]
    mermaid = sequences.mermaid(result)
    assert "alt Forward path" in mermaid
    assert "else Compensation before pivot" in mermaid
    state = sequences.build(
        reader, selector=process["id"], module=None, require=True, state=True
    )
    assert "stateDiagram-v2" in sequences.mermaid(state)
    assert "parked" in sequences.mermaid(state)
    document["processes"][0]["steps"][0]["kind"] = "changed"
    assert (
        Reader(project.root, document).freshness([reader.module("notes")])["state"]
        != "verified"
    )

    from copy import copy

    from sandloom.core.cli.inspection.model import InspectionError

    second = copy(definition)
    second.version = 2
    project.app.state.core.sagas.append((second, "app.modules.notes"))
    reader = _publish(
        project, collect.collect(project.app, project.rules, project.planned)
    )
    assert {p["id"] for p in reader.doc["processes"]} == {
        "saga:notes.checkout%401",
        "saga:notes.checkout%402",
    }
    with pytest.raises(InspectionError, match="stable process ID"):
        sequences.select(reader, "notes.checkout")


def test_route_process_uses_resolved_snapshot_not_empty_manifest(
    project: SimpleNamespace,
) -> None:
    from dataclasses import replace

    from sandloom.core.cli.inspection import sequences
    from sandloom.core.loader.manifest import RouteEntry

    original = project.app.state.app_manifest
    notes = replace(
        original.modules[0],
        routes=(RouteEntry("app.modules.notes", "POST", "/notes", "create"),),
    )
    project.app.state.app_manifest = AppManifest((notes, *original.modules[1:]))
    path = project.root / "app/modules/notes/routes.snapshot.json"
    policy = {
        "effect": "write",
        "session": "none",
        "idempotency": "none",
        "public_reason": "webhook",
    }
    project.planned.append(
        Generated(
            path,
            encode(
                {
                    "module": notes.name,
                    "routes": [
                        {
                            "method": "POST",
                            "path": "/notes",
                            "auth": "public",
                            "policy": policy,
                        }
                    ],
                }
            ),
            "SANDLOOM-T001",
        )
    )
    document = collect.collect(project.app, project.rules, project.planned)
    process = document["processes"][0]
    assert process["policy"] == policy
    result = sequences.project(process)
    assert not any(s["label"] == "Commit-time re-check" for s in result["steps"])
    assert not any(s["label"].startswith("Idempotency:") for s in result["steps"])
    assert any("opaque" in s["label"] for s in result["steps"])
    process["policy"] = {**policy, "operation_intent": "checkout"}
    result = sequences.project(process)
    assert not any(s["label"] == "before_commit → COMMIT" for s in result["steps"])
    assert any(s["label"] == "Validate intent release rule" for s in result["steps"])


def test_focused_context_keeps_dto_closure_and_explicit_owned_helpers(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = project.root / "app/modules/notes/unrelated.py"
    helper.write_text("def helper(): return 1\n")
    rc, text, _ = run(
        project,
        capsys,
        "context",
        "notes",
        "--focus",
        "contract:stats.quota.v1",
        "--format",
        "json",
    )
    assert rc == 0
    data = json.loads(text)["data"]
    paths = {s["path"] for s in data["sections"]}
    assert "app/shared/contracts/stats_dto.py" in paths
    assert "app/shared/events/stats.changed.v1.json" not in paths
    assert data["event_schemas"] == {}
    assert not any("modules/stats/" in p for p in paths)
    assert {
        "path": "app/modules/notes/unrelated.py",
        "reason": "outside_focus",
    } in data["omissions"]
    rc, text, _ = run(
        project,
        capsys,
        "context",
        "notes",
        "--focus",
        "contract:stats.quota.v1",
        "--file",
        "unrelated.py",
        "--format",
        "json",
    )
    assert rc == 0
    assert "app/modules/notes/unrelated.py" in {
        s["path"] for s in json.loads(text)["data"]["sections"]
    }
    assert (
        run(
            project, capsys, "context", "archive", "--focus", "contract:stats.quota.v1"
        )[0]
        == 1
    )


def test_declared_impact_preserves_direct_interface_witnesses(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(
        project,
        capsys,
        "graph",
        "--view",
        "impact",
        "--item",
        "contract:stats.quota.v1",
    )
    assert rc == 0
    affected = json.loads(text)["data"]["affected"]
    assert {m["module_id"] for m in affected} == {"module:notes", "module:stats"}
    assert all(m["seed_witnesses"] for m in affected)
    assert all(m["test_argv"][0:2] == ["sandloom", "test"] for m in affected)


def test_semantic_diff_ignores_bodies_and_checks_event_compatibility(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    original = (project.root / INDEX).read_text()
    (project.root / "before.json").write_text(original)
    (project.root / "app/modules/notes/services.py").write_text("# body-only edit\n")
    rc, text, _ = run(
        project, capsys, "graph", "--view", "diff", "--before", "before.json"
    )
    assert rc == 0
    assert json.loads(text)["data"]["counts"] == {
        "added": 0,
        "removed": 0,
        "changed": 0,
    }
    document = json.loads(original)
    schema = document["event_schemas"]["stats.changed.v1"]
    schema["properties"]["required_now"] = {"type": "string"}
    schema["required"] = ["required_now"]
    (project.root / INDEX).write_text(encode(document))
    rc, text, _ = run(
        project, capsys, "graph", "--view", "diff", "--before", "before.json"
    )
    assert rc == 0
    changes = json.loads(text)["data"]["changes"]
    assert len(changes) == 1 and changes[0]["id"] == "event:stats.changed.v1"
    assert changes[0]["compatibility"]["state"] == "breaking"
    assert changes[0]["compatibility"]["reasons"]


def test_contract_diff_uses_existing_shared_compatibility_rules(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    document = json.loads((project.root / INDEX).read_text())
    document["contract_descriptions"] = {
        "stats.quota.v1": {"methods": {"read": {"params": [], "return": {"k": "none"}}}}
    }
    (project.root / "before.json").write_text(encode(document))
    document["contract_descriptions"]["stats.quota.v1"]["methods"] = {}
    (project.root / INDEX).write_text(encode(document))
    rc, text, _ = run(
        project, capsys, "graph", "--view", "diff", "--before", "before.json"
    )
    assert rc == 0
    change = json.loads(text)["data"]["changes"][0]
    assert change["compatibility"]["state"] == "breaking"
    assert any("read" in reason for reason in change["compatibility"]["reasons"])


@pytest.mark.parametrize(
    "args",
    [
        ["--view", "state"],
        ["--view", "impact"],
        ["--view", "diff"],
        ["--process", "fake"],
        ["--view", "impact", "--item", "module:notes", "--format", "mermaid"],
        ["--view", "sequence", "--hops", "1"],
    ],
)
def test_new_views_reject_incompatible_or_missing_arguments(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    rc, text, _ = (
        run(project, capsys, "graph", *args, "--format", "json")
        if "--format" not in args
        else run(project, capsys, "graph", *args)
    )
    assert rc == 2
    if text:
        assert json.loads(text)["diagnostics"][0]["kind"] == "usage"


def test_ignore_policy_cannot_be_overwritten_through_alias(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project.root / ".sandloomignore"
    path.write_text("# keep\n")
    assert (
        run(project, capsys, "graph", "--output", "./.sandloomignore", "--force")[0]
        == 1
    )
    assert path.read_text() == "# keep\n"


def test_generation_fingerprint_is_not_a_source_export_filter(
    project: SimpleNamespace,
) -> None:
    from sandloom.core.cli.inspection import files

    path = project.root / "app/modules/notes/data.py"
    path.write_bytes(b"\xff\x00" + b"x" * (files.MAX_FILE + 1))
    assert collect.capture(project.rules)
    project.planned[0].content += "# written despite unavailable inspection metadata\n"
    assert (
        generation.finish(
            rules=project.rules,
            app=project.app,
            selected=list(project.planned),
            modules=[],
            check=False,
            before=collect.capture(project.rules),
            plan_all=lambda: project.planned,
            make_file=Generated,
            apply_files=apply,
        )
        == 0
    )
    assert "written despite" in project.planned[0].path.read_text()
    assert not (project.root / RECEIPT).exists()


def test_inspection_failure_keeps_generate_check_read_only(
    project: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli.inspection.model import InspectionError

    before = {p: (project.root / p).read_bytes() for p in (INDEX, RECEIPT)}

    def unavailable(*_args: object) -> None:
        raise InspectionError("unsafe_path", "unavailable")

    monkeypatch.setattr(collect, "collect", unavailable)
    assert (
        generation.finish(
            rules=project.rules,
            app=project.app,
            selected=list(project.planned),
            modules=[],
            check=True,
            before=collect.capture(project.rules),
            plan_all=lambda: project.planned,
            make_file=Generated,
            apply_files=apply,
        )
        == 0
    )
    assert before == {p: (project.root / p).read_bytes() for p in before}


def test_source_race_aborts_before_any_generated_file_is_written(
    project: SimpleNamespace,
) -> None:
    before = collect.capture(project.rules)
    project.planned[0].content += "# must not write\n"
    (project.root / "app/modules/notes/services.py").write_text(
        "# changed after capture\n"
    )
    assert (
        generation.finish(
            rules=project.rules,
            app=project.app,
            selected=list(project.planned),
            modules=[],
            check=False,
            before=before,
            plan_all=lambda: project.planned,
            make_file=Generated,
            apply_files=apply,
        )
        == 1
    )
    assert "must not write" not in project.planned[0].path.read_text()


def test_absent_optional_provider_is_reported_without_imaginary_wire(
    project: SimpleNamespace,
) -> None:
    from dataclasses import replace

    original = project.app.state.app_manifest
    notes = replace(
        original.modules[0],
        contracts_consumed=(
            *original.modules[0].contracts_consumed,
            ContractUse("app.modules.notes", "absent.optional.v1", required=False),
        ),
    )
    project.app.state.app_manifest = AppManifest((notes, *original.modules[1:]))
    reader = _publish(
        project, collect.collect(project.app, project.rules, project.planned)
    )
    assert not any(
        "absent_optional" in p for p in reader.wire_paths(reader.module("notes"))
    )
    assert reader.doc["unresolved"][0]["related_id"] == "contract:absent.optional.v1"
    assert not reader.doc["unresolved"][0]["required"]


def test_viewer_assets_and_placeholder_data_are_embedded_safely(
    project: SimpleNamespace,
) -> None:
    reader = Reader(project.root)
    reader.doc["nodes"][0]["name"] = "</script>__SANDLOOM_VIEWER_JS__"
    envelope = graph.build(
        reader,
        selector="notes",
        hops=0,
        discovery=False,
        detail="compact",
        require=False,
    )
    rendered = graph.viewer(envelope, reader)
    assert "\\u003c/script>__SANDLOOM_VIEWER_JS__" in rendered
    assert "Condense strongly connected components" in rendered
    assert "Save SVG" in rendered
    assert "__SANDLOOM_VIEWER_CSS__" not in rendered
    assert "<script src=" not in rendered
    assert "<link rel=" not in rendered


def test_compact_topology_defers_route_policy_to_explicit_detail(
    project: SimpleNamespace,
) -> None:
    reader = Reader(project.root)
    reader.doc["processes"] = [
        {
            "id": "route:notes",
            "owner": "module:notes",
            "name": "GET /notes",
            "policy": {"session": "live"},
        }
    ]
    compact = graph.build(
        reader,
        selector="notes",
        hops=0,
        discovery=False,
        detail="compact",
        require=False,
    )["data"]
    full = graph.build(
        reader, selector="notes", hops=0, discovery=False, detail="full", require=False
    )["data"]
    assert compact["process_counts"] == {"route": 1, "saga": 0}
    assert "processes" not in compact
    assert "routes" not in compact["modules"][0]
    assert compact["modules"][0]["route_count"] == 0
    assert full["processes"][0]["policy"] == {"session": "live"}
