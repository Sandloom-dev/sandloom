"""``sandloom new``, round two: what a first-time user meets.

The welcome, plain-language questions in their order, PostgreSQL and Redis
without Docker, a git repository by default (and never a commit into
someone else's), the administrator last with ``admin create`` on the
terminal (the wizard never reads the password), and the closing summary.
Design: ``design/cli-wizard/design.md`` (round two).
"""

from __future__ import annotations

import getpass
import io
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import project, ui, wizard

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="no git")


def _prompter(
    answers: Iterable[str], keys: Iterable[list[str]] | None = None
) -> tuple[wizard.Prompter, list[str], io.StringIO]:
    feed = iter(answers)
    asked: list[str] = []
    out = io.StringIO()

    def read(prompt: str) -> str:
        asked.append(prompt)
        out.write(prompt)
        return next(feed)

    key_lists: Iterator[list[str]] | None = iter(keys) if keys is not None else None
    return (
        wizard.Prompter(
            out=out,
            style=ui.Style(symbols=ui.UNICODE),
            read=read,
            keys=(lambda: next(key_lists)) if key_lists is not None else None,
        ),
        asked,
        out,
    )


def _machine(**kw: Any) -> wizard.Machine:
    options: dict[str, Any] = {
        "uv": "/usr/bin/uv",
        "docker": lambda: "ready",
        "git_email": "owner@example.com",
        "local_services": lambda: (None, None),
        "check_services": lambda db, redis: None,
    }
    options.update(kw)
    return wizard.Machine(**options)


def _ask(
    prompter: wizard.Prompter,
    tmp_path: Path,
    given: wizard.Given | None = None,
    **kw: Any,
) -> wizard.Answers:
    return wizard.ask(
        prompter,
        given or wizard.Given(),
        parent=tmp_path,
        detected={"codex": "codex"},
        machine=_machine(**kw),
    )


ADMIN = wizard.Given(app="cool_app", batteries=["admin"], agents=["claude"])


def _run(tmp_path: Path, **kw: Any) -> wizard._Run:
    stream = io.StringIO()
    options: dict[str, Any] = {
        "args": wizard.parser().parse_args(["cool_app"]),
        "environ": {},
        "stdin": stream,
        "out": stream,
        "err": stream,
        "version": "1.0.0",
        "asking": True,
        "style": ui.PLAIN,
    }
    options.update(kw)
    return wizard._Run(**options)


# ----------------------------------------------------------------- welcome


def test_the_header_welcomes_with_the_site_and_what_happens() -> None:
    out = io.StringIO()
    style = ui.Style(symbols=ui.UNICODE, utf8=True)
    wizard.welcome(out, style, "1.0.0", width=100, height=40)
    text = out.getvalue()
    assert ui.MARK[3] in text
    assert "https://sandloom.dev" in text
    assert "step by step" in text and "new Sandloom application" in text
    # Under the caracal and the name, in that order.
    assert text.index("sandloom") < text.index("https://sandloom.dev")
    assert text.index("https://sandloom.dev") < text.index("step by step")
    narrow = io.StringIO()
    wizard.welcome(narrow, ui.PLAIN, "1.0.0", width=50, height=20)
    assert "https://sandloom.dev" in narrow.getvalue()
    assert "step by step" in narrow.getvalue()


def test_the_first_question_says_what_it_asks(tmp_path: Path) -> None:
    prompter, asked, _ = _prompter(["", "3"], keys=[["confirm"], ["confirm"]])
    _ask(prompter, tmp_path)
    assert asked[0] == "What are you building? Name your app [my_app]: "


# ------------------------------------------------------- the order, the words


def test_the_questions_in_order_and_no_password(tmp_path: Path) -> None:
    prompter, asked, out = _prompter(["", ""])  # services (Docker), setup
    answers = _ask(prompter, tmp_path, ADMIN)
    assert answers.setup and answers.services == "docker" and answers.git
    assert answers.admin_email is None  # asked after the setup, not here
    assert asked == [
        "How do you want to run them? [1]: ",
        "Install the dependencies, start PostgreSQL and Redis in Docker, create "
        "the tables and your administrator now? (about a minute) [Y/n] ",
    ]
    text = out.getvalue()
    assert "Docker — Sandloom starts them for you" in text
    assert "I already run PostgreSQL and Redis" in text
    assert "Later — just create the files" in text
    assert "git" not in " ".join(asked).lower()  # a repository is the default


