"""App mode of the DX eval (``run_eval.py --mode app``): the agent works the way
a framework user does — in an empty directory, with the framework installed
from wheels built from the ref under test, starting with ``sandloom new``.

This module holds the mechanics; ``run_eval.py`` wires them into ``run``,
``rescore`` and the dry run:

* :func:`build_dist` — the distributions of ``packages/*`` built once
  per ref (``<eval home>/_dist/<sha>/wheels``), a CLI venv with all of them
  installed, and a ``bin/`` holding only the ``sandloom`` entry point (it goes on
  the agent's PATH; ``UV_FIND_LINKS`` points ``uv`` at the wheels, so an
  ``sandloom new`` app's ``uv sync`` resolves the framework from them, not PyPI —
  each benchmark must use its ref-specific builds);
* :func:`app_agent_env` — the agent's environment: PATH without the framework
  repository or any venv, the wheels as find-links;
* :func:`find_app` / :func:`make_pristine` / :func:`app_changes` — locate the
  app the agent generated, regenerate what ``sandloom new`` produced (the diff
  base), and list the agent's changes against it;
* :func:`classify_app_path` — what a changed path outside the task's module
  is (expected wiring, rule, generated, other module, …);
* :func:`run_app_gates` — the app's own ``sandloom test --ci`` (per step) and
  ``sandloom test``, plus the task modules' tests.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def sh(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


# --------------------------------------------------------------------------- #
# Distributions
# --------------------------------------------------------------------------- #


#: Bumped when the build output changes shape: a dist built by an older
#: harness (no unique local version) is rebuilt, never reused.
DIST_FORMAT = 2


def local_version(sha: str) -> str:
    """The PEP 440 local label every wheel of ``sha`` carries (``0.7.0+g<sha>``):
    a build of another commit can never pass for this one, whatever uv cached
    for the bare ``0.7.0``."""
    return f"g{sha[:12]}"


@dataclass
class Dist:
    """The framework as a user gets it, built from one commit."""

    sha: str
    root: Path
    wheels: Path
    bin: Path  # only the `sandloom` entry point: what goes on the agent's PATH
    packages: list[str]

    @property
    def sandloom(self) -> Path:
        return self.bin / "sandloom"

    def versions(self) -> dict[str, str]:
        """Distribution name -> the version of its built wheel."""
        found: dict[str, str] = {}
        for wheel in self.wheels.glob("*.whl"):
            name, version = wheel.name.split("-")[:2]
            found[name.replace("_", "-")] = version
        return found


def build_dist(repo: Path, ref: str, home: Path, log: Any = print) -> Dist:
    """Build every ``packages/*`` wheel of ``ref`` once (shared by every
    task and label on that commit): a clean ``git worktree`` of the commit, so
    uncommitted edits never leak into the measured framework."""
    sha = sh(["git", "rev-parse", f"{ref}^{{commit}}"], repo).stdout.strip()
    if not sha:
        raise RuntimeError(f"unknown ref {ref!r}")
    root = home / "_dist" / sha[:12]
    root.mkdir(parents=True, exist_ok=True)
    ready = root / "READY"
    with (root / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            meta = json.loads(ready.read_text()) if ready.is_file() else {}
            if meta.get("format") != DIST_FORMAT:
                _build(repo, sha, root, log)
                meta = json.loads(ready.read_text())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return Dist(
        sha=sha,
        root=root,
        wheels=root / "wheels",
        bin=root / "bin",
        packages=list(meta["packages"]),
    )


def _build(repo: Path, sha: str, root: Path, log: Any) -> None:
    started = time.monotonic()
    for sub in ("wheels", "cli", "bin"):
        shutil.rmtree(root / sub, ignore_errors=True)
    src = root / "src"
    if src.exists():
        sh(["git", "worktree", "remove", "--force", str(src)], repo)
        shutil.rmtree(src, ignore_errors=True)
    out = sh(["git", "worktree", "add", "--force", "--detach", str(src), sha], repo)
    if out.returncode:
        raise RuntimeError(f"git worktree add failed: {out.stderr}")
    try:
        packages_dir = src / "packages"
        packages = sorted(
            d.name for d in packages_dir.iterdir() if (d / "pyproject.toml").is_file()
        )
        if not packages:
            raise RuntimeError(f"no distributions under {packages_dir}")
        # A unique version per commit (the scratch worktree's copy only):
        # uv's cache can never hand the app another commit's build of 0.7.0.
        for name in packages:
            project = packages_dir / name / "pyproject.toml"
            text, n = re.subn(
                r'(?m)^version = "([^"+]+)"$',
                lambda m: f'version = "{m.group(1)}+{local_version(sha)}"',
                project.read_text(),
                count=1,
            )
            if n != 1:
                raise RuntimeError(f"no static version in {project}")
            project.write_text(text)
        wheels = root / "wheels"
        wheels.mkdir()
        for (
            name
        ) in packages:  # the set tools/packaging/check_package_distributions.py builds
            done = sh(
                ["uv", "build", "--wheel", "-q", "--out-dir", str(wheels), name],
                packages_dir,
                timeout=900,
            )
            if done.returncode:
                raise RuntimeError(f"uv build {name} failed: {done.stderr[-2000:]}")
        built = sorted(wheels.glob("*.whl"))
        if len(built) != len(packages):
            raise RuntimeError(
                f"built {len(built)} wheels for {len(packages)} packages"
            )
        venv = root / "cli"
        done = sh(["uv", "venv", "-q", str(venv)], root, timeout=300)
        if done.returncode:
            raise RuntimeError(f"uv venv failed: {done.stderr[-2000:]}")
        done = sh(
            [
                "uv",
                "pip",
                "install",
                "-q",
                "--python",
                str(venv / "bin" / "python"),
                "--find-links",
                str(wheels),
                *map(str, built),
                "sandloom[check,testing]",
            ],
            root,
            timeout=900,
        )
        if done.returncode:
            raise RuntimeError(f"installing the wheels failed: {done.stderr[-2000:]}")
        (root / "bin").mkdir()
        (root / "bin" / "sandloom").symlink_to(venv / "bin" / "sandloom")
        smoke = sh([str(root / "bin" / "sandloom"), "new", "--help"], root, timeout=120)
        if smoke.returncode:
            raise RuntimeError(f"`sandloom new --help` failed: {smoke.stderr[-2000:]}")
    finally:
        sh(["git", "worktree", "remove", "--force", str(src)], repo)
        shutil.rmtree(src, ignore_errors=True)
    (root / "READY").write_text(
        json.dumps(
            {
                "format": DIST_FORMAT,
                "sha": sha,
                "packages": packages,
                "seconds": round(time.monotonic() - started),
            },
            indent=2,
        )
        + "\n"
    )
    log(f"dist: built {len(packages)} wheels of {sha[:12]} in {root}")


def app_agent_env(base: dict[str, str], dist: Dist, repo: Path) -> dict[str, str]:
    """``base`` (the task's resources) for an app-mode agent: the installed
    ``sandloom`` first on PATH, no path into the framework repository or any venv
    (the runner itself runs under the backend's ``uv run``), and the wheels as
    ``uv``'s find-links."""
    env = {
        k: v
        for k, v in base.items()
        if k not in {"PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"}
    }
    keep = [
        p
        for p in base.get("PATH", "").split(os.pathsep)
        if p
        and not Path(p).resolve().is_relative_to(repo.resolve())
        and not (Path(p) / "activate").is_file()  # a venv's bin
    ]
    env["PATH"] = os.pathsep.join([str(dist.bin), *keep])
    env["UV_FIND_LINKS"] = str(dist.wheels)
    return env


# --------------------------------------------------------------------------- #
# The app and its diff base
# --------------------------------------------------------------------------- #

#: Never part of the app's diff (venv, caches, local data, test byproducts).
IGNORED_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    "test_data",
    "data",
    "node_modules",
    "_dx_product",  # the hidden product tests while they are copied in
}
IGNORED_FILES = re.compile(
    r"\.py[co]$|(^|/)\.observed_effects\.json(\.lock)?$|(^|/)\.DS_Store$|\.egg-info(/|$)"
)


def find_app(work: Path, name: str) -> Path | None:
    """The application under ``work``: ``<work>/<name>``, else the only
    directory (depth <= 2) holding an ``architecture_rules.yml``."""
    if (work / name / "architecture_rules.yml").is_file():
        return work / name
    found = sorted(
        p.parent
        for pattern in ("*/architecture_rules.yml", "*/*/architecture_rules.yml")
        for p in work.glob(pattern)
        if not set(p.relative_to(work).parts) & IGNORED_DIRS
    )
    return found[0] if found else None


def app_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not root.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            path = Path(dirpath) / f
            rel = path.relative_to(root).as_posix()
            if not IGNORED_FILES.search(rel):
                files[rel] = path
    return files


def _content(rel: str, path: Path) -> bytes:
    data = path.read_bytes()
    if rel == ".env":  # `sandloom new` writes a random JWT secret
        data = re.sub(rb"(?m)^JWT_SECRET=.*$", b"JWT_SECRET=<random>", data)
    return data


def app_changes(app: Path, pristine: Path) -> list[str]:
    """Paths added, changed or removed against ``pristine``."""
    now, base = app_files(app), app_files(pristine)
    return sorted(
        rel
        for rel in set(now) | set(base)
        if rel not in now
        or rel not in base
        or _content(rel, now[rel]) != _content(rel, base[rel])
    )


def app_patch(app: Path, pristine: Path, changed: list[str]) -> str:
    """A unified diff of ``changed`` (text files; binaries are named only)."""
    out: list[str] = []
    for rel in changed:
        old_p, new_p = pristine / rel, app / rel
        try:
            old = old_p.read_text().splitlines(keepends=True) if old_p.is_file() else []
            new = new_p.read_text().splitlines(keepends=True) if new_p.is_file() else []
        except UnicodeDecodeError:
            out.append(f"Binary file {rel} differs\n")
            continue
        out.extend(
            difflib.unified_diff(
                old,
                new,
                f"a/{rel}" if old_p.is_file() else "/dev/null",
                f"b/{rel}" if new_p.is_file() else "/dev/null",
            )
        )
    return "".join(out)


#: ``sandloom new`` as the agent typed it: plain, through ``uv run`` or ``uvx``.
SANDLOOM_NEW = re.compile(r"(?:^|[\s;&|(])sandloom\s+new\s+([^\n;&|)]+)")


def sandloom_new_invocations(commands: list[str]) -> list[list[str]]:
    found: list[list[str]] = []
    for cmd in commands:
        for m in SANDLOOM_NEW.finditer(cmd):
            try:
                args = shlex.split(m.group(1))
            except ValueError:
                continue
            # Up to the first shell redirection (`2>&1` leaves `2>`, `> log`).
            stop = next(
                (i for i, a in enumerate(args) if re.match(r"^\d*[<>]", a)), len(args)
            )
            args = args[:stop]
            if args and not args[0].startswith("-"):
                found.append(args)
    return found


def _strip_dir(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--dir":
            skip = True
            continue
        if a.startswith("--dir=") or a in {"-h", "--help"}:
            continue
        out.append(a)
    return out


def _inferred_args(app: Path) -> list[str]:
    """``sandloom new`` arguments read back from the app (no transcript match)."""
    readme = app / "README.md"
    bundled: list[str] = []
    if readme.is_file():
        m = re.search(r"bundled ones: `(\[.*?\])`", readme.read_text())
        if m:
            bundled = [b.removeprefix("sandloom.") for b in json.loads(m.group(1))]
    args = [app.name]
    if bundled:
        args += ["--with", ",".join(bundled)]
    return args


def make_pristine(
    app: Path, commands: list[str], dist: Dist, dest: Path, env: dict[str, str]
) -> str:
    """Regenerate what ``sandloom new`` produced for ``app`` into ``dest/<app>``:
    the last ``sandloom new <app> …`` the agent ran (its options, without
    ``--dir``), else arguments read back from the app. Returns the source
    (``transcript`` | ``inferred``)."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    matches = [a for a in sandloom_new_invocations(commands) if a[0] == app.name]
    source = "transcript" if matches else "inferred"
    args = _strip_dir(matches[-1]) if matches else _inferred_args(app)
    done = sh([str(dist.sandloom), "new", *args, "--dir", str(dest)], dest, env=env)
    if done.returncode or not (dest / app.name).is_dir():
        raise RuntimeError(
            f"could not regenerate the pristine app (sandloom new {' '.join(args)}): "
            + (done.stderr or done.stdout)[-1500:]
        )
    return source


def copy_app(src: Path, dest: Path) -> None:
    """``src`` without venvs and caches (a checkpoint, or a follow-up's start)."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(
        src,
        dest,
        ignore=lambda d, names: [
            n
            for n in names
            if n in IGNORED_DIRS or IGNORED_FILES.search(n) or n.endswith(".egg-info")
        ],
        symlinks=True,
    )


#: Bumped when a seed's build recipe changes shape (a cached build is redone).
SEED_FORMAT = 2


def _seed_digest(seed_dir: Path) -> str:
    """A hash of the seed's recipe and files: an edited seed is rebuilt."""
    digest = hashlib.sha256()
    for path in sorted(p for p in seed_dir.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(seed_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def build_seed(
    seed_dir: Path, dist: Dist, dest: Path, env: dict[str, str], log: Any = print
) -> Path:
    """The start app of a seeded task (``seed = "<name>"``), built once per
    ``dest`` (a label's ``_seed/<name>``) and reused while the dist and the
    seed's files are unchanged. The recipe (``seed.toml``): ``sandloom new`` with
    its arguments, ``uv sync``, ``uv run sandloom add module`` per module (in
    the app's own environment: ``sandloom add`` runs ``sandloom generate``,
    which boots the app), ``remove`` the listed files, copy ``files/`` over
    the app, then ``sandloom generate`` and ``sandloom test`` (it must pass: a
    seeded task starts from a green app). Returns the app directory (with its
    ``.venv``; :func:`copy_app` leaves the venv out of the agent's copy)."""
    import tomllib  # noqa: PLC0415 - stdlib; only seeded runs read a recipe

    recipe = tomllib.loads((seed_dir / "seed.toml").read_text())
    new_args = [str(a) for a in recipe["new"]]
    app = dest / new_args[0]
    key = {
        "format": SEED_FORMAT,
        "sha": dist.sha,
        "seed": _seed_digest(seed_dir),
    }
    dest.mkdir(parents=True, exist_ok=True)
    ready = dest / "READY"
    with (dest / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            meta = json.loads(ready.read_text()) if ready.is_file() else {}
            if {k: meta.get(k) for k in key} == key and app.is_dir():
                return app
            ready.unlink(missing_ok=True)
            started = time.monotonic()
            shutil.rmtree(app, ignore_errors=True)
            done = sh(
                [str(dist.sandloom), "new", *new_args, "--dir", str(dest)], dest, env
            )
            if done.returncode or not app.is_dir():
                raise RuntimeError(f"seed: sandloom new failed: {done.stderr[-1500:]}")
            env = {k: v for k, v in env.items() if k != "SANDLOOM_TEST_TAKE_OWNERSHIP"}
            done = sh(["uv", "sync", "-q"], app, env=env, timeout=900)
            if done.returncode:
                raise RuntimeError(f"seed: uv sync failed: {done.stderr[-1500:]}")
            for module in recipe.get("modules", []):
                done = sh(
                    ["uv", "run", "sandloom", "add", "module", module], app, env=env
                )
                if done.returncode:
                    raise RuntimeError(
                        f"seed: sandloom add module {module} failed: "
                        f"{(done.stdout + done.stderr)[-1500:]}"
                    )
            for rel in recipe.get("remove", []):
                (app / rel).unlink(missing_ok=True)
            files = seed_dir / "files"
            if files.is_dir():
                shutil.copytree(
                    files / new_args[0],
                    app / new_args[0],
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                for extra in files.iterdir():  # app-root files (README, …)
                    if extra.name != new_args[0]:
                        if extra.is_dir():
                            shutil.copytree(extra, app / extra.name, dirs_exist_ok=True)
                        else:
                            shutil.copy2(extra, app / extra.name)
            for cmd, timeout in (
                (["uv", "sync", "-q"], 900),
                (["uv", "run", "sandloom", "generate"], 600),
                (["uv", "run", "sandloom", "test"], 2400),
            ):
                done = sh(cmd, app, env=env, timeout=timeout)
                if done.returncode:
                    (dest / "build.log").write_text(done.stdout + done.stderr)
                    raise RuntimeError(
                        f"seed {seed_dir.name}: `{' '.join(cmd)}` failed (log: "
                        f"{dest / 'build.log'}): {(done.stdout + done.stderr)[-1500:]}"
                    )
            ready.write_text(
                json.dumps(
                    {**key, "seconds": round(time.monotonic() - started)}, indent=2
                )
                + "\n"
            )
            log(f"seed: built {seed_dir.name} in {app}")
            return app
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _installed_sandloom(app: Path) -> dict[str, Path]:
    """The Sandloom distributions in the app's venv: name -> its ``.dist-info``."""
    found: dict[str, Path] = {}
    for info in app.glob(".venv/lib/python*/site-packages/sandloom*.dist-info"):
        meta = (info / "METADATA").read_text(errors="replace")
        name = re.search(r"(?m)^Name: (.+)$", meta)
        if name:
            found[name.group(1).strip()] = info
    return found


