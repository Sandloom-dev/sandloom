"""Changelog fragments: ``changelog.d/<alias>.md`` replaces edits of ``[Unreleased]``.

The checks run on a tree built in ``tmp_path``: each refusal of ``--check`` and of
``--fold`` has its own case, so a validator that accepts anything fails here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLING = ROOT / "tools" / "packaging"

GOOD = """### Added

- **One** (alpha). Text.
  A continuation line.
- Two.

### Fixed

- Three.
"""

CHANGELOG = """# Release Notes

## [Unreleased]

- A legacy loose bullet.

## [0.7.0a13]

- Older.
"""


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(TOOLING))
    import changelog_fragments
    import pypi_release

    return changelog_fragments, pypi_release


PACKAGE = "sandloom-redis"
PACKAGE_CHANGELOG = """# Changelog — sandloom-redis

- Not a release heading.

## [0.7.0a13]

- Older package notes.
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    # The framework package carries the coordinated version, a package its own.
    for name in ("sandloom", PACKAGE):
        folder = tmp_path / "packages" / name
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_text('[project]\nversion = "0.7.0a14"\n')
    (tmp_path / "packages" / PACKAGE / "CHANGELOG.md").write_text(PACKAGE_CHANGELOG)
    return tmp_path


def write(repo: Path, name: str, text: str, package: str | None = None) -> None:
    folder = repo / "changelog.d" / (package or "")
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text)


def test_the_repository_fragments_are_sound(tools):
    fragments, _ = tools
    assert fragments.check(ROOT) == []


def test_a_sound_fragment_and_an_absent_directory_pass(tools, repo):
    fragments, _ = tools
    write(repo, "alpha.md", GOOD)
    write(repo, "README.md", "not a fragment\n")
    assert fragments.check(repo) == []
    (repo / "changelog.d" / "alpha.md").unlink()
    (repo / "changelog.d" / "README.md").unlink()
    (repo / "changelog.d").rmdir()
    assert fragments.check(repo) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("### Breaking\n\n- x\n", "unknown kind 'Breaking'"),
        ("- x\n", "needs a known '### <kind>'"),
        ("### Wrong\n\n- x\n", "needs a known '### <kind>'"),
        ("### Added\n\n- x\n\n### Added\n\n- y\n", "appears twice"),
        ("### Added\n\n### Fixed\n\n- y\n", "kind 'Added' has no bullet"),
        ("### Added\n\n- \n", "empty bullet"),
        ("### Added\n\nplain prose\n", "only '### <kind>' headings"),
        ("### Added\n\n- x\n\n  detached\n", "only '### <kind>' headings"),
        ("", "no '### <kind>' section"),
        ("\n\n", "no '### <kind>' section"),
    ],
)
def test_check_refuses_each_malformed_fragment(tools, repo, text, message):
    fragments, _ = tools
    write(repo, "alpha.md", text)
    errors = fragments.check(repo)
    assert errors, text
    assert any(message in error for error in errors), errors


def test_check_refuses_names_and_entries_that_are_not_fragments(tools, repo):
    fragments, _ = tools
    write(repo, "Alpha.md", GOOD)
    write(repo, "alpha.md", GOOD)
    write(repo, "notes.txt", GOOD)
    write(repo, "_x.md", GOOD)
    (repo / "changelog.d" / "sub.md").mkdir()
    errors = "\n".join(fragments.check(repo))
    for name in ("Alpha.md", "notes.txt", "_x.md", "sub.md"):
        assert f"changelog.d/{name}" in errors, name
    # On a case-sensitive disk both exist; one is the duplicate of the other.
    names = {p.name for p in (repo / "changelog.d").iterdir()}
    if {"Alpha.md", "alpha.md"} <= names:
        assert "ignoring case" in errors


def test_check_refuses_two_names_that_differ_only_in_case(tools, repo, monkeypatch):
    fragments, _ = tools
    folder = repo / "changelog.d"
    monkeypatch.setattr(
        fragments,
        "fragment_paths",
        lambda root, package=None: [folder / "Alpha.md", folder / "alpha.md"],
    )
    assert any("ignoring case" in e for e in fragments.check(repo))