def test_the_wizard_never_reads_a_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The administrator comes last: a heading, the email, then `admin
    create` attached to the terminal, which asks for the password itself.
    Nothing is piped into it and the wizard never calls getpass."""

    def no_getpass(*_a: Any, **_k: Any) -> str:
        raise AssertionError("the wizard read a password")

    monkeypatch.setattr(getpass, "getpass", no_getpass)
    assert not hasattr(wizard, "getpass")
    calls: list[dict[str, Any]] = []

    class Recorder:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            calls.append({"argv": argv, **kw})
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    prompter, asked, out = _prompter(["", ""])  # the git address, confirmed
    run = _run(tmp_path, environ={"UV": "/opt/uv"}, out=out)
    run.prompter, run.git_email = prompter, "owner@example.com"
    created = wizard.create_administrator(
        run, tmp_path, "cool_app", None, popen=Recorder
    )
    assert created == "owner@example.com"
    text = out.getvalue()
    assert text.index("Now create the console administrator of cool_app") < (
        text.index("Email of your console administrator")
    )
    (call,) = calls
    assert call["argv"] == [
        "/opt/uv",
        "run",
        "sandloom",
        "admin",
        "create",
        "--email",
        "owner@example.com",
        "--quiet",
    ]
    # Attached: the terminal is the child's; no pipe, no password flag.
    assert call.get("stdin") is None and call.get("stdout") is None
    assert not any("password" in part for part in call["argv"])
    assert not any("password" in q.lower() for q in asked)


def test_the_administrator_can_be_skipped(tmp_path: Path) -> None:
    prompter, _, out = _prompter(["-"])
    run = _run(tmp_path, environ={"UV": "/opt/uv"}, out=out)
    run.prompter, run.git_email = prompter, "owner@example.com"

    def popen(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("admin create ran")

    assert wizard.create_administrator(run, tmp_path, "x", None, popen=popen) is None
    assert "the summary says how to create one" in out.getvalue()


def test_without_a_terminal_the_administrator_gets_a_link() -> None:
    steps = wizard.setup_steps("/opt/uv", admin_email="ops@example.com", attach=False)
    assert steps[-1].argv[-1] == "--invite"
    attached = wizard.setup_steps("/opt/uv", admin_email="ops@example.com", attach=True)
    assert attached[-1].attached and "--password-stdin" not in attached[-1].argv


# --------------------------------------------------------------------- git


def _git_env(
    tmp_path: Path, *, identity: bool = True, extra: str = ""
) -> dict[str, str]:
    config = tmp_path / "gitconfig"
    config.write_text(
        ("[user]\n  name = Owner\n  email = owner@example.com\n" if identity else "")
        + "[init]\n  defaultBranch = main\n"
        + extra
    )
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout


def _app(parent: Path) -> Path:
    root = parent / "shop"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    return root


@needs_git
def test_a_repository_with_a_first_commit_by_default(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    root = _app(tmp_path / "work")
    run = _run(tmp_path, environ=env)
    answers = wizard.Answers("shop", [], ["claude"], False)
    assert answers.git  # the default
    assert wizard._start_git(run, root, answers)
    assert wizard.initial_commit(root, env) is None
    assert _git(root, env, "log", "--format=%s").strip() == wizard.INITIAL_COMMIT


@needs_git
def test_inside_a_work_tree_nothing_is_nested_or_committed(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    outer = tmp_path / "monorepo"
    outer.mkdir()
    _git(outer, env, "init", "-q")
    (outer / "README.md").write_text("mine\n")
    _git(outer, env, "add", "-A")
    _git(outer, env, "commit", "-q", "-m", "theirs")
    root = _app(outer / "apps")
    run = _run(tmp_path, environ=env)
    answers = wizard.Answers("shop", [], ["claude"], False)
    assert not wizard._start_git(run, root, answers)
    assert not (root / ".git").exists()
    assert _git(outer, env, "log", "--format=%s").split() == ["theirs"]
    assert _git(outer, env, "diff", "--cached", "--name-only") == ""  # not staged
    said = run.out.getvalue()
    assert f"inside the git repository at {outer.resolve()}" in said
    assert "the files are added there, not committed" in said
    assert "home directory" not in said


@needs_git
def test_a_home_directory_under_git_is_named(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _git(home, env, "init", "-q")
    root = _app(home / "code")
    run = _run(tmp_path, environ=env)
    assert not wizard._start_git(run, root, wizard.Answers("shop", [], [], False))
    assert "(your home directory is a git repository)" in run.out.getvalue()


def test_without_git_installed_there_is_one_hint(tmp_path: Path) -> None:
    root = _app(tmp_path)
    run = _run(tmp_path, environ={"PATH": str(tmp_path / "empty")})
    assert not wizard._start_git(run, root, wizard.Answers("shop", [], [], False))
    assert "git is not installed" in run.out.getvalue()


@needs_git
def test_no_git_identity_keeps_the_repository_and_says_how(tmp_path: Path) -> None:
    env = _git_env(tmp_path, identity=False)
    root = _app(tmp_path / "work")
    run = _run(tmp_path, environ=env)
    assert wizard._start_git(run, root, wizard.Answers("shop", [], [], False))
    hint = wizard.initial_commit(root, env)
    assert hint is not None
    assert 'git -c user.name="Your Name" -c user.email=you@example.com commit' in hint
    assert "git config --global user.name" in hint
    assert (root / ".git").is_dir()
    log = subprocess.run(["git", "log"], cwd=root, env=env, capture_output=True)
    assert log.returncode != 0  # no commit


@needs_git
def test_the_first_commit_neither_signs_nor_runs_hooks(tmp_path: Path) -> None:
    """commit.gpgsign=true with no usable key and a failing global hook:
    the one automated commit still lands; the settings stay for later."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    env = _git_env(
        tmp_path,
        extra=f"[commit]\n  gpgsign = true\n[user]\n  signingkey = NOPE\n"
        f"[core]\n  hooksPath = {hooks}\n",
    )
    root = _app(tmp_path / "work")
    assert wizard.git_init(root, env)
    assert wizard.initial_commit(root, env) is None
    assert _git(root, env, "config", "commit.gpgsign").strip() == "true"


