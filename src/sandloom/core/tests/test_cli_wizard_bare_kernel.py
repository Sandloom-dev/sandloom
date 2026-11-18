"""``sandloom new`` on a bare ``pip install sandloom``: no stack package here.

The kernel knows the stacks as a neutral list (a name, what it gives, what it
installs); the lines that name the stack's backing server come from its driver
package. Without the package the wizard writes every file with those lines left
as tokens, then the new app's own environment completes them (``uv sync``,
``sandloom add stack``). The same files on both paths, byte for byte: these tests
on a stand-in stack whose every member is a marker; the real package's output:
``tests/integration/with_batteries/test_scaffold_standard_stack_snapshot.py``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from sandloom.core.cli import add as _add
from sandloom.core.cli import project, stack_files, stack_service, ui, wizard
from sandloom.core.tests._fake_stack import FakeStack, install_fake_stack


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


class _Spawned:
    """A fake child process: the argvs it was asked to run, each exiting 0
    unless its label is in ``fail``."""

    calls: list[dict[str, Any]] = []
    fail: set[str] = set()

    def __init__(self, argv: list[str], **kw: Any) -> None:
        self.argv = argv
        self.stdout = None
        self.stderr = None
        self.returncode = 1 if " ".join(argv[1:]) in self.fail else 0
        self.calls.append({"argv": argv, **kw})

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode


@pytest.fixture
def bare(monkeypatch: pytest.MonkeyPatch) -> None:
    """No package describes the standard stack in this environment."""

    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(f"no package for {driver}")

    monkeypatch.setattr(stack_service, "_discover", missing)
    _Spawned.calls, _Spawned.fail = [], set()
    monkeypatch.setattr(
        wizard,
        "_runner",
        lambda run, root: wizard.SetupRunner(
            out=run.out,
            style=ui.Style(symbols=ui.UNICODE),
            environ={},
            log=root / wizard.SETUP_LOG,
            popen=_Spawned,
        ),
    )


def _new(
    tmp_path: Path, *argv: str, uv: str | None = "/opt/uv"
) -> tuple[int, str, str]:
    """``sandloom new shop --yes …`` with ``uv`` as the machine's uv (``None``:
    not installed): the exit status, the output, the error output."""
    out, err = _Stream(), _Stream()
    with mock.patch.object(wizard, "uv_binary", lambda environ, which=None: uv):
        code = wizard.main(
            ["shop", "--dir", str(tmp_path), "--yes", "--agents", "claude", *argv],
            environ={},
            stdin=_Stream(),
            out=out,
            err=err,
        )
    return code, out.getvalue(), err.getvalue()


def _labels() -> list[str]:
    return [" ".join(call["argv"][1:]) for call in _Spawned.calls]


@pytest.mark.usefixtures("bare")
def test_without_uv_the_files_are_written_and_the_commands_are_named(
    tmp_path: Path,
) -> None:
    code, out, err = _new(tmp_path, "--no-git", uv=None)
    assert code == 1  # a script sees the app is not complete
    assert "Traceback" not in out + err
    assert err == ""
    root = tmp_path / "shop"
    assert stack_files.pending(root) == list(stack_files.FILES)
    assert "@@STACK:infra_test@@" in (root / ".env.test").read_text()
    # One line names the commands to run later, and the next steps carry the
    # stack step right after `uv sync`.
    assert "uv run sandloom add stack" in out.split("Next steps:")[0]
    steps = out.split("Next steps:")[1]
    sync = steps.index("uv sync")
    assert steps.index("uv run sandloom add stack") > sync
    assert steps.index("uv run sandloom add stack") < steps.index(
        "uv run sandloom generate"
    )
    assert _Spawned.calls == []  # nothing ran


@pytest.mark.usefixtures("bare")
def test_the_app_requires_the_extras_of_the_stack(tmp_path: Path) -> None:
    _new(tmp_path, "--no-git", uv=None)
    pyproject = (tmp_path / "shop" / "pyproject.toml").read_text()
    assert '"sandloom[postgres,redis,celery]' in pyproject


@pytest.mark.usefixtures("bare")
def test_uv_completes_the_app_in_its_own_environment(tmp_path: Path) -> None:
    code, out, err = _new(tmp_path, "--no-git")
    assert code == 0, out + err
    assert _labels() == [
        "sync",
        "run sandloom add stack",
        "run sandloom generate",
    ]
    assert all(call["cwd"] == tmp_path / "shop" for call in _Spawned.calls)
    assert "Next steps:" in out


@pytest.mark.usefixtures("bare")
@pytest.mark.parametrize("failing", ["sync", "run sandloom add stack"])
def test_a_failing_step_names_the_commands_to_run_later(
    failing: str, tmp_path: Path
) -> None:
    """No network, or no package index: one line names the rest to run, never a
    traceback, and the status says the app is not complete."""
    _Spawned.fail = {failing}
    code, out, err = _new(tmp_path, "--no-git")
    assert code == 1
    assert "Traceback" not in out + err
    assert "Fix it and continue with:" in out
    assert f"cd {tmp_path / 'shop'}" in out
    assert "uv run sandloom add stack" in out
    assert (tmp_path / "shop" / "pyproject.toml").is_file()


@pytest.mark.usefixtures("bare")
def test_the_setup_writes_the_stack_files_right_after_uv_sync(
    tmp_path: Path,
) -> None:
    code, out, err = _new(tmp_path, "--no-git", "--setup", "--services", "docker")
    assert code == 0, out + err
    assert _labels() == [
        "sync",
        "run sandloom add stack",
        "run sandloom generate",
        "run sandloom dev up",
        "run sandloom migrate upgrade-all --apply",
    ]


@pytest.mark.usefixtures("bare")
def test_servers_you_run_are_refused_without_the_package(tmp_path: Path) -> None:
    code, out, err = _new(tmp_path, "--services", "external", "--no-git")
    assert code == 2
    assert "dev up --services external" in err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in out + err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("bare")
def test_the_question_does_not_offer_servers_it_cannot_check() -> None:
    said: list[str] = []
    answers = iter(["2", "3"])

    def read(prompt: str) -> str:
        said.append(prompt)
        return next(answers)

    out = io.StringIO()
    prompter = wizard.Prompter(out=out, style=ui.Style(symbols=ui.UNICODE), read=read)
    machine = wizard.Machine(
        uv="/opt/uv", docker=lambda: wizard.DOCKER_STOPPED, stack_here=False
    )
    assert wizard.ask_services(prompter, machine) == (wizard.LATER, None, None)
    assert len(said) == 2  # "2" asked again, "3" taken
    said_all = " ".join(out.getvalue().split())
    assert "dev up --services external" in said_all
    assert wizard.NEUTRAL_LABEL in said_all


def test_a_neutral_label_stands_in_until_the_package_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_stack(monkeypatch).label = "Valkey"
    assert wizard._label() == "Valkey"

    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(driver)

    monkeypatch.setattr(stack_service, "_discover", missing)
    assert wizard._label() == wizard.NEUTRAL_LABEL


# ------------------------------------------------- the same bytes on both paths


def _marked(monkeypatch: pytest.MonkeyPatch) -> FakeStack:
    fake = install_fake_stack(monkeypatch)
    fake.name = "kvstore"
    fake.prod_compose = "  kvstore:\n    image: PROD-MARK"
    fake.prod_volume = "kv_vol"
    fake.ci_service = "      kvstore:\n        image: CI-MARK"
    return fake


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".env"  # the one random secret
    }


def test_the_completed_bare_app_is_the_app_the_package_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _marked(monkeypatch)
    here = tmp_path / "here"
    here.mkdir()
    project.generate_project("shop", here, with_batteries=["identity"])

    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(driver)

    monkeypatch.setattr(stack_service, "_discover", missing)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project.generate_project("shop", elsewhere, with_batteries=["identity"])
    root = elsewhere / "shop"
    assert stack_files.pending(root)
    monkeypatch.setattr(stack_service, "_discover", lambda driver: fake)
    assert _add.main(["--root", str(root), "stack"]) == 0
    assert stack_files.pending(root) == []
    assert _tree(root) == _tree(here / "shop")
    # The markers of the stack's package are in the files it completed.
    assert "PROD-MARK" in (root / "deploy" / "docker-compose.yml").read_text()


def test_every_file_with_a_token_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FILES`` is what ``add stack`` completes: a template that takes a stack
    value in a file it does not list would stay unfinished."""

    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(driver)

    monkeypatch.setattr(stack_service, "_discover", missing)
    project.generate_project("shop", tmp_path, with_batteries=["identity", "admin"])
    root = tmp_path / "shop"
    found = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and ".git/" not in path.as_posix()
        and "@@STACK:" in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert found == sorted(stack_files.FILES)