def test_check_exit_status_follows_the_errors(tools, repo, capsys):
    fragments, _ = tools
    assert fragments.main(["--check", "--root", str(repo)]) == 0
    write(repo, "alpha.md", "- x\n")
    assert fragments.main(["--check", "--root", str(repo)]) == 1
    assert "alpha.md:1" in capsys.readouterr().err


def test_fold_writes_the_section_in_kind_order_and_deletes_the_fragments(tools, repo):
    fragments, release = tools
    write(
        repo, "beta.md", "### Fixed\n\n- Fix B.\n\n### Breaking changes\n\n- Break B.\n"
    )
    write(repo, "alpha.md", GOOD)
    fragments.fold(repo, "0.7.0a14", "2026-11-20")
    text = (repo / "CHANGELOG.md").read_text()
    assert text.startswith(
        "# Release Notes\n\n## [Unreleased]\n\n## [0.7.0a14] — 2026-11-20\n"
    )
    section = release.changelog_section(repo / "CHANGELOG.md", "0.7.0a14")
    # The loose legacy bullets are the first of "### Changed": nothing precedes
    # "### Breaking changes" in the section.
    assert section.startswith("### Breaking changes")
    order = [
        section.index(part)
        for part in (
            "### Breaking changes",
            "Break B.",
            "### Added",
            "- **One** (alpha). Text.\n  A continuation line.\n- Two.",
            "### Changed\n\n- A legacy loose bullet.",
            "### Fixed",
            "- Three.\n- Fix B.",
        )
    ]
    assert order == sorted(order)
    assert release.changelog_section(repo / "CHANGELOG.md", "0.7.0a13") == "- Older.\n"
    assert "A legacy loose bullet." not in text.split("## [0.7.0a14]")[0]
    assert list((repo / "changelog.d").iterdir()) == []


def test_fold_keeps_the_readme_and_works_without_legacy_bullets(tools, repo):
    fragments, _ = tools
    (repo / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [0.7.0a13]\n\n- Older.\n")
    write(repo, "README.md", "readme\n")
    write(repo, "alpha.md", GOOD)
    fragments.fold(repo, "0.7.0a14", "2026-11-20")
    assert [p.name for p in (repo / "changelog.d").iterdir()] == ["README.md"]
    assert (
        (repo / "CHANGELOG.md")
        .read_text()
        .startswith("## [Unreleased]\n\n## [0.7.0a14] — 2026-11-20\n\n### Added\n")
    )


@pytest.mark.parametrize(
    ("version", "fragment", "changelog", "message"),
    [
        ("v1", GOOD, CHANGELOG, "Not a release version"),
        ("0.7.0a13", GOOD, CHANGELOG, "already has a section"),
        ("0.7.0a14", "- x\n", CHANGELOG, "Fragments are not sound"),
        ("0.7.0a14", GOOD, "# Notes\n", "nor release section"),
        ("0.7.0a14", None, "## [Unreleased]\n\n## [0.7.0a13]\n", "Nothing to fold"),
        ("0.7.0a15", GOOD, CHANGELOG, "is not the package version 0.7.0a14"),
        (
            "0.7.0a14",
            GOOD,
            "## [Unreleased]\n\n### Added\n\n- Hand-made.\n",
            "has '###' headings",
        ),
    ],
)
def test_fold_refuses_and_leaves_every_file_as_it_was(
    tools, repo, version, fragment, changelog, message
):
    fragments, _ = tools
    (repo / "CHANGELOG.md").write_text(changelog)
    if fragment is not None:
        write(repo, "alpha.md", fragment)
    before = {p: p.read_text() for p in (repo / "changelog.d").iterdir()}
    with pytest.raises(ValueError, match=message):
        fragments.fold(repo, version, "2026-11-20")
    assert (repo / "CHANGELOG.md").read_text() == changelog
    assert {p: p.read_text() for p in (repo / "changelog.d").iterdir()} == before


def release_inputs(release, tmp_path: Path, repo: Path) -> tuple[dict, Path, Path]:
    version = "0.7.0a14"
    validated = tmp_path / "validated"
    validated.mkdir()
    checksums = {}
    for name in release.PACKAGES:
        for filename in release.filenames(name, version):
            (validated / filename).write_bytes(filename.encode())
            checksums[filename] = hashlib.sha256(filename.encode()).hexdigest()
    (validated / "sha256.json").write_text(json.dumps(checksums))
    plan = {
        "packages": list(release.PACKAGES),
        "versions": dict.fromkeys(release.PACKAGES, version),
    }
    return plan, validated, tmp_path / "reviewed"


def test_a_coordinated_release_refuses_unfolded_fragments_and_passes_once_folded(
    tools, repo, tmp_path_factory, monkeypatch
):
    fragments, release = tools
    work = tmp_path_factory.mktemp("release")
    plan, validated, reviewed = release_inputs(release, work, repo)
    write(repo, "alpha.md", GOOD)
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="Unfolded changelog fragments.*alpha.md"):
        release.select_artifacts(plan, validated, reviewed)
    assert not reviewed.exists()
    fragments.fold(repo, "0.7.0a14", "2026-11-20")
    release.select_artifacts(plan, validated, reviewed)
    notes = (reviewed / "release-notes.md").read_text()
    assert "- **One** (alpha). Text." in notes