def framework_version_mismatches(app: Path, dist: Dist) -> list[str]:
    """Why the app's installed Sandloom distributions are not ``dist``'s build
    (empty: every one carries the version of its wheel, ``+g<sha>``). A
    version that differs is a stale build: uv reused a cached wheel."""
    expected = dist.versions()
    installed = _installed_sandloom(app)
    problems: list[str] = []
    if "sandloom" not in installed:
        problems.append("sandloom is not installed")
    for name, info in sorted(installed.items()):
        meta = (info / "METADATA").read_text(errors="replace")
        m = re.search(r"(?m)^Version: (.+)$", meta)
        version = m.group(1).strip() if m else "?"
        if version != expected.get(name):
            problems.append(f"{name} {version} (built: {expected.get(name)})")
    return problems


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(  # noqa: S324 - git's object id, not a security hash
        b"blob %d\0" % len(data) + data
    ).hexdigest()


def framework_content_mismatches(app: Path, dist: Dist, repo: Path) -> list[str]:
    """Installed ``sandloom/`` and ``sandloom_<module>/`` files whose bytes are
    not the file at ``dist.sha`` (``src/<package>/...``; the packaged core chain
    is ``migrations/core``)."""
    listing = sh(
        ["git", "ls-tree", "-r", dist.sha, "--", "src", "migrations/core"], repo
    ).stdout
    blobs = {
        path: line.split()[2]
        for line in listing.splitlines()
        for path in [line.split("\t", 1)[1]]
    }
    problems: list[str] = []
    checked = 0
    for info in _installed_sandloom(app).values():
        site = info.parent
        for line in (info / "RECORD").read_text().splitlines():
            rel = line.rsplit(",", 2)[0]
            if not rel.startswith(("sandloom/", "sandloom_")) or rel.endswith(".pyc"):
                continue
            if ".dist-info/" in rel:
                continue
            core_chain = "sandloom/core/migrations/core_chain/"
            source = (
                "migrations/core/" + rel.removeprefix(core_chain)
                if rel.startswith(core_chain)
                else f"src/{rel}"
            )
            path = site / rel
            checked += 1
            if not path.is_file() or blobs.get(source) != _git_blob(path.read_bytes()):
                problems.append(rel)
    if not checked:
        problems.append("no installed sandloom/ file to compare")
    return problems


