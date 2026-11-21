"""Parallel branches that each add a module merge with no manual step.

``sandloom generate`` rewrites the tracked ``.sandloom/inspection.json``. It used
to be one line of JSON, so any two branches that regenerated it conflicted. Now
it is one record per line: two branches add different lines, and the
``merge=union`` line of the scaffold's ``.gitattributes`` keeps both where the
lines land in the same gap. The end-to-end test runs real ``git`` and the real
CLI; the others pin the format that makes it work.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli.inspection import collect
from sandloom.core.cli.inspection.model import (
    LINES,
    InspectionError,
    decode_index,
    encode,
    encode_index,
)
from sandloom.core.cli.inspection.read import parse_index
from sandloom.core.tests._root_support import redis_free_infra_env

_APP = "mergeapp"
_CLI = "import sys; from sandloom import main; sys.argv[0] = 'sandloom'; main()"


def _env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(root)),
        "SANDLOOM_COMPOSITION_ROOT": f"{_APP}.asgi",
        "PYTHONPATH": str(root),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        # the kernel-without-redis lane: the app's Redis drivers become memory
        **redis_free_infra_env(),
    }


def _run(
    root: Path, *argv: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv, cwd=root, env=_env(root), capture_output=True, text=True, timeout=240
    )
    if check:
        assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
    return result


def _sandloom(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(root, sys.executable, "-c", _CLI, *args, check=check)


def _git(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(root, "git", *args, check=check)


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A new app with one commit on ``main``: generated, inspection index tracked."""
    parent = tmp_path_factory.mktemp("merge-base")
    _sandloom(
        parent,
        "new",
        _APP,
        "--dir",
        str(parent),
        "--yes",
        "--no-batteries",
        "--agents",
        "claude",
        "--no-git",
    )
    root = parent / _APP
    if redis_free_infra_env():
        # The kernel-without-redis lane: the project's `.env.test` INFRA (urls of
        # Redis instances) would win over the memory drivers of the child's INFRA.
        env_test = root / ".env.test"
        env_test.write_text(
            "".join(
                line
                for line in env_test.read_text("utf-8").splitlines(keepends=True)
                if not line.startswith("INFRA=")
            ),
            "utf-8",
        )
    _sandloom(root, "generate")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _branch(root: Path, name: str) -> None:
    """A branch off ``main`` that adds module ``name`` and regenerates."""
    _git(root, "checkout", "-q", "-b", name, "main")
    _sandloom(root, "add", "module", name)
    _sandloom(root, "generate")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", name)


def _two_branches(base: Path, tmp_path: Path, first: str, second: str) -> Path:
    root = tmp_path / _APP
    shutil.copytree(base, root)
    _branch(root, first)
    _branch(root, second)
    _git(root, "checkout", "-q", first)
    return root


def _module_ids(root: Path) -> list[str]:
    document = parse_index((root / ".sandloom/inspection.json").read_text("utf-8"))
    return [m["id"] for m in document["modules"]]


@pytest.mark.subprocess_probe
def test_two_branches_that_each_add_a_module_merge_and_need_no_generate(
    base: Path, tmp_path: Path
) -> None:
    """``alpha`` and ``beta`` both sort before the app's ``example`` module: every
    section of the index gets both modules' lines in one gap."""
    attributes = (base / ".gitattributes").read_text("utf-8")
    assert ".sandloom/inspection.json merge=union text eol=lf" in attributes
    root = _two_branches(base, tmp_path, "alpha", "beta")
    merge = _git(root, "merge", "--no-edit", "beta", check=False)
    assert merge.returncode == 0, merge.stdout + merge.stderr
    assert _module_ids(root) == ["module:alpha", "module:beta", "module:example"]
    # what the merge left is current: the gate passes and generate changes nothing
    check = _sandloom(root, "generate", "--check", check=False)
    assert check.returncode == 0, check.stdout + check.stderr
    _sandloom(root, "generate")
    assert _git(root, "status", "--porcelain").stdout == ""
    # and it is readable as it is
    assert _sandloom(root, "graph", "--format", "json", check=False).returncode == 0


