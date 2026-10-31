"""Exercise cold CLI exports, ownership, closure and generation through real files."""

from __future__ import annotations

import json
import shutil
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
        imported.__doc__ = (
            f"{name.title()} purpose.\nInternal details stay out of the summary."
        )
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
        service_registry=SimpleNamespace(list=lambda: []),
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
    # Most tests inspect the structured protocol explicitly.
    fmt = ["--format", "json"] if command == "graph" and "--format" not in args else []
    result = main(command, ["--root", str(project.root), *fmt, *args])
    output = capsys.readouterr()
    return result, output.out, output.err


def test_context_map_has_rule_and_interface_pointers_without_bodies(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, err = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0, err + text
    result = json.loads(text)
    data = result["data"]
    assert data["representation"] == "module_map"
    assert result["freshness"]["state"] == "verified"
    assert {"AGENTS.md", "app/modules/notes/AGENTS.md"} <= set(data["rules"])
    assert "app/shared/contracts/stats_dto.py" in data["interface_files"]
    assert "contract:stats.quota.v1" in {c["id"] for c in data["contracts_used"]}
    assert data["events_consumed"][0]["channel"] == "stats.changed.v1"
    assert not any("modules/stats/" in f["path"] for f in data["files"])
    assert "sections" not in data and "sdk" not in data
    assert (
        "local implementation" not in text and "APPLICATION MUST NOT IMPORT" not in text
    )
    assert "bytes" not in data and "budget" not in data


def test_markdown_default_deterministic_and_collision_safe(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "app/modules/notes/services.py").write_text("# ```\n# </script>\n")
    first = run(project, capsys, "context", "module:notes")
    second = run(project, capsys, "context", "app.modules.notes")
    assert first == second
    assert first[0] == 0
    assert "Freshness: source changed since last generate" in first[1]
    assert "````python" not in first[1]
    assert "## Files" in first[1]


def test_graph_hops_preserve_contract_and_event_witnesses(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(project, capsys, "graph", "notes", "--hops", "1")
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


def test_project_json_contains_modules_and_relations(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(
        project,
        capsys,
        "graph",
    )
    data = json.loads(text)["data"]
    assert rc == 0
    assert len(data["modules"]) == 3
    assert data["edges"] and data["module_relations"]


@pytest.mark.parametrize(
    "extra,kind,code",
    [
        (["unknown"], "unknown_module", 1),
        (["notes", "--file", "../stats/services.py"], "usage", 2),
        (["notes", "--max-bytes", "20"], "usage", 2),
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
    rc, text, _ = run(
        project,
        capsys,
        "graph",
    )
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "layout_changed"


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
    assert rc == 2 and "never-print-this" not in text


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
    rc, text, _ = run(
        project,
        capsys,
        "graph",
    )
    assert rc == 0
    result = json.loads(text)
    assert result["freshness"]["state"] == "partial"
    assert result["freshness"]["issues"]["module:notes"] == "incomplete_wire"
    assert run(project, capsys, "graph", "--require-verified")[0] == 3
    assert run(project, capsys, "context", "notes")[0] == 1


def test_map_omits_sdk_dump_and_respects_ignore_policy(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "app/modules/notes/services.py"
    (project.root / path).write_text(
        "import sandloom.sdk.authz as authz\nx = authz.Account.user()\n"
    )
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and "sdk" not in json.loads(text)["data"]
    assert "authz.Account.user" not in text
    (project.root / ".sandloomignore").write_text(path + "\n")
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    data = json.loads(text)["data"]
    assert path not in {f["path"] for f in data["files"]}
    assert {"reason": "context_ignore", "count": 1} in data["omissions"]
    assert run(project, capsys, "context", "notes", "--file", "services.py")[0] == 2


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
    rc, text, _ = run(
        project,
        capsys,
        "graph",
    )
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
    assert "[PIVOT]" in mermaid
    assert "provider command unit" in mermaid and "separate commit" in mermaid
    assert "local savepoint" in mermaid
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


def test_declared_impact_preserves_direct_interface_witnesses(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, text, _ = run(
        project,
        capsys,
        "graph",
        "impact",
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
    rc, text, _ = run(project, capsys, "graph", "diff", "before.json")
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
    rc, text, _ = run(project, capsys, "graph", "diff", "before.json")
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
    rc, text, _ = run(project, capsys, "graph", "diff", "before.json")
    assert rc == 0
    change = json.loads(text)["data"]["changes"][0]
    assert change["compatibility"]["state"] == "breaking"
    assert any("read" in reason for reason in change["compatibility"]["reasons"])


@pytest.mark.parametrize(
    "args",
    [
        [
            "state",
        ],
        [
            "impact",
        ],
        [
            "diff",
        ],
        ["--process", "fake"],
        ["impact", "module:notes", "--format", "mermaid"],
        ["sequence", "--hops", "1"],
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
    assert (project.root / RECEIPT).exists()


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
    assert "Contracts set the direction first" in rendered
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


@pytest.mark.parametrize(
    "mutation",
    [
        "future",
        "boolean_version",
        "schema",
        "modules",
        "basis",
        "source",
        "wire",
        "digest",
        "distributions",
        "issues",
    ],
)
def test_invalid_receipt_returns_typed_error_without_verification_or_writes(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    path = project.root / RECEIPT
    receipt = json.loads(path.read_text())
    if mutation == "future":
        receipt["schema_version"] = 999
    elif mutation == "boolean_version":
        receipt["schema_version"] = True
    elif mutation == "schema":
        receipt["schema"] = "unknown"
    elif mutation == "modules":
        receipt["modules"] = []
    elif mutation == "basis":
        receipt["modules"]["module:notes"] = []
    elif mutation == "source":
        receipt["modules"]["module:notes"]["source"] = []
    elif mutation == "wire":
        receipt["modules"]["module:notes"]["wire"] = {"file": 42}
    elif mutation == "digest":
        receipt["modules"]["module:notes"]["semantic_digest"] = "invalid"
    elif mutation == "distributions":
        receipt["modules"]["module:notes"]["distributions"] = []
    else:
        receipt["issues"] = []
    path.write_text(encode(receipt))
    destination = project.root / "existing.json"
    destination.write_text("preserve previous file\n")
    rc, text, err = run(
        project,
        capsys,
        "graph",
        "--require-verified",
    )
    assert rc == 1 and not err
    envelope = json.loads(text)
    assert not envelope["ok"] and envelope["freshness"] is None
    assert envelope["diagnostics"][0]["kind"] in {
        "unsupported_receipt",
        "invalid_receipt",
    }
    assert destination.read_text() == "preserve previous file\n"
    assert json.loads(path.read_text()) == receipt


def _embedded_viewer(text: str) -> dict[str, object]:
    return json.loads(
        text.split('<script id="graph-data" type="application/json">', 1)[1].split(
            "</script>", 1
        )[0]
    )


def _process_reader(project: SimpleNamespace) -> Reader:
    document = json.loads((project.root / INDEX).read_text())
    document["processes"] = [
        {
            "id": f"route:{module}-{method}",
            "name": f"{method} /{module}",
            "owner": "module:" + module,
            "handler": "handle",
            "policy": {"effect": "read", "session": "live", "idempotency": "none"},
            "evidence_kind": "resolved_route_policy",
        }
        for module, method in (
            ("notes", "GET"),
            ("notes", "POST"),
            ("stats", "GET"),
            ("archive", "GET"),
        )
    ]
    return _publish(project, document)


def test_html_topology_preserves_module_scope_and_process_freshness(
    project: SimpleNamespace,
) -> None:
    reader = _process_reader(project)
    envelope = graph.build(
        reader,
        selector="notes",
        hops=0,
        discovery=False,
        detail="compact",
        require=True,
    )
    payload = _embedded_viewer(graph.viewer(envelope, reader))
    assert payload["selection"]["module_ids"] == ["module:notes"]
    assert payload["workspace"]["scope"]["module_ids"] == ["module:notes"]
    assert {m["id"] for m in payload["workspace"]["modules"]} == {"module:notes"}
    assert {p["owner"] for p in payload["sequences"]} == {"module:notes"}
    assert set(payload["freshness"]["modules"]) == {"module:notes"}


@pytest.mark.parametrize("specific", [False, True])
def test_html_sequence_preserves_module_or_process_scope(
    project: SimpleNamespace,
    specific: bool,
) -> None:
    from sandloom.core.cli.inspection import sequences

    reader = _process_reader(project)
    envelope = sequences.build(
        reader,
        selector="route:notes-GET" if specific else None,
        module=None if specific else "notes",
        require=True,
    )
    payload = _embedded_viewer(graph.viewer(envelope, reader))
    expected = (
        {"route:notes-GET"} if specific else {"route:notes-GET", "route:notes-POST"}
    )
    assert {p["id"] for p in payload["sequences"]} == expected
    assert {p["id"] for p in payload["data"]["processes"]} == expected
    assert {m["id"] for m in payload["workspace"]["modules"]} == {"module:notes"}
    assert set(payload["freshness"]["modules"]) == {"module:notes"}


def test_impact_html_workspace_excludes_unaffected_modules(
    project: SimpleNamespace,
) -> None:
    from sandloom.core.cli.inspection import queries

    reader = _process_reader(project)
    payload = _embedded_viewer(
        graph.viewer(queries.impact(reader, "contract:stats.quota.v1", True), reader)
    )
    expected = {"module:notes", "module:stats"}
    assert set(payload["selection"]["module_ids"]) == expected
    assert {m["id"] for m in payload["workspace"]["modules"]} == expected
    assert {p["owner"] for p in payload["sequences"]} == expected
    assert set(payload["freshness"]["modules"]) == expected


def test_scc_handles_deep_graphs_and_keeps_families_separate() -> None:
    links = [
        {"kind": "required_contract", "source": str(i), "target": str(i + 1)}
        for i in range(2500)
    ]
    assert graph.cycles(links) == []
    links.append({"kind": "required_contract", "source": "2500", "target": "0"})
    found = graph.cycles(links)
    assert len(found) == 1 and len(found[0]["module_ids"]) == 2501
    typed = [
        {"kind": "event_impact", "source": "a", "target": "b"},
        {"kind": "event_impact", "source": "b", "target": "a"},
        {"kind": "required_contract", "source": "a", "target": "b"},
        {"kind": "optional_contract", "source": "c", "target": "c"},
    ]
    result = graph.cycles(typed)
    assert result == graph.cycles(list(reversed(typed)))
    assert [(c["kind"], c["module_ids"]) for c in result] == [
        ("event_impact", ["a", "b"]),
        ("optional_contract", ["c"]),
    ]


def test_mermaid_sequence_escapes_control_text_without_losing_unit_boundaries() -> None:
    from sandloom.core.cli.inspection import sequences
    from sandloom.core.cli.inspection.model import response

    process = {
        "evidence": "declared",
        "steps": [
            {
                "source": "runtime",
                "target": "module:notes",
                "label": "end",
                "kind": "pivot",
                "transaction": 'unit; separate commit & <callback> "quoted"',
                "condition": "first\rnext\u2028line",
            }
        ],
        "alternatives": [],
    }
    text = sequences.mermaid(response("graph", "sequence", {"process": process}))
    assert "#101;nd [PIVOT]" in text
    assert "unit#59; separate commit #38; #60;callback#62; #34;quoted#34;" in text
    assert "first next line" in text
    assert "&amp" not in text and "&#" not in text


@pytest.mark.parametrize(
    ("tty", "fmt", "expected"),
    [
        (True, None, "text"),
        (False, None, "text"),
        (True, "json", "json"),
        (False, "text", "text"),
    ],
)
def test_graph_terminal_and_pipe_formats(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tty: bool,
    fmt: str | None,
    expected: str,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    args = ["notes", *(["--format", fmt] if fmt else [])]
    rc = main("graph", ["--root", str(project.root), *args])
    text, err = capsys.readouterr()
    assert rc == 0 and not err
    if expected == "json":
        assert json.loads(text)["selection"]["module_ids"] == [
            "module:notes",
            "module:stats",
        ]
    else:
        assert "Freshness: verified" in text and "├─" in text
        assert "stats.quota.v1" in text and "stats.changed.v1" in text
        assert "archive" not in text


def test_module_and_interface_ids_match_logical_selectors(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(project, capsys, "graph", "notes", "--hops", "0") == run(
        project, capsys, "graph", "module:notes", "--hops", "0"
    )
    short = run(
        project, capsys, "graph", "impact", "--format", "json", "stats.quota.v1"
    )
    assert short == run(
        project,
        capsys,
        "graph",
        "impact",
        "contract:stats.quota.v1",
    )


@pytest.mark.parametrize(
    "args",
    [
        ["impact"],
        ["diff"],
        ["notes", "stats"],
        ["notes", "stats"],
        ["impact", "stats.quota.v1", "--view", "graph"],
        ["diff", "main", "saved.json"],
        ["impact", "stats.quota.v1", "notes"],
    ],
)
def test_short_command_usage_is_structured_without_writes(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    before = {p: p.read_bytes() for p in project.root.rglob("*") if p.is_file()}
    rc, text, err = run(project, capsys, "graph", *args)
    assert rc == 2 and not err
    assert json.loads(text)["diagnostics"][0]["kind"] == "usage"
    assert before == {p: p.read_bytes() for p in project.root.rglob("*") if p.is_file()}


def test_terminal_errors_use_stderr(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc, text, err = run(project, capsys, "graph", "impact", "--format", "text")
    assert rc == 2 and text == "" and "graph impact ITEM" in err


def _git_commit(root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=" + str(root / ".disabled-hooks"),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Inspection Fixture",
                "-c",
                "user.email=inspection@example.invalid",
                "-C",
                str(root),
                *args,
            ],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()

    git("init", "--initial-branch", "main")
    git("add", ".")
    git("commit", "-m", "Record architecture")
    return git("rev-parse", "HEAD")


@pytest.mark.parametrize("nested", [False, True])
def test_git_diff_reads_saved_model_without_checkout_or_app_import(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    tmp_path_factory: pytest.TempPathFactory,
    nested: bool,
) -> None:
    repo = tmp_path_factory.mktemp("inspection-git")
    root = repo / "apps/demo" if nested else repo
    shutil.copytree(project.root, root, dirs_exist_ok=True)
    commit = _git_commit(repo)
    document = json.loads((root / INDEX).read_text())
    document["modules"][0]["version"] = "2"
    (root / INDEX).write_text(encode(document))
    before = (repo / ".git/HEAD").read_bytes()
    rc, text, err = run(SimpleNamespace(root=root), capsys, "graph", "diff", "main")
    assert rc == 0 and not err
    data = json.loads(text)["data"]
    assert data["basis"]["before_revision"] == commit
    assert data["basis"]["before"] == "main:" + ("apps/demo/" if nested else "") + INDEX
    assert data["counts"] == {"added": 0, "removed": 0, "changed": 1}
    assert data["changes"][0]["compatibility"]["state"] == "not_assessed"
    assert (repo / ".git/HEAD").read_bytes() == before
    assert json.loads((root / INDEX).read_text())["modules"][0]["version"] == "2"


def test_git_diff_missing_baseline_and_option_like_revision_are_errors(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    saved = (project.root / INDEX).read_bytes()
    (project.root / INDEX).unlink()
    _git_commit(project.root)
    (project.root / INDEX).write_bytes(saved)
    for revision in ("main", "does-not-exist", "--output=unexpected"):
        rc, text, err = run(project, capsys, "graph", "diff", "--", revision)
        assert rc == 1 and not err
        assert json.loads(text)["diagnostics"][0]["kind"] == "git_baseline_unavailable"
    assert not (project.root / "unexpected").exists()


def test_git_diff_handles_missing_git_and_timeout(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli.inspection import history

    monkeypatch.setattr(history.shutil, "which", lambda _: None)
    rc, text, _ = run(project, capsys, "graph", "diff", "main")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "git_unavailable"
    monkeypatch.setattr(history.shutil, "which", lambda _: "/git")

    def timed_out(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(history._tool_process, "run_tool", timed_out)
    rc, text, _ = run(project, capsys, "graph", "diff", "main")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "git_unavailable"


def test_terminal_escapes_controls(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = json.loads((project.root / INDEX).read_text())
    document["nodes"][0]["name"] = "notes\x1b[31m\nspoof"
    document["modules"][0]["name"] = "notes\x1b[31m\nspoof"
    (project.root / INDEX).write_text(encode(document))
    rc, text, _ = run(project, capsys, "graph", "--format", "text")
    assert rc == 0 and "\x1b" not in text and "\\x1b[31m\\nspoof" in text


@pytest.mark.parametrize("generated", [True, False])
def test_explore_generates_then_opens_one_local_artifact(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    generated: bool,
) -> None:
    from sandloom.core.cli import explore

    calls = []

    def generate(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(("generate", argv))
        return subprocess.CompletedProcess(argv, 0, stdout="generated\n")

    def open_browser(uri: str, **kwargs: object) -> bool:
        calls.append(("open", uri))
        assert (project.root / explore.OUTPUT).is_file()
        return True

    monkeypatch.setattr(explore._tool_process, "run_tool", generate)
    monkeypatch.setattr(explore.webbrowser, "open", open_browser)
    args = ["notes", "--root", str(project.root), "--hops", "0"]
    assert explore.main(args + ([] if generated else ["--no-generate"])) == 0
    output = capsys.readouterr()
    assert "file:" in output.out and "freshness: verified" in output.out
    assert [c[0] for c in calls] == (["generate", "open"] if generated else ["open"])
    if generated:
        assert calls[0][1][-3:] == ["generate", "--root", str(project.root)]
    assert calls[-1][1] == (project.root / explore.OUTPUT).as_uri()
    payload = _embedded_viewer((project.root / explore.OUTPUT).read_text())
    assert payload["selection"]["module_ids"] == ["module:notes"]


def test_explore_failure_never_opens_stale_html(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import explore

    target = project.root / explore.OUTPUT
    target.write_text("old page")
    monkeypatch.setattr(
        explore._tool_process,
        "run_tool",
        lambda *a, **kw: subprocess.CompletedProcess(
            [], 1, stdout="incompatible declaration\n"
        ),
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("browser must not open")

    monkeypatch.setattr(explore.webbrowser, "open", forbidden)
    assert explore.main(["--root", str(project.root)]) == 1
    assert target.read_text() == "old page"
    assert "Generation failed" in capsys.readouterr().err


def test_explore_no_open_and_unavailable_browser_keep_usable_artifact(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import explore

    monkeypatch.setattr(explore.webbrowser, "open", lambda *a, **kw: False)
    assert (
        explore.main(["--root", str(project.root), "--no-generate", "--no-open"]) == 0
    )
    assert "No browser" not in capsys.readouterr().err
    assert explore.main(["--root", str(project.root), "--no-generate"]) == 0
    assert "No browser available" in capsys.readouterr().err


def test_explore_invalid_input_has_no_traceback(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sandloom.core.cli import explore

    (project.root / INDEX).write_text(
        '{"schema": "sandloom.inspection", "schema_version": 1}'
    )
    assert (
        explore.main(["--root", str(project.root), "--no-generate", "--no-open"]) == 1
    )
    output = capsys.readouterr()
    assert not output.out and "cannot safely read" in output.err


@pytest.mark.parametrize("mutation", ["crlf", "secret", "oversize", "future"])
def test_git_baseline_preserves_input_validation(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    from sandloom.core.cli.inspection.files import MAX_FILE

    path = project.root / INDEX
    current = path.read_bytes()
    document = json.loads(current)
    expected = None
    if mutation == "crlf":
        path.write_bytes(
            json.dumps(document, indent=2, ensure_ascii=False)
            .replace("\n", "\r\n")
            .encode()
        )
    elif mutation == "oversize":
        path.write_bytes(b" " * (MAX_FILE + 1))
        expected = "file_too_large"
    elif mutation == "secret":
        document["modules"][0]["name"] = "ghp_" + "A" * 40
        path.write_text(encode(document))
        expected = "sensitive_content"
    else:
        document["schema_version"] = 999
        path.write_text(encode(document))
        expected = "unsupported_schema"
    _git_commit(project.root)
    path.write_bytes(current)
    rc, text, err = run(project, capsys, "graph", "diff", "main")
    assert not err
    result = json.loads(text)
    if expected:
        assert rc == 1 and result["diagnostics"][0]["kind"] == expected
    else:
        assert rc == 0 and result["data"]["counts"] == {
            "added": 0,
            "changed": 0,
            "removed": 0,
        }


@pytest.mark.parametrize(
    "formats",
    [
        ["--format=json", "--format", "text"],
        ["--format", "text", "--format=json"],
    ],
)
def test_explicit_error_format_uses_the_last_option(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    formats: list[str],
) -> None:
    rc, out, err = run(project, capsys, "graph", "impact", *formats)
    assert rc == 2
    if formats[-1].endswith("json"):
        assert not err and json.loads(out)["kind"] == "error"
    else:
        assert not out and "usage" in err


def test_graph_never_starts_a_browser_or_authoring_process(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli import explore

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("graph must remain a cold read")

    monkeypatch.setattr(explore.webbrowser, "open", forbidden)
    monkeypatch.setattr(explore._tool_process, "run_tool", forbidden)
    for args in ([], ["notes"], ["impact", "stats.quota.v1"], ["--format", "text"]):
        rc, _, err = run(project, capsys, "graph", *args)
        assert rc == 0 and not err


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_map_never_contains_implementation_or_rule_bodies(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
) -> None:
    (project.root / "app/modules/notes/services.py").write_text(
        '"""Business service entry points."""\nclass Worker:\n    def execute(self):\n        return "BODY_CANARY_NEVER_EMBED"\n'
    )
    (project.root / "AGENTS.md").write_text("RULE_BODY_CANARY_NEVER_EMBED\n")
    wire = project.root / "app/shared/contracts/stats_dto.py"
    wire.write_text(
        wire.read_text()
        + '\ndef implementation_helper(): return "WIRE_BODY_CANARY_NEVER_EMBED"\n'
    )
    rc, out, err = run(project, capsys, "context", "notes", "--format", fmt)
    assert rc == 0 and not err
    assert "BODY_CANARY_NEVER_EMBED" not in out
    assert "RULE_BODY_CANARY_NEVER_EMBED" not in out
    assert "Business service entry points." in out and "Worker" in out
    assert "AGENTS.md" in out
    assert "services.py" in out
    assert "def execute" not in out and "## Public SDK" not in out


def test_contract_map_has_signatures_and_callers(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = json.loads((project.root / INDEX).read_text())
    dto = {
        "k": "model",
        "ref": "app.modules.stats.schemas:QuotaOut",
        "fields": {
            "remaining": {"required": True, "type": {"k": "scalar", "t": "int"}},
            "next": {
                "required": False,
                "type": {
                    "k": "union",
                    "of": [
                        {"k": "ref", "ref": "app.modules.stats.schemas:QuotaOut"},
                        {"k": "none"},
                    ],
                },
            },
        },
    }
    doc["contract_descriptions"] = {
        "stats.quota.v1": {
            "methods": {
                "read": {
                    "kind": "read",
                    "params": [
                        {
                            "name": "limit",
                            "kind": "keyword_only",
                            "required": False,
                            "type": {"k": "scalar", "t": "int"},
                        }
                    ],
                    "returns": dto,
                }
            }
        }
    }
    (project.root / INDEX).write_text(encode(doc))
    rc, out, _ = run(project, capsys, "context", "stats", "--format", "json")
    assert rc == 0
    contract = json.loads(out)["data"]["contracts_provided"][0]
    assert contract["methods"][0]["signature"] == "read(*, limit: int = …) -> QuotaOut"
    assert contract["peers"] == ["module:notes"] and "dtos" not in contract


def test_map_inventory_groups_files_and_prefers_public_symbols_without_execution(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = project.root / "app/modules/notes"
    (root / "api").mkdir()
    (root / "api/work.py").write_text(
        '"""Work endpoints."""\ndef _private(): pass\nclass Worker: pass\n'
        "def create(): pass\ndef update(): pass\ndef delete(): pass\ndef inspect(): pass\n"
    )
    for name in (
        "schemas/input.py",
        "models/note.py",
        "tests/api/test_work.py",
        "migrations/versions/0001.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("raise RuntimeError('MUST_NOT_EXECUTE')\n")
    (root / "migrations/versions/0001.py").write_text(
        "revision = 'r1'\ndown_revision = None\n"
    )
    rc, out, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and "MUST_NOT_EXECUTE" not in out
    data = json.loads(out)["data"]
    record = next(f for f in data["files"] if f["path"].endswith("api/work.py"))
    assert [s["name"] for s in record["symbols"]] == [
        "Worker",
        "create",
        "update",
        "delete",
    ]
    assert record["more_symbols"] == 1 and record["symbols"][0]["line"] == 3
    groups = {g["kind"]: g for g in data["file_groups"]}
    assert groups["tests"]["directories"] == {"api": 1}
    assert groups["schemas"]["files"] == ["input.py"]
    assert groups["migrations"]["heads"] == ["r1"]


def test_map_keeps_navigation_when_implementation_is_temporarily_invalid(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (project.root / "app/modules/notes/services.py").write_text("def unfinished(:\n")
    rc, out, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    data = json.loads(out)
    record = next(f for f in data["data"]["files"] if f["path"].endswith("services.py"))
    assert record["parse_error"] == "Syntax metadata unavailable at line 1"
    assert data["freshness"]["state"] == "source_changed"
    assert "def unfinished" not in out


def test_route_map_groups_namespaces_without_losing_methods_or_parameters() -> None:
    from sandloom.core.cli.inspection import interfaces

    paths = [
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/users"),
        ("GET", "/api/v1/users/{id}"),
        ("GET", "/api/v1/roles"),
        ("POST", "/api/v1/roles"),
        ("GET", "/auth/me"),
        ("PATCH", "/auth/me"),
        ("GET", "/auth/2fa/status"),
        ("POST", "/auth/2fa/enable"),
    ]
    groups = interfaces.routes(
        [{"method": method, "path": path, "name": "handler"} for method, path in paths]
    )
    restored = [
        (r["method"], (g["prefix"].rstrip("/") + r["path"]).rstrip("/"))
        for g in groups
        for r in g["routes"]
    ]
    assert sorted(restored) == sorted(paths)
    assert {g["prefix"] for g in groups} == {
        "/api/v1/users",
        "/api/v1/roles",
        "/auth",
        "/auth/2fa",
    }


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (int, "int"),
        (list[str], "list[str]"),
        (set[int], "set[int]"),
        (frozenset[str], "frozenset[str]"),
        (dict[str, int], "dict[str, int]"),
        (tuple[int, str], "tuple[int, str]"),
        (tuple[int, ...], "tuple[int, ...]"),
        (tuple[()], "tuple[()]"),
        (str | None, "str | None"),
    ],
)
def test_map_types_match_real_contract_wire_descriptors(
    annotation: object, expected: str
) -> None:
    from sandloom.core.cli.inspection.interfaces import type_name
    from sandloom.core.contract_wire import type_descriptor

    assert type_name(type_descriptor(annotation)) == expected


def test_map_literal_types_preserve_the_allowed_values() -> None:
    from typing import Literal

    from sandloom.core.cli.inspection.interfaces import type_name
    from sandloom.core.contract_wire import type_descriptor

    assert (
        type_name(type_descriptor(Literal["open", "closed"]))
        == "Literal['open', 'closed']"
    )


@pytest.mark.parametrize("installed_in_venv", [False, True])
def test_installed_provider_descriptions_come_from_registered_contracts(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    installed_in_venv: bool,
) -> None:
    from sandloom.core.module_api import contract_method

    class Provider:
        @classmethod
        @contract_method
        async def read(cls, limit: int = 10) -> int:
            raise AssertionError("collection must never call the provider")

    location = (
        project.root
        / ".venv/lib/python3.12/site-packages/app/modules/stats/__init__.py"
        if installed_in_venv
        else Path("/foreign/stats/__init__.py")
    )
    if installed_in_venv:
        location.parent.mkdir(parents=True)
        location.write_text(
            'raise AssertionError("installed source must not execute")\n'
        )
    monkeypatch.setattr(sys.modules["app.modules.stats"], "__file__", str(location))
    core = project.app.state.core
    core.service_registry = SimpleNamespace(
        list=lambda: ["stats.quota.v1"], get=lambda _name: Provider
    )
    core.contract_owner = lambda _name: "app.modules.stats"
    document = collect.collect(project.app, project.rules, project.planned)
    assert (
        next(m for m in document["modules"] if m["name"] == "stats")["source"] is None
    )
    assert (
        next(m for m in document["modules"] if m["name"] == "stats")["summary"]
        == "Stats purpose."
    )
    (project.root / INDEX).write_text(encode(document))
    (project.root / RECEIPT).write_text(Reader(project.root).receipt())
    rc = main("graph", ["--root", str(project.root)])
    text, err = capsys.readouterr()
    assert rc == 0 and not err and "stats: Stats purpose." in text
    rc, text, err = run(
        project, capsys, "context", "notes", "--format", "json", "--require-verified"
    )
    assert rc == 0 and not err
    contract = json.loads(text)["data"]["contracts_used"][0]
    assert contract["description_available"]
    assert contract["methods"][0]["signature"] == "read(limit: int = …) -> int"


@pytest.mark.parametrize(
    "path,content,warning",
    [
        (
            "tests/test_keys.py",
            b"KEY = '" + b"-" * 5 + b"BEGIN PRIVATE" + b" KEY" + b"-" * 5 + b"'\n",
            False,
        ),
        ("tests/test_large.py", b"#" * (2 * 1024 * 1024 + 1), False),
        ("legacy.md", b"\xff\xfe", True),
        ("large.py", b"#" * (2 * 1024 * 1024 + 1), True),
        ("secrets/helpers.py", b"def credential_name(): pass\n", False),
        ("credentials/helpers.py", b"def credential_name(): pass\n", False),
    ],
)
def test_metadata_failures_are_local_and_test_bodies_are_not_read(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    content: bytes,
    warning: bool,
) -> None:
    from sandloom.core.cli.inspection import files

    target = project.root / "app/modules/notes" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    original = files.source

    def metadata(root: Path, locator: str) -> str:
        assert "/tests/" not in locator, "test bodies need no metadata read"
        return original(root, locator)

    monkeypatch.setattr(files, "source", metadata)
    rc, text, err = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and not err and "BEGIN PRIVATE KEY" not in text
    result = json.loads(text)
    assert result["freshness"]["state"] == "source_changed"
    if warning:
        record = next(f for f in result["data"]["files"] if f["path"].endswith(path))
        assert record["parse_error"] and record["bytes"] == len(content)
    if path.startswith("tests/"):
        assert (
            next(g for g in result["data"]["file_groups"] if g["kind"] == "tests")[
                "count"
            ]
            == 1
        )


def test_extracted_metadata_still_redacts_secrets_and_private_symbols(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (project.root / "app/modules/notes/services.py").write_text(
        '"""'
        + "-" * 5
        + "BEGIN PRIVATE"
        + " KEY"
        + "-" * 5
        + '"""\ndef _private(): pass\ndef public(): pass\n'
    )
    rc, text, _ = run(project, capsys, "context", "notes")
    assert rc == 0 and "BEGIN PRIVATE KEY" not in text and "_private" not in text
    assert "public:" in text and "Summary omitted" in text


def test_map_contains_concrete_facts_without_generic_advice_or_scaffolding(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = project.root / "app/modules/notes"
    (root / "api").mkdir()
    (root / "api/__init__.py").write_text(
        '"""API."""\nfrom .notes import router\n__all__ = ["router"]\n'
    )
    (root / "api/notes.py").write_text("def create_note(): pass\n")
    (root / "services").mkdir()
    (root / "services/__init__.py").write_text("def configure(): pass\n")
    (root / "tests").mkdir()
    (root / "tests/test_notes.py").write_text("def test_notes(): pass\n")
    doc = json.loads((project.root / INDEX).read_text())
    module = next(m for m in doc["modules"] if m["id"] == "module:notes")
    module["routes"] = [
        {
            "method": "GET",
            "path": "/notes",
            "name": "get_notes",
            "auth": "authenticated",
        },
        {"method": "POST", "path": "/notes", "name": "create_note", "auth": "public"},
    ]
    module["models"] = [
        {"table": "users", "tenant_scoped": False},
        {"table": "roles", "tenant_scoped": False},
        {"table": "notes", "tenant_scoped": True},
    ]
    (project.root / INDEX).write_text(encode(doc))
    rc, text, _ = run(project, capsys, "context", "notes")
    assert rc == 0
    for noise in (
        "## Next",
        "Module map.",
        "CONTRACT_OR_EVENT_ID",
        "KiB",
        "api/__init__.py",
        ".: 1",
        "[authenticated]",
        "- /notes (2)",
        "run sandloom",
    ):
        assert noise not in text
    assert "POST /notes → create_note [public]" in text
    assert "GET /notes → get_notes" in text
    assert "Global: roles, users" in text and "Tenant scoped: notes" in text
    assert "tests/ · 1 file" in text
    assert "services/__init__.py" in text and "configure:" in text


def test_map_reports_its_module_unresolved_facts(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = json.loads((project.root / INDEX).read_text())
    doc["unresolved"] = [
        {
            "module_id": "module:notes",
            "kind": "provider_not_in_composition",
            "related_id": "contract:stats.quota.v1",
            "required": False,
        }
    ]
    doc["processes"] = [
        {
            "id": "route:notes%3APOST%20%2Fnotes",
            "owner": "module:notes",
            "name": "POST /notes",
            "handler": "create_note",
        }
    ]
    (project.root / INDEX).write_text(encode(doc))
    rc, text, _ = run(project, capsys, "context", "notes")
    assert rc == 0 and "stats.quota.v1 — provider not in composition (optional)" in text
    assert '"module_id"' not in text


def test_single_published_event_uses_its_literal_channel(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, text, _ = run(project, capsys, "context", "stats")
    assert rc == 0 and "- stats.changed.v1" in text and "stats.{changed}.v1" not in text


@pytest.mark.parametrize(
    "state,label",
    [
        ("verified", "verified"),
        ("missing_receipt", "not verified on this checkout"),
        ("source_changed", "source changed since last generate"),
        (
            "dependency_changed",
            "declarations or dependencies changed since last generate",
        ),
    ],
)
def test_context_explains_freshness_without_changing_json_statuses(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    state: str,
    label: str,
) -> None:
    if state == "missing_receipt":
        (project.root / RECEIPT).unlink()
    elif state == "source_changed":
        (project.root / "app/modules/notes/services.py").write_text("# changed\n")
    elif state == "dependency_changed":
        path = project.root / "app/shared/contracts/stats_dto.py"
        path.write_text(path.read_text() + "# changed\n")
    rc, text, err = run(project, capsys, "context", "notes")
    assert rc == 0 and not err
    assert "Freshness: " + label in text
    assert "\n\n\n" not in text
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and json.loads(text)["freshness"]["state"] == state


def test_text_map_omits_exclusion_accounting(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = project.root / "app/modules/notes"
    (root / "api").mkdir()
    (root / "api/__init__.py").write_text('"""API package."""\n')
    (root / "ignored.py").write_text("# implementation\n")
    (project.root / ".sandloomignore").write_text("app/modules/notes/ignored.py\n")
    rc, text, _ = run(project, capsys, "context", "notes")
    assert rc == 0 and "Excluded file metadata" not in text
    assert "package_scaffolding" not in text and "context_ignore" not in text
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    counts = {o["reason"]: o["count"] for o in json.loads(text)["data"]["omissions"]}
    assert counts == {"package_scaffolding": 1, "context_ignore": 1}


@pytest.mark.parametrize("name", ["impact", "diff", "sequence", "state"])
def test_reserved_module_names_use_explicit_module_ids(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    path = project.root / INDEX
    path.write_text(path.read_text().replace("archive", name))
    (project.root / "app/modules/archive").rename(project.root / "app/modules" / name)
    rc, text, err = run(project, capsys, "graph", "module:" + name, "--hops", "0")
    assert rc == 0 and not err
    assert [m["id"] for m in json.loads(text)["data"]["modules"]] == ["module:" + name]


@pytest.mark.parametrize(
    "command,flags",
    [
        ("context", ["notes", "--detail", "full"]),
        ("context", ["notes", "--max-bytes", "1"]),
        ("context", ["notes", "--output", "map.md"]),
        ("context", ["notes", "--force"]),
        ("graph", ["--output", "map.html"]),
        ("graph", ["--force"]),
        ("graph", ["--module", "notes"]),
        ("graph", ["--view", "modules"]),
        ("graph", ["--item", "notes"]),
        ("graph", ["--process", "notes"]),
        ("graph", ["--before", "old.json"]),
    ],
)
def test_removed_options_are_usage_errors_without_writes(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    command: str,
    flags: list[str],
) -> None:
    before = {p: p.read_bytes() for p in project.root.rglob("*") if p.is_file()}
    rc, text, err = run(project, capsys, command, *flags, "--format", "json")
    assert rc == 2 and not err
    assert json.loads(text)["diagnostics"][0]["kind"] == "usage"
    assert before == {p: p.read_bytes() for p in project.root.rglob("*") if p.is_file()}


def test_sequence_actions_preserve_scope_and_full_verification(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _process_reader(project)
    rc, text, _ = run(project, capsys, "graph", "sequence")
    assert rc == 0 and len(json.loads(text)["data"]["processes"]) == 4
    rc, text, _ = run(
        project, capsys, "graph", "sequence", "route:notes-GET", "--detail", "full"
    )
    assert rc == 0
    doc = json.loads(text)
    assert doc["selection"]["module_ids"] == ["module:notes"]
    assert "evidence" in doc["freshness"]
    rc, text, _ = run(project, capsys, "graph", "state", "route:notes-GET")
    assert rc == 2


def test_graph_limit_is_retained_but_context_has_no_size_accounting(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, text, _ = run(project, capsys, "graph", "--max-bytes", "10")
    assert rc == 1 and json.loads(text)["diagnostics"][0]["kind"] == "budget_exceeded"
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0
    assert (
        "bytes" not in json.loads(text)["data"]
        and "budget" not in json.loads(text)["data"]
    )


def test_diff_existing_file_takes_precedence_over_revision_name(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.cli.inspection import history

    (project.root / "main").write_bytes((project.root / INDEX).read_bytes())
    monkeypatch.setattr(history.shutil, "which", lambda _: None)
    rc, text, err = run(project, capsys, "graph", "diff", "main")
    assert (
        rc == 0
        and not err
        and json.loads(text)["data"]["counts"]
        == {"added": 0, "removed": 0, "changed": 0}
    )


def test_explore_refuses_a_symlink_at_its_managed_output(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sandloom.core.cli import explore

    target = project.root / explore.OUTPUT
    target.symlink_to(project.root / "AGENTS.md")
    before = (project.root / "AGENTS.md").read_bytes()
    assert (
        explore.main(["--root", str(project.root), "--no-generate", "--no-open"]) == 1
    )
    assert (project.root / "AGENTS.md").read_bytes() == before
    assert "symlink" in capsys.readouterr().err


def test_explore_accepts_a_module_import_path(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sandloom.core.cli import explore

    assert (
        explore.main(
            [
                "app.modules.notes",
                "--root",
                str(project.root),
                "--hops",
                "0",
                "--no-generate",
                "--no-open",
            ]
        )
        == 0
    )
    assert "file:" in capsys.readouterr().out
    payload = _embedded_viewer((project.root / explore.OUTPUT).read_text())
    assert payload["selection"]["module_ids"] == ["module:notes"]


def test_context_returns_a_complete_map_above_the_former_byte_limit(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = json.loads((project.root / INDEX).read_text())
    doc["contract_descriptions"] = {
        "stats.quota.v1": {
            "methods": {
                f"read_{n}": {
                    "kind": "read",
                    "params": [],
                    "returns": {"k": "scalar", "t": "int"},
                }
                for n in range(4000)
            }
        }
    }
    (project.root / INDEX).write_text(encode(doc))
    rc, text, err = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and not err and len(text.encode()) > 131072
    assert len(json.loads(text)["data"]["contracts_used"][0]["methods"]) == 4000


def test_project_overview_uses_purposes_and_module_connections(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main("graph", ["--root", str(project.root)])
    text, err = capsys.readouterr()
    assert rc == 0 and not err
    assert "notes: Notes purpose." in text and "archive: Archive purpose." in text
    assert "calls stats" in text and "listens to stats" in text
    assert "provides stats.quota.v1" in text and "1 event" in text
    assert "database" not in text and "Internal details" not in text
    assert len(text.encode()) / 3 <= 300
    rc, text, _ = run(project, capsys, "graph")
    assert rc == 0
    assert {m["id"]: m["summary"] for m in json.loads(text)["data"]["modules"]} == {
        "module:" + n: n.title() + " purpose." for n in ("notes", "stats", "archive")
    }


def test_graph_piped_to_cat_is_text_without_application_imports(
    project: SimpleNamespace,
) -> None:
    script = """
import sys, socket
class Deny:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('app', 'yaml', 'sandloom.sdk', 'sandloom.core.config')):
            raise AssertionError('unexpected import ' + fullname)
sys.meta_path.insert(0, Deny())
socket.socket = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network'))
from sandloom.core.cli.inspection.cli import main
raise SystemExit(main('graph', []))
"""
    producer = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=project.root,
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert producer.stdout is not None
    copied = subprocess.run(
        ["/bin/cat"], stdin=producer.stdout, capture_output=True, timeout=30
    )
    producer.stdout.close()
    _, err = producer.communicate(timeout=30)
    assert producer.returncode == 0 and copied.returncode == 0 and not err
    assert b"notes: Notes purpose." in copied.stdout
    assert not copied.stdout.startswith(b"{")


@pytest.mark.parametrize(
    "command,args", [("context", ["notes", "--focus", "X"]), ("graph", ["modules"])]
)
def test_removed_inspection_modes_are_usage_errors(
    project: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: list[str],
) -> None:
    rc = main(command, ["--root", str(project.root), *args])
    text, err = capsys.readouterr()
    assert rc == 2 and not text and "usage" in err


def test_modules_is_an_ordinary_module_name(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project.root / INDEX
    path.write_text(path.read_text().replace("archive", "modules"))
    (project.root / "app/modules/archive").rename(project.root / "app/modules/modules")
    rc, text, err = run(project, capsys, "graph", "modules", "--hops", "0")
    assert rc == 0 and not err
    assert [m["id"] for m in json.loads(text)["data"]["modules"]] == ["module:modules"]


def test_default_map_exposes_command_risk_without_duplicate_idempotency(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = project.root / INDEX
    doc = json.loads(path.read_text())
    doc["contract_descriptions"] = {
        "stats.quota.v1": {
            "methods": {
                "charge": {
                    "kind": "command",
                    "risk": "money",
                    "idempotency_key": "operation_key",
                    "params": [
                        {
                            "name": "operation_key",
                            "kind": "positional_or_keyword",
                            "required": True,
                            "type": {"k": "scalar", "t": "str"},
                        }
                    ],
                    "returns": {"k": "scalar", "t": "int"},
                }
            }
        }
    }
    path.write_text(encode(doc))
    rc, text, err = run(project, capsys, "context", "stats")
    assert rc == 0 and not err
    assert "command charge(operation_key: str) -> int  [money]" in text
    assert "idempotency_key" not in text
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    method = json.loads(text)["data"]["contracts_used"][0]["methods"][0]
    assert rc == 0 and method["risk"] == "money" and "idempotency_key" not in method


@pytest.mark.parametrize(
    "state,label",
    [
        ("missing_receipt", "not verified on this checkout"),
        ("source_changed", "source changed since last generate"),
        (
            "dependency_changed",
            "declarations or dependencies changed since last generate",
        ),
        ("partial", "verification incomplete for this scope"),
    ],
)
def test_graph_and_context_share_freshness_wording(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str], state: str, label: str
) -> None:
    from sandloom.core.cli.inspection import terminal
    from sandloom.core.cli.inspection.model import response

    envelope = response("graph", "graph", {"modules": []}, freshness={"state": state})
    assert "Freshness: " + label in terminal.render(envelope)
    if state == "missing_receipt":
        (project.root / RECEIPT).unlink()
        rc, text, err = run(project, capsys, "graph", "--format", "text")
        assert rc == 0 and not err and "Freshness: " + label in text
        rc, text, _ = run(project, capsys, "graph")
        assert rc == 0 and json.loads(text)["freshness"]["state"] == state


@pytest.mark.parametrize(
    "args",
    [["notes"], ["impact", "stats.quota.v1"], ["diff", "before.json"], ["sequence"]],
)
def test_graph_text_has_no_next_step_footer(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    (project.root / "before.json").write_bytes((project.root / INDEX).read_bytes())
    rc, text, err = run(project, capsys, "graph", *args, "--format", "text")
    assert rc == 0 and not err and "sandloom explore" not in text
    if args[0] == "impact":
        assert text.count("Potential impact:") == 1
        assert "an overapproximation, not complete call-site or test coverage" in text


def test_sequence_text_catalog_uses_selectable_names_and_keeps_json_ids(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = _process_reader(project)
    doc = reader.doc
    doc["processes"][0]["id"] = "route:notes%3AGET%20%2Fnotes"
    _publish(project, doc)
    rc, text, err = run(project, capsys, "graph", "sequence", "--format", "text")
    assert rc == 0 and not err and "GET /notes · notes" in text
    assert "route:" not in text and "%3A" not in text
    rc, text, _ = run(project, capsys, "graph", "sequence")
    assert (
        rc == 0
        and json.loads(text)["data"]["processes"][0]["id"]
        == "route:notes%3AGET%20%2Fnotes"
    )
    rc, text, _ = run(
        project, capsys, "graph", "sequence", "GET /notes", "--format", "text"
    )
    assert rc == 0 and "Forward path" in text


@pytest.mark.parametrize("count", [0, 1, 2])
def test_project_text_pluralization_and_docstring_markup(
    project: SimpleNamespace, capsys: pytest.CaptureFixture[str], count: int
) -> None:
    path = project.root / INDEX
    doc = json.loads(path.read_text())
    module = doc["modules"][0]
    module["summary"] = "Owns the ``notes`` module."
    module["routes"] = [
        {"method": "GET", "path": "/notes", "name": "list_notes"}
    ] * count
    module["models"] = [{"table": "notes"}] * count
    path.write_text(encode(doc))
    (project.root / "app/modules/notes/services.py").write_text(
        '"""Handles ``notes`` with `db()`."""\n'
    )
    rc = main("graph", ["--root", str(project.root)])
    text, err = capsys.readouterr()
    assert rc == 0 and not err and "Owns the `notes` module." in text
    suffix = "" if count == 1 else "s"
    assert f"{count} route{suffix} · {count} table{suffix}" in text
    rc, text, _ = run(project, capsys, "context", "notes")
    assert rc == 0 and "Handles `notes` with `db()`." in text and "``" not in text
    rc, text, _ = run(project, capsys, "context", "notes", "--format", "json")
    assert rc == 0 and "Handles ``notes`` with `db()`." in text


def test_viewer_uses_full_scoped_module_cards_and_readable_freshness(
    project: SimpleNamespace,
) -> None:
    path = project.root / INDEX
    document = json.loads(path.read_text())
    module = next(m for m in document["modules"] if m["id"] == "module:notes")
    module["routes"] = [
        {
            "method": "GET",
            "path": "/notes",
            "name": "list_notes",
            "auth": "authenticated",
        }
    ]
    path.write_text(encode(document))
    (project.root / RECEIPT).unlink()
    reader = Reader(project.root)
    compact = graph.build(
        reader,
        selector="notes",
        hops=0,
        discovery=False,
        detail="compact",
        require=False,
    )
    assert "routes" not in compact["data"]["modules"][0]
    payload = _embedded_viewer(graph.viewer(compact, reader))
    assert payload["presentation"]["freshness"] == "not verified on this checkout"
    cards = payload["workspace"]["modules"]
    assert len(cards) == 1 and cards[0]["id"] == "module:notes"
    assert cards[0]["routes"] == module["routes"]
    assert cards[0]["summary"] == "Notes purpose."