def tampered_framework_files(app: Path) -> list[str]:
    """Installed ``sandloom`` files the agent edited in the app's venv: each Sandloom
    distribution's ``RECORD`` hash no longer matches (``site-packages/sandloom/…``)."""
    bad: list[str] = []
    for record in app.glob(
        ".venv/lib/python*/site-packages/sandloom*.dist-info/RECORD"
    ):
        site = record.parent.parent
        for line in record.read_text().splitlines():
            parts = line.rsplit(",", 2)
            if len(parts) != 3 or not parts[1].startswith("sha256="):
                continue
            path = site / parts[0]
            if not parts[0].startswith(("sandloom/", "sandloom_")):
                continue
            if ".dist-info/" in parts[0] or not path.is_file():
                continue
            digest = urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest())
            if digest.rstrip(b"=").decode() != parts[1].removeprefix("sha256="):
                bad.append(f"site-packages/{parts[0]}")
    return sorted(bad)


# --------------------------------------------------------------------------- #
# Classification of the agent's changes outside its module
# --------------------------------------------------------------------------- #


def generated_re(app: str) -> re.Pattern[str]:
    """Files ``sandloom test`` (or ``uv sync``: the lock) writes in an app."""
    a = re.escape(app)
    return re.compile(
        rf"^{a}/shared/events/[^/]+\.json$"
        rf"|^{a}/shared/contracts/[a-z0-9_]+\.py$"
        rf"|^{a}/modules/[a-z_][a-z0-9_]*/(module|routes|contracts)\.snapshot\.json$"
        r"|^uv\.lock$"
    )