@needs_git
def test_a_failing_commit_keeps_the_repository_and_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _git_env(tmp_path)
    root = _app(tmp_path / "work")
    assert wizard.git_init(root, env)
    real = wizard._run_quiet

    def failing(argv: list[str], environ: Any, **kw: Any) -> Any:
        if "commit" in argv:
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: cannot lock ref 'HEAD'\n"
            )
        return real(argv, environ, **kw)

    monkeypatch.setattr(wizard, "_run_quiet", failing)
    hint = wizard.initial_commit(root, env)
    assert hint is not None and "\n" not in hint
    assert "cannot lock ref" in hint
    assert 'git add -A && git commit -m "Initial commit from sandloom new"' in hint
    assert (root / ".git").is_dir()


@pytest.mark.usefixtures("_no_boot")
def test_a_target_folder_with_a_repository_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "shop" / ".git").mkdir(parents=True)
    err = io.StringIO()
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--agents", "claude", "--yes"],
        environ={},
        out=io.StringIO(),
        err=err,
    )
    assert code == 1 and str(tmp_path / "shop") in err.getvalue()


@pytest.mark.usefixtures("_no_boot")
def test_yes_creates_a_repository_unless_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(wizard, "git_toplevel", lambda directory, env: None)
    monkeypatch.setattr(
        wizard, "git_init", lambda root, env: calls.append(root) or True
    )
    monkeypatch.setattr(wizard, "initial_commit", lambda root, env: None)
    out = io.StringIO()
    base = ["shop", "--dir", str(tmp_path), "--agents", "claude", "--yes"]
    env = {"PATH": os.environ["PATH"]}
    assert wizard.main(base, environ=env, out=out, err=io.StringIO()) == 0
    assert calls == [tmp_path / "shop"]
    head, _ = out.getvalue().split("Next steps:", 1)
    assert "git repository with a first commit" in head  # before the block CI parses
    other = ["desk", "--dir", str(tmp_path), "--agents", "claude", "--yes", "--no-git"]
    assert wizard.main(other, environ=env, out=io.StringIO(), err=io.StringIO()) == 0
    assert calls == [tmp_path / "shop"]


