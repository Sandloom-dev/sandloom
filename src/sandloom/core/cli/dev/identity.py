"""Where a checkout is: its repository, its state directory, its names.

The dev services belong to a **repository** (``git rev-parse --git-common-dir``,
realpath: the main checkout and every worktree share it), the databases to a
**checkout**. The repository's state (``dev.json``, ``dev.lock``, the CLI's
copy of the compose file) lives in ``<git common dir>/sandloom/``: shared by
every worktree, never committed, never in a synced tree. Outside git it is
the project root's ``.sandloom/``. ``GIT_DIR`` / ``GIT_COMMON_DIR`` (set
inside a git hook) are ignored.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sandloom.core.cli.dev import process as _process
from sandloom.core.testing.machine import canonical, machine_id, short_hash

#: Variables git honours that would point ``rev-parse`` at another repository.
_GIT_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
#: The compose file a project carries (``sandloom new`` writes it).
COMPOSE_FILE = Path("deploy") / "docker-compose.dev.yml"


@dataclass(frozen=True)
class Identity:
    """The names of one checkout's dev services (see the module doc)."""

    repository: Path  # git common dir (realpath), or the project root
    state_dir: Path
    checkout: Path  # this checkout's root (realpath)
    root: Path  # the project inside it (pyproject.toml, .env, deploy/)
    main: bool  # the main checkout (not a worktree)
    app: str  # snake_case app name
    machine: str
    git: bool

    @property
    def slug(self) -> str:
        return (
            re.sub(r"[^a-z0-9-]+", "-", self.app.lower().replace("_", "-")).strip("-")
            or "app"
        )

    @property
    def project(self) -> str:
        """The compose project: ``<app>-<6 hex of the repository>``."""
        return f"{self.slug}-{short_hash(self.machine, canonical(self.repository))}"

    @property
    def checkout_hash(self) -> str:
        return short_hash(self.machine, canonical(self.checkout))

    @property
    def database(self) -> str:
        """This checkout's dev database (``<app>`` in the main checkout)."""
        return self.app if self.main else f"{self.app}_{self.checkout_hash}"

    @property
    def test_database(self) -> str:
        return f"sandloom_test_{self.checkout_hash}"

    @property
    def checkout_state(self) -> Path:
        """``<project>/.sandloom``: this checkout's lock and run files."""
        return self.root / ".sandloom"


def _git(cwd: Path, *args: str, environ: Mapping[str, str]) -> str | None:
    env = {k: v for k, v in environ.items() if k not in _GIT_ENV}
    try:
        proc = _process.run(["git", *args], env=env, cwd=cwd)
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def project_root(cwd: Path) -> Path:
    """The nearest directory holding ``pyproject.toml`` (else ``cwd``)."""
    start = cwd.resolve()
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return start


def app_name(root: Path) -> str:
    """``[project].name`` of ``root``'s pyproject, snake_case."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        name = str(data.get("project", {}).get("name") or "")
    except (OSError, tomllib.TOMLDecodeError):
        name = ""
    name = re.sub(r"[^a-z0-9_]+", "_", (name or root.name).lower()).strip("_")
    if not name or not name[0].isalpha():
        name = f"app_{name}" if name else "app"
    return name


def resolve(cwd: Path, environ: Mapping[str, str]) -> Identity:
    """The identity of the checkout holding ``cwd``."""
    common = _git(
        cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", environ=environ
    )
    top = _git(cwd, "rev-parse", "--show-toplevel", environ=environ)
    machine = machine_id(environ)
    if common and top:
        repository = Path(os.path.realpath(common))
        checkout = Path(os.path.realpath(top))
        # The main checkout holds the common dir as its own `.git`.
        main = repository == Path(os.path.realpath(checkout / ".git"))
        return Identity(
            repository=repository,
            state_dir=repository / "sandloom",
            checkout=checkout,
            root=Path(os.path.realpath(project_root(cwd))),
            main=main,
            app=app_name(project_root(cwd)),
            machine=machine,
            git=True,
        )
    root = Path(os.path.realpath(project_root(cwd)))
    return Identity(
        repository=root,
        state_dir=root / ".sandloom",
        checkout=root,
        root=root,
        main=True,
        app=app_name(root),
        machine=machine,
        git=False,
    )


__all__ = ["COMPOSE_FILE", "Identity", "app_name", "project_root", "resolve"]
