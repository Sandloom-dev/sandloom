"""``sandloom new``: the wizard at a terminal, the flags everywhere else.

The questions run on fed answers and keys; the setup runner on fake commands
(this interpreter); one test drives the real ``sandloom new`` in a
pseudo-terminal. Design: ``design/cli-wizard/design.md``.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli import project, ui, wizard

PY = sys.executable


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


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


def _ask(
    prompter: wizard.Prompter,
    tmp_path: Path,
    given: wizard.Given | None = None,
    **kw: Any,
) -> wizard.Answers:
    options: dict[str, Any] = {
        "parent": tmp_path,
        "detected": {"codex": "codex"},
        "uv": "/usr/bin/uv",
        "docker_ready": lambda: True,
        "git_email": "you@example.com",
    }
    options.update(kw)
    return wizard.ask(prompter, given or wizard.Given(), **options)


# ----------------------------------------------------------------- the flow


def test_the_questions_in_order(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    keys = [
        ["down", "down", "down", "toggle", "confirm"],  # modules: admin
        ["confirm"],  # agents: the installed one stays ticked
    ]
    names = ["Bad-Name", "json", "taken", "shop"]
    prompter, asked, out = _prompter([*names, "", ""], keys)
    answers = _ask(prompter, tmp_path)
    assert answers == wizard.Answers(
        app="shop",
        batteries=["identity", "admin"],
        agents=["codex"],
        setup=True,
        admin_email="you@example.com",
    )
    text = out.getvalue()
    assert "Scripts and agents: add --yes" in text
    assert text.index("Scripts and agents") < text.index("App name")
    assert asked[0] == "App name [my_app]: "
    assert "snake_case" in text
    assert "standard library" in text
    assert "already exists" in text
    assert "admin brings identity" in text
    assert "space to add, Enter to continue" in text
    assert "Set it up now? [Y/n] " in asked
    assert asked[-1].startswith("First administrator's email [you@example.com]")


def test_enter_takes_the_default_name_and_no_module(tmp_path: Path) -> None:
    prompter, _, _ = _prompter(["", "n"], [["confirm"], ["confirm"]])
    answers = _ask(prompter, tmp_path)
    assert (answers.app, answers.batteries, answers.setup) == ("my_app", [], False)
    assert answers.admin_email is None


def test_flags_leave_only_the_rest(tmp_path: Path) -> None:
    prompter, asked, _ = _prompter(["", ""])
    given = wizard.Given(app="shop", batteries=["admin"], agents=["claude"])
    answers = _ask(prompter, tmp_path, given, git_email=None)
    assert asked == [
        "Set it up now? [Y/n] ",
        "First administrator's email (Enter to skip): ",
    ]
    assert answers.setup and answers.admin_email is None
    assert answers.batteries == ["identity", "admin"]


def test_no_setup_question_without_uv(tmp_path: Path) -> None:
    prompter, asked, out = _prompter(["shop"], [["confirm"], ["confirm"]])
    answers = _ask(prompter, tmp_path, uv=None)
    assert not answers.setup
    assert not any("Set it up" in q for q in asked)
    assert "docs.astral.sh/uv" in out.getvalue()


def test_docker_down_offers_the_files_only(tmp_path: Path) -> None:
    checks = iter([False, True])
    prompter, _, out = _prompter(["shop", "", "r"], [["confirm"], ["confirm"]])
    answers = _ask(prompter, tmp_path, docker_ready=lambda: next(checks))
    assert "Docker is starting or not running" in out.getvalue()
    assert answers.setup  # `r` checked again and Docker answered
    prompter, _, _ = _prompter(["shop", "", ""], [["confirm"], ["confirm"]])
    answers = _ask(prompter, tmp_path, docker_ready=lambda: False)
    assert not answers.setup


def test_typed_answers_where_keys_cannot_be_read(tmp_path: Path) -> None:
    """TERM=dumb, --plain or Windows without VT: typed answers, no cursor moves."""
    prompter, _, out = _prompter(["shop", "4, tenancy", "2", "n"])
    answers = _ask(prompter, tmp_path)
    assert answers.batteries == ["identity", "tenancy", "admin"]
    assert answers.agents == ["codex"]
    assert "\x1b[" not in out.getvalue()


def test_ctrl_c_in_a_question_cancels(tmp_path: Path) -> None:
    def read(_prompt: str) -> str:
        raise KeyboardInterrupt

    prompter = wizard.Prompter(out=io.StringIO(), style=ui.PLAIN, read=read)
    with pytest.raises(wizard.Cancelled):
        _ask(prompter, tmp_path)


def test_a_dash_skips_the_administrator(tmp_path: Path) -> None:
    given = wizard.Given(app="shop", batteries=["admin"], agents=["claude"])
    prompter, _, _ = _prompter(["", "-"])
    assert _ask(prompter, tmp_path, given).admin_email is None


# ------------------------------------------------------------------- names


@pytest.mark.parametrize(
    "name,problem",
    [
        ("shop", None),
        ("my_app2", None),
        ("Shop", "snake_case"),
        ("2shop", "snake_case"),
        ("class", "Python keyword"),
        ("json", "standard library"),
        ("email", "standard library"),
        ("string", "standard library"),
    ],
)
def test_app_names(name: str, problem: str | None, tmp_path: Path) -> None:
    found = wizard.name_problem(name, tmp_path)
    assert (found is None) if problem is None else (problem in (found or ""))


def test_the_generator_refuses_a_stdlib_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="standard library"):
        project.generate_project("json", tmp_path)
    assert not (tmp_path / "json").exists()


def test_an_existing_directory_is_named(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    assert "already exists" in (wizard.name_problem("shop", tmp_path) or "")


# --------------------------------------------------------- repeat command


@pytest.mark.parametrize(
    "plan",
    [
        wizard.Plan(app="shop", parent=Path("/w"), agents=["claude"]),
        wizard.Plan(
            app="shop",
            parent=Path("/w"),
            batteries=["identity", "admin"],
            agents=["claude", "codex"],
            setup=True,
        ),
        wizard.Plan(
            app="desk",
            parent=Path("/elsewhere/with space"),
            batteries=["tenancy"],
            agents=["kiro"],
            module="tickets",
            framework_source="path",
            framework_path=Path("/src/sandloom"),
        ),
        wizard.Plan(
            app="desk",
            parent=Path("/w"),
            agents=["claude"],
            framework_req="sandloom[redis,celery]==0.7.0a8",
        ),
    ],
)
def test_the_repeat_command_round_trips(plan: wizard.Plan) -> None:
    argv = wizard.repeat_command("0.7.0a8", plan, cwd=Path("/w"))
    extra = "[standard]" if plan.batteries else ""
    assert argv[:5] == ["uvx", "--from", f"sandloom{extra}==0.7.0a8", "sandloom", "new"]
    args = wizard.parser().parse_args(argv[5:])
    assert args.yes
    assert wizard.plan_from_args(args, cwd=Path("/w")) == plan
    # Never the administrator or a password.
    assert not any("admin-email" in a or "password" in a for a in argv)


def test_the_repeat_command_is_shell_quoted() -> None:
    plan = wizard.Plan(app="desk", parent=Path("/a b"), agents=["claude"])
    line = wizard.repeat_line("0.7.0a8", plan, cwd=Path("/w"))
    assert line.startswith('uvx --from "sandloom==0.7.0a8" sandloom new desk')
    assert "--dir '/a b'" in line
    assert line.endswith(" --yes")


# ------------------------------------------------------------ setup runner


def _runner(tmp_path: Path, **kw: Any) -> tuple[wizard.SetupRunner, io.StringIO]:
    out = io.StringIO()
    options: dict[str, Any] = {
        "out": out,
        "style": ui.Style(symbols=ui.UNICODE),
        "environ": {"PATH": os.environ.get("PATH", ""), "VIRTUAL_ENV": "/venv"},
        "log": tmp_path / ".sandloom/run/new-setup.log",
    }
    options.update(kw)
    return wizard.SetupRunner(**options), out


def _py(label: str, code: str) -> wizard.Step:
    return wizard.Step(label, [PY, "-c", code])


def test_a_failing_step_prints_its_tail_and_how_to_continue(tmp_path: Path) -> None:
    runner, out = _runner(tmp_path)
    steps = [
        _py("uv sync", "print('synced')"),
        _py(
            "uv run sandloom generate",
            "import sys\n"
            "for i in range(30): print(f'line {i}')\n"
            "print('error: the generator broke', file=sys.stderr); sys.exit(3)",
        ),
        _py("uv run sandloom dev up", "print('never')"),
    ]
    result = runner.run(steps, cwd=tmp_path, app_dir="shop")
    text = out.getvalue()
    assert (result.ok, result.failed) == (False, 1)
    assert "✓ uv sync" in text and "✗ uv run sandloom generate" in text
    assert "line 29" in text and "line 5" not in text  # the last 20 lines
    assert "error: the generator broke" in text
    assert (
        "Fix it and continue with: cd shop && uv run sandloom generate"
        " && uv run sandloom dev up"
    ) in text
    assert "never" not in text
    log = (tmp_path / ".sandloom/run/new-setup.log").read_text()
    assert "synced" in log and "line 0" in log
    assert "\x1b" not in log


def test_ctrl_c_stops_the_step_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C while a step runs: the step is asked to stop, no traceback, and
    the remaining commands are printed. The interrupt is raised in the wait
    itself: a real SIGINT reaches the main thread, which under xdist need not
    be the one running the test."""
    runner, out = _runner(tmp_path)
    real_wait = subprocess.Popen.wait
    fired = []

    def wait(self: subprocess.Popen[str], timeout: float | None = None) -> int:
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return real_wait(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    steps = [
        _py("uv sync", "import time\ntime.sleep(30)"),
        _py("uv run sandloom generate", "pass"),
    ]
    started = time.monotonic()
    result = runner.run(steps, cwd=tmp_path, app_dir="shop")
    assert time.monotonic() - started < 20
    assert result.cancelled and not result.ok
    text = out.getvalue()
    assert "Traceback" not in text
    assert "Cancelled" in text
    assert "Continue with: cd shop && uv sync && uv run sandloom generate" in text


def test_ctrl_c_right_after_the_step_starts_prints_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interrupt can land between spawning the step and waiting for it
    (a busy machine widens that window): still a clean cancel."""
    runner, out = _runner(tmp_path)
    real_start = threading.Thread.start
    fired = []

    def start(self: threading.Thread) -> None:
        real_start(self)
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt

    monkeypatch.setattr(threading.Thread, "start", start)
    steps = [_py("uv sync", "import time; time.sleep(30)")]
    started = time.monotonic()
    result = runner.run(steps, cwd=tmp_path, app_dir="shop")
    assert time.monotonic() - started < 20
    assert result.cancelled and not result.ok
    assert "Traceback" not in out.getvalue()


def test_children_get_a_cleaned_environment(tmp_path: Path) -> None:
    environ = {
        "PATH": "/bin",
        "VIRTUAL_ENV": "/venv",
        "UV_PROJECT_ENVIRONMENT": "/x",
        "UV_PROJECT": "/y",
        "HOME": "/home/me",
    }
    assert wizard.child_env(environ) == {"PATH": "/bin", "HOME": "/home/me"}
    runner, _ = _runner(tmp_path, environ={**environ, "PATH": os.environ["PATH"]})
    step = _py("env", "import os; print('VENV=' + str(os.environ.get('VIRTUAL_ENV')))")
    assert runner.run([step], cwd=tmp_path, app_dir="shop").ok
    assert "VENV=None" in (tmp_path / ".sandloom/run/new-setup.log").read_text()


def test_uv_is_the_uv_variable_else_the_path() -> None:
    def which(name: str) -> str | None:
        return "/usr/bin/uv" if name == "uv" else None

    assert wizard.uv_binary({"UV": "/opt/uv"}, which=which) == "/opt/uv"
    assert wizard.uv_binary({}, which=which) == "/usr/bin/uv"
    assert wizard.uv_binary({}, which=lambda _n: None) is None


class _FakePopen:
    calls: list[dict[str, Any]] = []

    def __init__(self, argv: list[str], **kw: Any) -> None:
        self.argv = argv
        self.stdout = None
        self.stderr = None
        self.returncode = 0
        _FakePopen.calls.append({"argv": argv, **kw})

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0


def test_the_admin_step_is_admin_create_on_the_terminal(tmp_path: Path) -> None:
    steps = wizard.setup_steps("/opt/uv", admin_email="you@example.com", attach=True)
    assert [s.label for s in steps] == [
        "uv sync",
        "uv run sandloom generate",
        "uv run sandloom dev up",
        "uv run sandloom migrate upgrade-all --apply",
        "uv run sandloom admin create --email you@example.com",
    ]
    admin = steps[-1]
    assert admin.attached
    assert admin.argv == [
        "/opt/uv",
        "run",
        "sandloom",
        "admin",
        "create",
        "--email",
        "you@example.com",
    ]
    _FakePopen.calls = []
    runner, _ = _runner(tmp_path, popen=_FakePopen)
    assert runner.run([admin], cwd=tmp_path, app_dir="shop").ok
    (call,) = _FakePopen.calls
    assert call["argv"] == admin.argv
    # Attached: the terminal is the child's, which asks for the password.
    assert call.get("stdin") is None and call.get("stdout") is None
    assert "VIRTUAL_ENV" not in call["env"]
    assert not any("password" in a.lower() for a in call["argv"])
    assert not any("password" in k.lower() for k in call["env"])


def test_without_a_terminal_the_admin_step_prints_a_link() -> None:
    steps = wizard.setup_steps("/opt/uv", admin_email="ops@example.com", attach=False)
    assert steps[-1].argv[-1] == "--invite" and not steps[-1].attached
    assert len(wizard.setup_steps("/opt/uv", admin_email=None, attach=False)) == 4


# --------------------------------------------------------------- git init


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_init_is_skipped_inside_a_work_tree(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert wizard.git_init(fresh, {"PATH": os.environ["PATH"]})
    assert (fresh / ".git").is_dir()
    inner = fresh / "shop"
    inner.mkdir()
    assert not wizard.git_init(inner, {"PATH": os.environ["PATH"]})
    assert not (inner / ".git").exists()
    assert not wizard.git_init(tmp_path / "x", {"PATH": ""})  # git missing


# -------------------------------------------------------------- plain mode


@pytest.fixture
def _no_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project, "write_generated_files", lambda _root: None)


@pytest.mark.usefixtures("_no_boot")
def test_plain_mode_prints_nothing_new_after_next_steps(tmp_path: Path) -> None:
    out, err = _Stream(tty=False), _Stream(tty=False)
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--agents", "claude", "--with", "admin"],
        environ={"TERM": "xterm-256color"},
        stdin=_Stream(tty=False),
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
    root = tmp_path / "shop"
    lines = [
        *project.quickstart("shop", "example", path=str(root)),
        project.ADMIN_NEXT_STEP,
    ]
    expected_tail = "".join(f"  {line}\n" for line in lines)
    head, tail = out.getvalue().split("Next steps:\n", 1)
    assert tail == expected_tail
    assert head.startswith("Created application 'shop' (")
    assert "\x1b" not in out.getvalue()
    assert not (root / ".git").exists()  # plain mode leaves git alone


@pytest.mark.usefixtures("_no_boot")
def test_yes_needs_the_name(tmp_path: Path) -> None:
    err = _Stream(tty=False)
    code = wizard.main(
        ["--yes", "--dir", str(tmp_path)],
        environ={},
        stdin=_Stream(tty=True),
        out=_Stream(tty=True),
        err=err,
    )
    assert code == 2
    assert "app" in err.getvalue() and "sandloom new <app>" in err.getvalue()


@pytest.mark.usefixtures("_no_boot")
def test_an_agent_at_a_terminal_gets_no_question(tmp_path: Path) -> None:
    out, err = _Stream(tty=True), _Stream(tty=True)
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--agents", "claude"],
        environ={"CLAUDECODE": "1"},
        stdin=_Stream(tty=True),
        out=out,
        err=err,
    )
    assert code == 0, err.getvalue()
    assert "Next steps:" in out.getvalue()
    assert "Set it up now" not in out.getvalue()


@pytest.mark.usefixtures("_no_boot")
def test_setup_refuses_in_ci(tmp_path: Path) -> None:
    err = _Stream(tty=False)
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--yes", "--setup"],
        environ={"CI": "true"},
        stdin=_Stream(tty=False),
        out=_Stream(tty=False),
        err=err,
    )
    assert code == 1
    assert "CI provides its own services" in err.getvalue()
    assert not (tmp_path / "shop").exists()


@pytest.mark.usefixtures("_no_boot")
def test_admin_email_needs_the_console(tmp_path: Path) -> None:
    err = _Stream(tty=False)
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--yes", "--admin-email", "a@b.c"],
        environ={},
        stdin=_Stream(tty=False),
        out=_Stream(tty=False),
        err=err,
    )
    assert code == 2 and "--with admin" in err.getvalue()


@pytest.mark.usefixtures("_no_boot")
def test_an_existing_directory_names_dir(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    err = _Stream(tty=False)
    code = wizard.main(
        ["shop", "--dir", str(tmp_path), "--agents", "claude"],
        environ={},
        stdin=_Stream(tty=False),
        out=_Stream(tty=False),
        err=err,
    )
    assert code == 1 and "--dir" in err.getvalue()


# ------------------------------------------------------- pseudo-terminal


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if ui.agent_marker({k: v}) is None
        and k not in {"CI", "NO_COLOR", "FORCE_COLOR", "VIRTUAL_ENV"}
    }
    env.update({"TERM": "xterm-256color", "COLUMNS": "100", "LANG": "en_US.UTF-8"})
    return env


@pytest.mark.slow
@pytest.mark.subprocess_probe
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_the_wizard_in_a_pseudo_terminal(tmp_path: Path) -> None:
    """``sandloom new`` answered by keys: name, modules (identity), agents
    (claude only), no setup. The files exist and the summary carries the
    repeat command."""
    import fcntl
    import pty
    import select
    import struct
    import termios

    script = Path(PY).parent / "sandloom"
    if not script.exists():  # pragma: no cover - not an installed env
        pytest.skip("the sandloom console script is not installed")
    fd, child_fd = pty.openpty()
    # 100 columns: wide enough for the mark.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    child = subprocess.Popen(
        [str(script), "new"],
        cwd=tmp_path,
        env=_clean_env(),
        stdin=child_fd,
        stdout=child_fd,
        stderr=child_fd,
        start_new_session=True,
    )
    os.close(child_fd)
    output = b""

    def pump(until: bytes, start: int = 0, timeout: float = 60) -> None:
        nonlocal output
        end = time.monotonic() + timeout
        while until not in output[start:] and time.monotonic() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    output += os.read(fd, 65536)
                except OSError:
                    return

    def send(data: bytes, until: bytes) -> None:
        start = len(output)
        os.write(fd, data)
        pump(until, start)

    try:
        pump(b"App name")
        send(b"shop\r", b"Enter to continue")
        send(b" \r", b"Esc cancel")  # identity; then the agents list
        # Claude Code (the first row) only, whatever this machine has installed.
        frame = output.rsplit(b"Coding agents", 1)[-1]
        rows = [line for line in frame.split(b"\n") if b"] " in line][:9]
        keys = b"".join(
            (b" " if (b"[x]" in row) != (i == 0) else b"") + b"j"
            for i, row in enumerate(rows)
        )
        send(keys + b"\r", b"[Y/n]")
        os.write(fd, b"n\r")
        deadline = time.monotonic() + 180
        while child.poll() is None and time.monotonic() < deadline:
            pump(b"\0", len(output), 0.5)  # keep reading: a full terminal blocks
        pump(b"\0", len(output), 0.5)  # what is left in the terminal
        status = child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(fd)
    text = output.decode(errors="replace")
    assert status == 0, text
    assert ui.MARK[3] in text  # the caracal beside the name
    assert "Scripts and agents: add --yes" in text
    assert "sandloom new shop --with identity --agents claude --yes" in text
    assert "Traceback" not in text
    root = tmp_path / "shop"
    assert (root / "pyproject.toml").is_file()
    assert (root / ".claude/skills").is_dir() and not (root / ".agents").exists()
    assert "sandloom-identity" in (root / "pyproject.toml").read_text()
    assert (root / ".git").is_dir() or subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    ).returncode == 0