def test_every_key_is_supplied_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_stack(monkeypatch)
    assert set(stack_files.values(fake)) == set(stack_files.KEYS)
    with pytest.raises(KeyError):
        stack_files.token("not_a_key")
    with pytest.raises(stack_service.StackServiceError, match="unknown stack token"):
        stack_files.fill("@@STACK:nope@@", {})


def test_add_stack_without_the_package_says_so_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(f"no package for {driver}")

    monkeypatch.setattr(stack_service, "_discover", missing)
    project.generate_project("shop", tmp_path)
    root = tmp_path / "shop"
    before = _tree(root)
    assert _add.main(["--root", str(root), "stack"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("sandloom add stack: no package for redis")
    assert len(captured.err.strip().splitlines()) == 1
    assert _tree(root) == before


def test_add_stack_on_a_complete_app_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_stack(monkeypatch)
    project.generate_project("shop", tmp_path)
    root = tmp_path / "shop"
    before = _tree(root)
    assert _add.main(["--root", str(root), "stack"]) == 0
    assert "already written" in capsys.readouterr().out
    assert _tree(root) == before


def test_write_generated_files_waits_for_the_stack_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(driver: str) -> Any:
        raise stack_service.StackNotInstalled(driver)

    monkeypatch.setattr(stack_service, "_discover", missing)
    project.generate_project("shop", tmp_path)
    problem = project.write_generated_files(tmp_path / "shop")
    assert problem is not None and "add stack" in problem


# ------------------------------------------------------------- the stack list


def test_a_stack_other_than_the_known_ones_is_refused(tmp_path: Path) -> None:
    with pytest.raises(stack_service.StackServiceError, match="unknown stack 'lean'"):
        project.generate_project("shop", tmp_path, stack="lean")
    assert list(tmp_path.iterdir()) == []


def test_the_kernel_lists_stacks_without_any_server_text() -> None:
    (standard,) = stack_service.STACKS
    assert standard.name == stack_service.DEFAULT_STACK == "standard"
    assert standard.extras == ("redis", "celery")
    assert json.dumps(standard.summary).count("image") == 0


def _two_stacks(monkeypatch: pytest.MonkeyPatch) -> None:
    lean = stack_service.StackChoice("lean", "no servers", ("celery",), "memory")
    monkeypatch.setattr(stack_service, "STACKS", (*stack_service.STACKS, lean))


def test_a_second_stack_is_asked_and_goes_into_the_repeat_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_stacks(monkeypatch)
    out = io.StringIO()
    answers = iter(["9", "2"])
    prompter = wizard.Prompter(
        out=out, style=ui.Style(symbols=ui.UNICODE), read=lambda _p: next(answers)
    )
    assert wizard.ask_stack(prompter, stack_service.STACKS) == "lean"
    assert "standard" in out.getvalue() and "no servers" in out.getvalue()
    args = wizard.parser().parse_args(["shop", "--stack", "lean", "--yes"])
    plan = wizard.plan_from_args(args, cwd=Path("/w"))
    assert plan.stack == "lean"
    argv = wizard.repeat_command("0.7.0a8", plan, cwd=Path("/w"))
    assert argv[argv.index("--stack") + 1] == "lean"
    standard = wizard.Plan(app="shop", parent=Path("/w"))
    assert "--stack" not in wizard.repeat_command("0.7.0a8", standard, cwd=Path("/w"))


def test_one_stack_is_told_not_asked() -> None:
    out = io.StringIO()

    def never(_prompt: str) -> str:
        raise AssertionError("one stack is no question")

    prompter = wizard.Prompter(out=out, style=ui.Style(symbols=ui.UNICODE), read=never)
    assert wizard.ask_stack(prompter) == "standard"
    assert "PostgreSQL and Redis" in out.getvalue()