def test_the_repeat_command_carries_no_git_only_when_used() -> None:
    default = wizard.Plan(app="desk", parent=Path("/w"), agents=["claude"])
    argv = wizard.repeat_command("1.0.0", default, cwd=Path("/w"))
    assert "--git" not in argv and "--no-git" not in argv
    off = wizard.Plan(app="desk", parent=Path("/w"), agents=["claude"], git=False)
    argv = wizard.repeat_command("1.0.0", off, cwd=Path("/w"))
    assert "--no-git" in argv
    args = wizard.parser().parse_args(argv[5:])
    assert wizard.plan_from_args(args, cwd=Path("/w")) == off


def test_the_repeat_command_carries_external_services() -> None:
    plan = wizard.Plan(
        app="desk",
        parent=Path("/w"),
        agents=["claude"],
        services="external",
        database_url="postgresql://me@127.0.0.1:5432/postgres",
        redis_url="redis://127.0.0.1:6379",
        setup=True,
    )
    argv = wizard.repeat_command("1.0.0", plan, cwd=Path("/w"))
    assert argv[argv.index("--services") + 1] == "external"
    args = wizard.parser().parse_args(argv[5:])
    assert wizard.plan_from_args(args, cwd=Path("/w")) == plan


def test_a_database_password_is_not_printed_in_the_repeat_line() -> None:
    plan = wizard.Plan(
        app="desk",
        parent=Path("/w"),
        agents=["claude"],
        services="external",
        database_url="postgresql://me:s3cret@127.0.0.1:5432/postgres",
        redis_url="redis://127.0.0.1:6379",
    )
    line = wizard.repeat_line("1.0.0", plan, cwd=Path("/w"))
    assert "s3cret" not in line and "me:<password>@127.0.0.1" in line


# ---------------------------------------------------------------- services


def test_docker_missing_is_said_plainly_and_not_the_default(tmp_path: Path) -> None:
    prompter, asked, out = _prompter(["", ""])
    answers = _ask(prompter, tmp_path, ADMIN, docker=lambda: "missing")
    assert "Docker is not installed" in out.getvalue()
    assert asked[0].startswith("How do you want to run them? [3]")
    assert answers.services == "none" and not answers.setup


def test_docker_stopped_defaults_to_the_servers_that_answer(tmp_path: Path) -> None:
    found = ("postgresql://me@127.0.0.1:5432/postgres", "redis://127.0.0.1:6379")
    prompter, asked, out = _prompter(["", "", "", "n"])
    answers = _ask(
        prompter,
        tmp_path,
        ADMIN,
        docker=lambda: "stopped",
        local_services=lambda: found,
    )
    text = out.getvalue()
    assert "Docker is not running" in text
    assert asked[0].startswith("How do you want to run them? [2]")
    assert "PostgreSQL answers at 127.0.0.1:5432" in text
    assert asked[1] == f"PostgreSQL URL [{found[0]}]: "
    assert asked[2] == f"Redis URL [{found[1]}]: "
    assert answers.services == "external"
    assert (answers.database_url, answers.redis_url) == found
    assert not answers.setup  # the last "n"


def test_a_server_that_does_not_answer_is_asked_again(tmp_path: Path) -> None:
    checks = iter(["PostgreSQL at 127.0.0.1:5432: connection refused", None])
    prompter, _, out = _prompter(
        [
            "2",
            "postgresql://me@127.0.0.1:5432/postgres",
            "redis://127.0.0.1:6379",
            "",  # try other URLs? yes
            "postgresql://me@127.0.0.1:5433/postgres",
            "redis://127.0.0.1:6379",
            "n",
        ]
    )
    answers = _ask(
        prompter, tmp_path, ADMIN, check_services=lambda db, redis: next(checks)
    )
    assert "connection refused" in out.getvalue()
    assert answers.database_url == "postgresql://me@127.0.0.1:5433/postgres"


def test_later_creates_the_files_only(tmp_path: Path) -> None:
    prompter, asked, _ = _prompter(["3"])
    answers = _ask(prompter, tmp_path, ADMIN)
    assert answers.services == "none" and not answers.setup
    assert not any("Install the dependencies" in q for q in asked)


def test_the_setup_question_for_own_servers_starts_nothing(tmp_path: Path) -> None:
    found = ("postgresql://me@127.0.0.1:5432/postgres", "redis://127.0.0.1:6379")
    given = wizard.Given(app="shop", batteries=[], agents=["claude"])
    prompter, asked, _ = _prompter(["2", "", "", "n"])
    _ask(prompter, tmp_path, given, local_services=lambda: found)
    assert asked[-1] == (
        "Install the dependencies and create the tables now? (about a minute) [Y/n] "
    )