def classify_app_path(path: str, app: str) -> str:
    """The kind of a changed path outside the task's module(s):

    - ``wiring``: the app's own expected wiring (``pyproject.toml``, ``.env*``,
      ``deploy/``, ``architecture_rules.yml``, CI / pre-commit / Docker files,
      the composition root ``<app>/asgi.py``): NOT counted as edited outside;
    - ``rule``: the app's shared DTO package ``<app>/shared/`` (contract DTOs
      live there by rule): NOT counted;
    - ``framework``: an installed framework file edited in the venv;
    - ``other-module``: another module of the app (the sample module too);
    - ``docs``: documentation; ``other``: anything else (``tests/``, …).
    """
    a = re.escape(app)
    if path.startswith("site-packages/"):
        return "framework"
    if re.search(
        rf"^(pyproject\.toml|\.env(\.[\w.-]+)?|architecture_rules\.yml|Dockerfile"
        rf"|\.dockerignore|\.gitignore|\.pre-commit-config\.yaml|{a}/asgi\.py"
        rf"|{a}/__init__\.py|{a}/modules/__init__\.py)$|^(deploy|\.github)/",
        path,
    ):
        return "wiring"
    if re.match(rf"^{a}/shared/", path):
        return "rule"
    if re.match(rf"^{a}/modules/", path):
        return "other-module"
    if path.endswith(".md"):
        return "docs"
    return "other"