def test_a_single_package_release_reads_its_own_changelog_and_ignores_fragments(
    tools, repo, tmp_path_factory, monkeypatch
):
    _, release = tools
    work = tmp_path_factory.mktemp("release")
    plan, validated, reviewed = release_inputs(release, work, repo)
    only = release.PACKAGES[0]
    plan["packages"] = [only]
    package = repo / "packages" / only
    package.mkdir(parents=True, exist_ok=True)
    (package / "CHANGELOG.md").write_text("## [0.7.0a14]\n\n- Own notes.\n")
    write(repo, "alpha.md", GOOD)
    write(repo, "alpha.md", GOOD, PACKAGE)  # another package's fragment: not its scope
    monkeypatch.chdir(repo)
    release.select_artifacts(plan, validated, reviewed)
    assert (reviewed / "release-notes.md").read_text() == "- Own notes.\n"


def test_a_package_release_refuses_the_fragments_of_that_package(
    tools, repo, tmp_path_factory, monkeypatch
):
    _, release = tools
    work = tmp_path_factory.mktemp("release")
    plan, validated, reviewed = release_inputs(release, work, repo)
    plan["packages"] = [PACKAGE]
    write(repo, "alpha.md", GOOD, PACKAGE)
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match=f"Unfolded.*{PACKAGE}/alpha.md.*--package"):
        release.select_artifacts(plan, validated, reviewed)
    assert not reviewed.exists()


def test_a_coordinated_release_also_refuses_a_package_fragment(
    tools, repo, tmp_path_factory, monkeypatch
):
    _, release = tools
    work = tmp_path_factory.mktemp("release")
    plan, validated, reviewed = release_inputs(release, work, repo)
    write(repo, "alpha.md", GOOD, PACKAGE)
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match=f"Unfolded.*{PACKAGE}/alpha.md"):
        release.select_artifacts(plan, validated, reviewed)