def test_a_failed_own_server_setup_fails_a_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--yes --setup --services external``: when the servers answer but
    the setup cannot finish (no CREATEDB), the status says so."""
    monkeypatch.setattr(
        wizard, "configure_external", lambda run, root, answers: "role cannot create"
    )
    run = _run(tmp_path, asking=False)
    plan = wizard.Plan(app="cool_app", parent=tmp_path, batteries=[])
    answers = wizard.Answers(
        "cool_app", [], ["claude"], True, git=False, services=wizard.EXTERNAL
    )
    (tmp_path / "cool_app").mkdir()
    assert wizard._finish(run, plan, tmp_path / "cool_app", answers) == 1
    assert "role cannot create" in run.out.getvalue()


def test_own_servers_on_the_usual_ports_need_no_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No URL typed (the usual local ports): the setup still says where."""
    monkeypatch.setattr(wizard, "configure_external", lambda run, root, answers: None)
    run = _run(tmp_path)
    plan = wizard.Plan(app="cool_app", parent=tmp_path, batteries=[])
    answers = wizard.Answers(
        "cool_app", [], ["claude"], True, git=False, services=wizard.EXTERNAL
    )
    kept = wizard._own_servers(run, plan, tmp_path / "cool_app", answers)
    assert kept.setup
    assert "the database cool_app on your PostgreSQL" in run.out.getvalue()


# ------------------------------------------------------------------ summary


def test_the_summary_says_ready_then_how_to_start_then_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wizard, "app_url_of", lambda root, environ: "http://127.0.0.1:8001"
    )
    run = _run(tmp_path)
    plan = wizard.Plan(app="cool_app", parent=tmp_path, batteries=["identity", "admin"])
    answers = wizard.Answers(
        "cool_app", ["identity", "admin"], ["claude"], True, "owner@example.com"
    )
    root = tmp_path / "cool_app"
    text = wizard._summary(run, plan, root, answers, admin_created=True)
    order = [
        "Your app is ready",
        "Start it:",
        "uv run sandloom dev run",
        "Then open",
        "http://127.0.0.1:8001/admin/",
        "Sign in as owner@example.com",
        "Help",
        "uv run sandloom admin list",
        "uv run sandloom admin invite --email owner@example.com",
        "https://sandloom.dev/docs/",
        "For scripts and agents",
        "--yes",
    ]
    positions = [text.index(part) for part in order]
    assert positions == sorted(positions), text
    assert text.count("Sign in as") == 1
    assert "8000" not in text


def test_the_summary_of_files_only_lists_the_steps_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wizard, "app_url_of", lambda root, environ: None)
    monkeypatch.setattr(wizard, "free_app_port", lambda: 8000)
    run = _run(tmp_path)
    plan = wizard.Plan(app="cool_app", parent=tmp_path, batteries=["identity", "admin"])
    answers = wizard.Answers("cool_app", ["identity", "admin"], ["claude"], False)
    text = wizard._summary(run, plan, tmp_path / "cool_app", answers)
    assert "Your app is created" in text
    for line in (
        "uv sync",
        "uv run sandloom migrate upgrade-all --apply",
        "uv run sandloom admin create --email you@example.com",
        "uv run sandloom dev run",
    ):
        assert line in text
    assert text.index("uv sync") < text.index("uv run sandloom dev run")
    assert "Sign in as" not in text


# --------------------------------------------------------------- fixtures


@pytest.fixture
def _no_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project, "write_generated_files", lambda _root: None)


@pytest.mark.parametrize(
    "name", ["demo", "shop", "my_app", "a" * 20, "b" * 40, "c" * 60]
)
def test_the_generated_asgi_passes_the_apps_format_gate(
    name: str, tmp_path: Path
) -> None:
    """The app's own `ruff format --check` must accept `asgi.py` whatever the
    name's length (the quickstart's `demo` once tripped it)."""
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not installed")
    project.generate_project(name, tmp_path, agents=["claude"])
    asgi = tmp_path / name / name / "asgi.py"
    check = subprocess.run(
        [ruff, "format", "--check", "--line-length", "88", str(asgi)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + asgi.read_text()