#: Kinds that are the app's expected wiring, not "edited outside".
EXPECTED_KINDS = ("wiring", "rule")


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #

SUMMARY_LINE = re.compile(
    r"^\s{2}(PASS|FAIL \(exit -?\d+\))\s+(.+?)\s+-?\d+(?:\.\d+)?s(?:\s+->.*)?$", re.M
)


def parse_sandloom_test(output: str) -> dict[str, bool]:
    """``sandloom test``'s summary: step name -> passed."""
    return {
        m.group(2).strip(): m.group(1) == "PASS" for m in SUMMARY_LINE.finditer(output)
    }


def run_app_gates(
    app: Path,
    env: dict[str, str],
    module_tests: list[str],
    logs: Path,
    *,
    require_module_tests: bool = True,
) -> dict[str, bool]:
    """The app's own gates, run by the harness on the agent's resources:

    * ``sandloom test --ci`` — every step is a gate (``ci: <step>``): the
      generated files must be current, never written;
    * ``sandloom test`` — the developer loop, one gate (it never writes a
      generated file either: the agent runs ``sandloom generate``);
    * ``module-tests`` — the tests of the task's module(s), slow ones too.
    """
    env = {k: v for k, v in env.items() if k != "SANDLOOM_TEST_TAKE_OWNERSHIP"}
    logs.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}
    for name, cmd in (
        ("sandloom-test-ci", ["uv", "run", "sandloom", "test", "--ci"]),
        ("sandloom-test", ["uv", "run", "sandloom", "test"]),
    ):
        try:
            done = sh(cmd, app, env=env, timeout=2400)
            output = done.stdout + done.stderr
            (logs / f"{name}.log").write_text(output[-60000:])
            if name == "sandloom-test-ci":
                steps = parse_sandloom_test(output)
                results.update({f"ci: {k}": ok for k, ok in steps.items()})
                if not steps:  # no summary at all: sandloom test could not start
                    results[name] = done.returncode == 0
            else:
                results[name] = done.returncode == 0
        except subprocess.TimeoutExpired:
            results[name] = False
            (logs / f"{name}.log").write_text("timeout\n")
    if module_tests:
        cmd = ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *module_tests]
        try:
            done = sh(cmd, app, env=env, timeout=1800)
            results["module-tests"] = done.returncode == 0
            (logs / "module-tests.log").write_text((done.stdout + done.stderr)[-30000:])
        except subprocess.TimeoutExpired:
            results["module-tests"] = False
    elif require_module_tests:
        results["module-tests"] = False
        (logs / "module-tests.log").write_text("no tests in the task's module(s)\n")
    return results