@pytest.mark.parametrize(
    ("package", "fragment_of", "refused"),
    [
        ("all", None, True),
        ("all", PACKAGE, True),
        (PACKAGE, PACKAGE, True),
        (PACKAGE, None, False),
        (PACKAGE, "sandloom-s3", False),
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_the_plan_refuses_unfolded_fragments_even_for_a_dry_run(
    tools, repo, package, fragment_of, refused, dry_run
):
    _, release = tools
    for name in release.PACKAGES:
        folder = repo / "packages" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pyproject.toml").write_text('[project]\nversion = "0.7.0a14"\n')
    (repo / "packages" / "sandloom-s3" / "CHANGELOG.md").write_text(PACKAGE_CHANGELOG)
    write(repo, "alpha.md", GOOD, fragment_of)

    def plan():
        return release.plan_release(
            repo,
            package,
            "workflow_dispatch",
            "refs/heads/main",
            dry_run=dry_run,
            on_main=True,
            on_pypi=lambda *_: True,
        )

    if refused:
        with pytest.raises(ValueError, match="Unfolded changelog fragments"):
            plan()
    else:
        assert plan()["packages"] == [package]


def test_fold_of_a_package_writes_its_own_changelog_and_only_its_fragments(tools, repo):
    fragments, release = tools
    write(repo, "alpha.md", GOOD, PACKAGE)
    write(repo, "root.md", GOOD)
    fragments.fold(repo, "0.7.0a14", "2026-11-20", PACKAGE)
    changelog = repo / "packages" / PACKAGE / "CHANGELOG.md"
    # The package changelog had no Unreleased heading: it gets one, empty.
    assert changelog.read_text().startswith(
        "# Changelog — sandloom-redis\n\n- Not a release heading.\n\n"
        "## [Unreleased]\n\n## [0.7.0a14] — 2026-11-20\n\n### Added\n"
    )
    assert release.changelog_section(changelog, "0.7.0a14").endswith("- Three.\n")
    assert (
        release.changelog_section(changelog, "0.7.0a13") == "- Older package notes.\n"
    )
    assert not (repo / "changelog.d" / PACKAGE / "alpha.md").exists()
    assert (repo / "changelog.d" / "root.md").exists()
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG


def test_fold_of_a_package_checks_the_version_of_that_package(tools, repo):
    fragments, _ = tools
    (repo / "packages" / PACKAGE / "pyproject.toml").write_text(
        '[project]\nversion = "0.7.0a13"\n'
    )
    write(repo, "alpha.md", GOOD, PACKAGE)
    with pytest.raises(ValueError, match="is not the package version 0.7.0a13"):
        fragments.fold(repo, "0.7.0a14", "2026-11-20", PACKAGE)
    with pytest.raises(ValueError, match="No changelog at"):
        fragments.fold(repo, "0.7.0a14", "2026-11-20", "sandloom-nothing")
    assert (repo / "changelog.d" / PACKAGE / "alpha.md").exists()


@pytest.mark.parametrize("date", ["tomorrow", "2026-13-45", "20261120", "2026-1-2"])
def test_fold_refuses_a_date_that_is_not_an_iso_date(tools, repo, date):
    fragments, _ = tools
    write(repo, "alpha.md", GOOD)
    with pytest.raises(ValueError, match="date|month|day"):
        fragments.fold(repo, "0.7.0a14", date)
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG
    assert (repo / "changelog.d" / "alpha.md").exists()


def test_check_covers_package_directories_and_refuses_unknown_ones(tools, repo):
    fragments, _ = tools
    write(repo, "alpha.md", "- x\n", PACKAGE)
    (repo / "changelog.d" / "not-a-package").mkdir()
    errors = "\n".join(fragments.check(repo))
    assert f"changelog.d/{PACKAGE}/alpha.md:1" in errors
    assert "changelog.d/not-a-package" in errors
    write(repo, "alpha.md", GOOD, PACKAGE)
    (repo / "changelog.d" / "not-a-package").rmdir()
    assert fragments.check(repo) == []


def test_main_folds_a_package_through_the_command_line(tools, repo, capsys):
    fragments, _ = tools
    write(repo, "alpha.md", GOOD, PACKAGE)
    base = ["--root", str(repo), "--date", "2026-11-20"]
    assert fragments.main(["--fold", "0.7.0a15", "--package", PACKAGE, *base]) == 1
    assert "is not the package version" in capsys.readouterr().err
    assert fragments.main(["--fold", "0.7.0a14", "--package", PACKAGE, *base]) == 0
    assert "## [0.7.0a14]" in (repo / "packages" / PACKAGE / "CHANGELOG.md").read_text()