@pytest.mark.subprocess_probe
def test_a_crlf_copy_of_the_index_is_read_by_graph_and_generate(
    base: Path, tmp_path: Path
) -> None:
    """Windows ``core.autocrlf`` and some editors write CRLF line ends."""
    root = tmp_path / _APP
    shutil.copytree(base, root)
    index = root / ".sandloom/inspection.json"
    lf_bytes = index.read_bytes()
    assert b"\r" not in lf_bytes
    index.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))
    graph = _sandloom(root, "graph", "--format", "json", check=False)
    assert graph.returncode == 0, graph.stdout + graph.stderr
    assert "SANDLOOM-T015" not in graph.stdout + graph.stderr
    generate = _sandloom(root, "generate", check=False)
    assert generate.returncode == 0, generate.stdout + generate.stderr
    check = _sandloom(root, "generate", "--check", check=False)
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.subprocess_probe
def test_without_the_union_attribute_the_same_gap_still_conflicts(
    base: Path, tmp_path: Path
) -> None:
    """The attribute is what keeps both modules where their lines meet: take it
    away and git reports the conflict the one-line index used to give."""
    root = tmp_path / _APP
    shutil.copytree(base, root)
    (root / ".gitattributes").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "no attributes")
    for name in ("alpha", "beta"):
        _branch(root, name)
    _git(root, "checkout", "-q", "alpha")
    merge = _git(root, "merge", "--no-edit", "beta", check=False)
    assert merge.returncode != 0
    assert ".sandloom/inspection.json" in merge.stdout


# --------------------------------------------------------------------------- #
# The format: what makes the merge above possible.
# --------------------------------------------------------------------------- #


def _document(*names: str) -> dict[str, Any]:
    modules = [
        {"id": f"module:{n}", "name": n, "routes": [{"path": f"/{n}"}]} for n in names
    ]
    return {
        "schema": "sandloom.inspection",
        "schema_version": 2,
        "layout": {"modules_path": "app/modules"},
        "scope": {
            "kind": "authoring_composition",
            "module_ids": sorted(m["id"] for m in modules),
        },
        "modules": modules,
        "nodes": [{"id": f"module:{n}", "kind": "module", "name": n} for n in names],
        "edges": [],
        "unresolved": [
            {"kind": "provider_not_in_composition", "module_id": f"module:{n}"}
            for n in names
        ],
        "event_schemas": {f"{n}.created.v1": {"type": "object"} for n in names},
        "wire_documents": {},
    }


def test_the_index_is_one_record_per_line_and_reads_back_as_the_document() -> None:
    document = _document("alpha", "example")
    text = encode_index(document)
    lines = text.splitlines()
    assert lines[0] == LINES
    assert all(json.loads(line) for line in lines)  # each line is a JSON value
    assert len([line for line in lines if '"module:alpha"' in line]) == 3
    assert decode_index(text) == document
    # an empty section stays declared
    assert decode_index(text)["wire_documents"] == {}
    assert decode_index(text)["edges"] == []


def test_adding_a_module_only_adds_lines() -> None:
    """The property the merge rests on: another module's records are never
    rewritten, so two branches never edit one line."""
    before = set(encode_index(_document("example")).splitlines())
    after = set(encode_index(_document("alpha", "example")).splitlines())
    assert before - after == set()  # no line of the others is touched
    added = after - before
    assert len(added) == 4  # its module, node, unresolved entry and event schema
    assert all("alpha" in line for line in added)


def test_lines_that_union_repeats_are_read_once() -> None:
    """``merge=union`` writes a line both sides share once per side."""
    document = _document("alpha", "example")
    lines = encode_index(document).splitlines()
    repeated = [*lines, lines[1], lines[3], lines[-1]]
    assert decode_index("\n".join(repeated) + "\n") == document


def test_the_order_of_the_lines_does_not_change_the_document() -> None:
    document = _document("alpha", "beta", "example")
    lines = encode_index(document).splitlines()
    body = lines[1:]
    random.Random(7).shuffle(body)
    assert decode_index("\n".join([lines[0], *body]) + "\n") == document


def test_a_merged_file_is_kept_as_it_is_when_it_is_the_same_document() -> None:
    document = _document("alpha", "beta")
    lines = encode_index(document).splitlines()
    shuffled = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
    assert shuffled != encode_index(document)
    assert collect.rendered(document, shuffled) == shuffled


