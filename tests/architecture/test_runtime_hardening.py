"""Production deployment restrictions and the image's privilege-removal boundary."""

from __future__ import annotations

import runpy
import stat
from pathlib import Path

import pytest
import yaml

from sandloom.core.cli.project import generate_project
from sandloom.core.testing.deploy_profile import app_processes

ROOT = Path(__file__).resolve().parents[2]
HARDENER = ROOT / "src/sandloom/core/cli/templates/deploy/harden_runtime.py"


def assert_restricted(profile: dict) -> None:
    for name, service in app_processes(profile).items():
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        mounts = service.get("tmpfs", [])
        assert any(
            m.startswith("/tmp:")
            and "nosuid" in m
            and "noexec" in m
            and "uid=10001" in m
            and "size=" in m
            for m in mounts
        ), name


@pytest.mark.parametrize("name", ["monolith", "microservices"])
def test_shipped_application_processes_are_restricted(name: str) -> None:
    assert_restricted(yaml.safe_load((ROOT / f"deploy/compose/{name}.yml").read_text()))


def test_new_projects_have_the_same_runtime_boundary(tmp_path: Path) -> None:
    generate_project("secureapp", tmp_path, with_batteries=[])
    profile = yaml.safe_load(
        (tmp_path / "secureapp/deploy/docker-compose.yml").read_text()
    )
    assert_restricted(profile)
    image = (tmp_path / "secureapp/Dockerfile").read_text()
    assert "packages_to_remove" in image
    assert "strip_privileged_bits" in image


def test_package_minimization_never_forces_essential_or_protected_removal() -> None:
    remove = runpy.run_path(str(HARDENER))["packages_to_remove"]
    assert remove(
        "apt\tno\tno\nmount\tno\tno\nlibapt-pkg7.0:arm64\tno\tno\nlibstdc++6\tno\tno\n"
    ) == ["apt", "mount", "libapt-pkg7.0"]
    for flag in ["yes\tno", "no\tyes"]:
        with pytest.raises(ValueError, match="essential or protected"):
            remove("apt\t" + flag + "\n")


def test_privileged_bit_removal_preserves_executable_and_ignores_links(
    tmp_path: Path,
) -> None:
    strip = runpy.run_path(str(HARDENER))["strip_privileged_bits"]
    binary = tmp_path / "bin"
    binary.mkdir()
    tool = binary / "tool"
    tool.write_text("fixture")
    tool.chmod(0o6755)
    outside = tmp_path / "outside"
    outside.write_text("outside")
    outside.chmod(0o4755)
    (binary / "link").symlink_to(outside)
    strip((binary,))
    assert stat.S_IMODE(tool.stat().st_mode) == 0o755
    assert stat.S_IMODE(outside.stat().st_mode) == 0o4755


@pytest.mark.parametrize(
    "name", ["academy", "helpdesk", "linkly", "projecthub", "tutorial"]
)
def test_shipped_examples_have_the_same_runtime_boundary(name: str) -> None:
    profile = yaml.safe_load(
        (ROOT / f"examples/{name}/deploy/docker-compose.yml").read_text()
    )
    assert_restricted(profile)
    assert "strip_privileged_bits" in (ROOT / f"examples/{name}/Dockerfile").read_text()


def test_zlib_conclusion_rejects_a_different_binary_or_source() -> None:
    verify = runpy.run_path(str(ROOT / "tools/checks/zlib_source_evidence.py"))[
        "verify"
    ]
    inventory = {
        "machine": "aarch64",
        "packages": [{"name": "zlib1g", "version": "reviewed"}],
        "library_sha256": {"/lib/libz.so.1": "expected"},
    }
    expected = {"source_orig_sha256": "source", "debian_packaging_sha256": "packaging"}
    proof = {
        "machine": "aarch64",
        "binary_version": "reviewed",
        "binary_library_sha256": {"libz.so.1": "expected"},
        **expected,
        "debian_patches_present": False,
        "nonblocking_mechanism_present": False,
        "fatal_write_error": True,
        "returns_before_memmove": True,
        "assessment": "under_investigation",
    }
    assert (
        verify(dict(proof), inventory, expected)["assessment"] == "under_investigation"
    )
    for key, value in [
        ("machine", "x86_64"),
        ("binary_version", "different"),
        ("source_orig_sha256", "unreviewed"),
        ("nonblocking_mechanism_present", True),
        ("binary_library_sha256", {"libz.so.1": "different"}),
    ]:
        with pytest.raises(ValueError):
            verify({**proof, key: value}, inventory, expected)


def test_runtime_review_reassesses_new_components_and_refuses_new_cves() -> None:
    import json

    namespace = runpy.run_path(str(ROOT / "tools/checks/runtime_review.py"))
    review = json.loads(
        (ROOT / "tools/checks/debian-runtime-review-2026-10-31.json").read_text()
    )
    tools = dict.fromkeys(
        [
            "getfacl",
            "setfacl",
            "chacl",
            "getfattr",
            "setfattr",
            "apt-key",
            "bzip2recover",
            "nscd",
            "systemd",
            "systemd-homed",
            "systemd-oomd",
            "systemd-journald",
            "journalctl",
            "systemd-tmpfiles",
        ]
    )
    inventory = {
        "tools": tools,
        "systemd_paths": {"/usr/lib/systemd/systemd": False},
        "perl_modules": {"Archive/Tar.pm": False, "Pod/Text.pm": False},
        "sqlite_modules": [],
        "sqlite_zipfile_extensions": [],
        "symbols": {"sqlite3": {"sqlite3changeset_apply_v3": False}},
        "machine": "aarch64",
        "tool_source_packages": {"chfn": None, "chsh": None},
        "no_new_privileges": 1,
        "cap_effective": 0,
        "cap_bounding": 0,
        "privileged_files": [],
        "boundary_checks": "pass",
        "mounts": [{"path": "/", "options": ["ro"]}],
        "packages": [{"source": "attr"}],
    }
    scan = {"vulnerabilities": [{"id": "SNYK-DEBIAN13-ATTR-17678182"}]}
    result = namespace["assess"](review, inventory, scan, None)
    row = next(
        r for r in result["findings"] if r["id"] == scan["vulnerabilities"][0]["id"]
    )
    assert row["status"] == "not_affected_component_absent"
    assert row["automatic_scanner_ignore_authorized"] is False
    tools["getfattr"] = "/usr/bin/getfattr"
    result = namespace["assess"](review, inventory, scan, None)
    assert (
        next(r for r in result["findings"] if r["id"] == row["id"])["status"]
        == "needs_reassessment"
    )
    with pytest.raises(ValueError, match="Unreviewed scanner findings"):
        namespace["assess"](
            review, inventory, {"vulnerabilities": [{"id": "NEW-CVE"}]}, None
        )