def test_a_merged_file_that_is_another_document_is_regenerated() -> None:
    old = encode_index(_document("alpha"))
    new = _document("alpha", "beta")
    assert collect.rendered(new, old) == encode_index(new)
    assert collect.rendered(new, "not an index\n") == encode_index(new)
    assert collect.rendered(new, None) == encode_index(new)
    # the one-line form of an earlier release is the same document, and is
    # still rewritten as lines
    assert collect.rendered(new, encode(new)) == encode_index(new)


def test_two_versions_of_one_record_are_refused_not_read() -> None:
    """A merge that kept both sides' version of a changed record (`union` on a
    real conflict) is stale, and says so."""
    document = _document("alpha")
    lines = encode_index(document).splitlines()
    changed = json.loads(next(line for line in lines if line.startswith('["nodes",')))
    changed[1]["name"] = "renamed"
    text = "\n".join([*lines, encode(changed).strip()]) + "\n"
    with pytest.raises(InspectionError) as caught:
        decode_index(text)
    assert caught.value.kind == "invalid_artifact"
    assert "sandloom generate" in str(caught.value)
    assert collect.rendered(document, text) == encode_index(document)


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"an": "object"}',
        '["edges",{"id":"x"}]',  # an item of a section nothing declared
        '["nodes","a","b"]',  # a map entry in a list section
        '["@val","nodes",[]]',  # declared twice, differently
        '["@map","nodes"]',
        '["@val"]',
    ],
)
def test_a_damaged_index_is_one_typed_error(line: str) -> None:
    text = "\n".join([LINES, '["@list","nodes"]', line]) + "\n"
    with pytest.raises(InspectionError) as caught:
        decode_index(text)
    assert caught.value.kind == "invalid_artifact"


def test_crlf_line_ends_read_as_the_same_document() -> None:
    document = _document("alpha", "example")
    text = encode_index(document)
    crlf = text.replace("\n", "\r\n")
    assert decode_index(crlf) == document
    assert parse_index(crlf) == document  # recognised as the lines format
    assert collect.rendered(document, crlf) == crlf  # a CRLF copy is not a change
    # the one-line form of an earlier release with a CRLF end still reads
    assert parse_index(encode(document).replace("\n", "\r\n")) == document


def test_a_non_string_module_id_is_one_typed_error() -> None:
    document = _document("alpha", "beta")  # two ids: sorting them compares
    del document["scope"]["module_ids"]
    for bad in (None, 7, ["module:alpha"]):
        document["modules"][0]["id"] = bad
        with pytest.raises(InspectionError) as encoded:
            encode_index(document)
        assert encoded.value.kind == "invalid_artifact"
        lines = [LINES, '["@val","scope",{"kind":"authoring_composition"}]']
        lines += ['["@list","modules"]', '["modules",{"id":"module:beta"}]']
        lines += [json.dumps(["modules", {"id": bad}])]
        with pytest.raises(InspectionError) as decoded:
            decode_index("\n".join(lines) + "\n")
        assert decoded.value.kind == "invalid_artifact"


def test_a_summary_with_unicode_line_separators_survives_the_round_trip() -> None:
    """``encode_index`` leaves U+2028, U+2029 and U+0085 unescaped: only LF
    ends a record."""
    document = _document("alpha")
    separators = "".join(chr(code) for code in (0x2028, 0x2029, 0x85, 0x0B, 0x0C))
    summary = "first" + separators + "last"
    document["modules"][0]["summary"] = summary
    text = encode_index(document)
    assert decode_index(text) == document
    assert decode_index(text.replace("\n", "\r\n")) == document
    assert parse_index(text)["modules"][0]["summary"] == summary


def test_a_repeated_key_within_a_record_is_refused_not_read() -> None:
    """``json.loads`` keeps the last of a repeated key: a conflicting record
    inside one line would go unreported."""
    lines = [LINES, '["@list","nodes"]', '["nodes",{"id":"x","name":"a","name":"b"}]']
    with pytest.raises(InspectionError) as caught:
        decode_index("\n".join(lines) + "\n")
    assert caught.value.kind == "invalid_artifact"
    assert "Duplicate JSON keys" in str(caught.value)


def test_the_one_line_index_of_an_earlier_release_is_still_read() -> None:
    document = _document("alpha")
    assert parse_index(encode(document)) == document
