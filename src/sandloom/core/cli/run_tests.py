"""``sandloom test`` — the one command for the inner loop and the gates.

    sandloom test                  fast suite + static gates
    sandloom test billing          billing's own tests first, then the same
    sandloom test --full           the parallel FULL suite (slow tests included)
    sandloom test --no-gates       tests only
    sandloom test --plan           print the steps, run nothing
    sandloom test --ci             CI: no fixes either (ruff check --no-fix)
    sandloom test --seed N         replay a test order (default: a fresh seed, printed)
    sandloom test --no-shuffle     run the tests in file order

Every test step runs in a seeded random order (``sandloom.core.testing.shuffle``:
the files, then the tests inside each file), so a test that passes only after
another one fails here instead of in someone else's run. The seed is printed
before the steps and again after a failure.

Steps, in order (all run; the summary lists each, exit 1 if any failed):

1. ``generated files`` (with the gates): the L1 modules' generated files --
   ``module.snapshot.json``, the route and contract snapshots, the exported
   producer event schemas, the contract stubs and the DTO copies in the
   shared DTO package (:mod:`sandloom.core.cli.generated`) -- compared with
   what ``sandloom generate`` would write. ``sandloom test`` NEVER writes (dev
   and ``--ci`` alike): an incompatible change fails with its code
   (SANDLOOM-T001 / T004 / T005 / T007 / C012) and a stale file fails with
   "generated files are stale: run `sandloom generate`" and the list; then ``schema &
   deploy`` (:mod:`sandloom.core.testing.static_checks`, no database or Redis):
   migration-chain coverage (SANDLOOM-T002), redundant indexes (SANDLOOM-T012) and one
   beat per module with periodic tasks in each deploy profile (SANDLOOM-T010);
2. ``<module>``: the module's own ``tests/`` (when a module is named);
3. ``suite``: ``pytest -m "not slow" -n auto --dist loadfile`` with
   ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` (``--full``: without ``-m "not slow"``;
   serially, with a notice, when pytest-xdist is not installed);
4. static gates: ``ruff check`` (``--no-fix`` with ``--ci``), ``ruff format
   --check``, ``mypy``,
   ``sandloom check`` (architecture rules, SANDLOOM-Cnnn), the framework tree's
   architecture tests, ``lint-imports`` (when the project has a
   ``.importlinter``), the contract snapshots (SANDLOOM-T004), the route
   snapshots (SANDLOOM-T001), the migration drift (SANDLOOM-T006: every module chain
   applied to a scratch database and compared with the models) and, in the
   framework tree, the public API baseline (SANDLOOM-T008);
5. ``typing tests`` (``--full``, framework tree): the L1 typing proofs under
   mypy ``--strict`` and pyright (``tests/typing``).

Run it from the project root (the directory holding ``pyproject.toml`` /
``architecture_rules.yml``): the framework's ``backend/`` or an ``sandloom new``
app. Tests need the project's test services and test env, like ``pytest``.

Every step sees the SAME test environment, loaded once before the first step
exactly as the pytest harness loads it (:func:`load_project_test_env`): the
nearest ``.env.test`` (override), then the ``SANDLOOM_TEST_*`` variables on top —
so the in-process snapshot checks work on a checkout without ``.env``. Every
xdist step (``-n``) gets ``SANDLOOM_TEST_REDIS_DB_STRIDE=auto`` unless the caller
exported a stride, so parallel steps never collide with other runs' Redis
indexes.

Not covered, framework tree only: the benchmark security suite
(``benchmarks/tests``, CI job ``benchmarks-security``). It needs a superuser
connection to build its RLS roles (``S5_TEST_ROLE``) and runs ~6 minutes;
run it with ``tools/ci/ci_local.sh <ref> --jobs benchmarks-security``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import random
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import sandloom.core.cli.generated as _generated_cli
import sandloom.core.cli.tool_process as _tool_process
import sandloom.core.testing.static_checks as _static_checks_cli
from sandloom.core.archcheck.manifest import load_rules
from sandloom.core.cli import check as _check
from sandloom.core.descriptors.module_names import installed_modules
from sandloom.core.testing.env import find_env_file, load_test_env

#: Catalogue code a failing step points to (``sandloom explain <code>``).
_STEP_CODES = {
    "sandloom check": "SANDLOOM-C0xx (per violation)",
    "route snapshots": "SANDLOOM-T001",
    "contract snapshots": "SANDLOOM-T004",
    "generated files": "SANDLOOM-T005 / T007 / C012 (stale: sandloom generate)",
    "migration drift": "SANDLOOM-T006",
    "schema & deploy": "SANDLOOM-T002 / SANDLOOM-T010 / SANDLOOM-T012 (per problem)",
    "public API baseline": "SANDLOOM-T008",
}


@dataclass
class Step:
    """One step: an external command (``argv``) or an in-process ``call``.

    ``env`` holds DEFAULTS for the command's environment (an exported value
    of the same variable wins)."""

    name: str
    argv: list[str] = field(default_factory=list)
    call: Callable[[], int] | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None

    def describe(self) -> str:
        if self.call is not None:
            return f"(in-process) {self.name}"
        prefix = " ".join(f"{k}={v}" for k, v in self.env.items())
        return (prefix + " " if prefix else "") + " ".join(self.argv)


def _run_command(step: Step, root: Path) -> int:
    # A step's variables are defaults: a value the caller exported wins.
    env = dict(os.environ)
    for key, value in step.env.items():
        env.setdefault(key, value)
    return _tool_process.run_tool(step.argv, cwd=step.cwd or root, env=env).returncode


#: Default for every pytest-xdist step (an exported value wins).
_XDIST_ENV = {"SANDLOOM_TEST_REDIS_DB_STRIDE": "auto"}
#: Outside ``--ci`` the tests record the effects each route ran (G-ROUTE-05),
#: which ``sandloom routes snapshot --write`` attaches as observed_effects.
_OBSERVE_ENV = {"SANDLOOM_TEST_OBSERVE_EFFECTS": "1"}


def load_project_test_env(root: Path) -> Path | None:
    """Load the project's test env for every step (``None``: no ``.env.test``).

    The harness's own loader (:func:`sandloom.core.testing.env.load_test_env`):
    the nearest ``.env.test`` with override, then ``SANDLOOM_TEST_*`` overrides.
    Must run before anything imports ``sandloom.core.config.settings``.
    """
    if find_env_file([root]) is None:
        return None
    return load_test_env([root])


#: The seeded-order plugin every test step loads (``--seed`` / ``--no-shuffle``).
SHUFFLE_PLUGIN = "sandloom.core.testing.shuffle"


def fresh_seed() -> int:
    """A new test-order seed (printed, so a failure replays with ``--seed``)."""
    return random.SystemRandom().randrange(1, 10**8)


#: Printed once when the project's python has no pytest-xdist.
XDIST_MISSING_NOTICE = (
    "sandloom test: pytest-xdist is not installed in this environment; the test "
    "steps run serially (add pytest-xdist to the dev dependencies)"
)


def xdist_available() -> bool:
    """Whether pytest-xdist is importable by the python running ``sandloom test``
    (every pytest step runs ``sys.executable -m pytest``)."""
    return importlib.util.find_spec("xdist") is not None


def _xdist_step(
    name: str,
    argv: list[str],
    *,
    xdist: bool,
    xdist_args: list[str],
    tail: tuple[str, ...] = (),
) -> Step:
    """A pytest-xdist step: ``argv + xdist_args + tail``, with the stride
    default (see module doc).

    Without xdist the step runs serially: ``xdist_args`` (``-n`` /
    ``--dist``) are left out, so pytest does not refuse them (exit 4)."""
    if not xdist:
        return Step(name, [*argv, *tail])
    return Step(name, [*argv, *xdist_args, *tail], env=dict(_XDIST_ENV))


def _module_tests(root: Path, module: str) -> Path | None:
    """The tests of ``module``: its directory name, or the module name of a
    module package (``billing`` -> ``src/sandloom_billing/tests``)."""
    names = [module]
    names += [m.path for m in installed_modules() if m.name == module]
    rules = root / "architecture_rules.yml"
    if rules.is_file():
        modules_path = load_rules(rules).modules_path
        for name in names:
            tests = modules_path.joinpath(*name.split(".")) / "tests"
            if tests.is_dir():
                return tests
    for candidate in (
        *(root / "src" / name / "tests" for name in names),
        root / module / "tests",
    ):
        if candidate.is_dir():
            return candidate
    return None


def _mypy_targets(root: Path) -> list[str]:
    try:
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    if config.get("tool", {}).get("mypy", {}).get("files"):
        return []  # mypy reads its own `files`
    if (root / "src").is_dir():
        return ["src"]
    name = config.get("project", {}).get("name", "")
    package = name.replace("-", "_")
    return [package] if package and (root / package).is_dir() else ["."]


def _lint_targets(root: Path) -> list[str]:
    return (
        (["src/", "tests/", "conftest.py"] if (root / "tests").is_dir() else ["src/"])
        if (root / "src").is_dir()
        else ["."]
    )


def _in_process(fn: Callable[[list[str]], int], argv: list[str]) -> Callable[[], int]:
    def _call() -> int:
        try:
            return fn(argv)
        except SystemExit as exc:  # a configuration refusal
            print(exc, file=sys.stderr)
            return int(exc.code) if isinstance(exc.code, int) else 1

    return _call


def _sandloom_check(argv: list[str]) -> int:
    return _check.main(argv)


def _contracts(argv: list[str]) -> int:
    from sandloom.core.cli.contracts import main  # noqa: PLC0415 - cold: configured command - builds settings; sandloom test loads .env.test first, the static steps stay settings-free

    return main(argv)


def _generated(argv: list[str]) -> int:
    return _generated_cli.main(argv)


def _static_checks(argv: list[str]) -> int:
    return _static_checks_cli.main(argv)


def _routes(argv: list[str]) -> int:
    from sandloom.core.cli.routes import main  # noqa: PLC0415 - cold: configured command - builds settings; sandloom test loads .env.test first, the static steps stay settings-free

    return main(argv)


def plan(
    root: Path,
    module: str | None,
    *,
    full: bool,
    gates: bool,
    workers: str,
    ci: bool = False,
    xdist: bool | None = None,
    shuffle_seed: int | None = None,
) -> list[Step]:
    """The steps ``sandloom test`` runs, in order.

    ``xdist`` (default: detected, :func:`xdist_available`) selects parallel
    pytest steps; without it they run serially. ``shuffle_seed`` runs the
    module's tests and the suite in that seeded random order
    (:mod:`sandloom.core.testing.shuffle`)."""
    python = sys.executable
    shuffle = (
        []
        if shuffle_seed is None
        else ["-p", SHUFFLE_PLUGIN, f"--sandloom-shuffle={shuffle_seed}"]
    )
    if xdist is None:
        xdist = xdist_available()
    steps: list[Step] = []
    if gates and (root / "architecture_rules.yml").is_file():
        argv = ["--root", str(root), "--check"]  # sandloom test never writes
        steps.append(Step("generated files", call=_in_process(_generated, argv)))
        steps.append(
            Step(
                "schema & deploy",
                call=_in_process(_static_checks, ["--root", str(root)]),
            )
        )
    if module:
        tests = _module_tests(root, module)
        if tests is None:
            raise ValueError(f"no tests/ directory found for module {module!r}")
        steps.append(
            Step(
                module,
                [python, "-m", "pytest", "-q", *shuffle, str(tests)],
                env={} if ci else dict(_OBSERVE_ENV),
            )
        )
    step = _xdist_step(
        "full suite" if full else "suite (not slow)",
        [python, "-m", "pytest", "-q", *shuffle],
        xdist=xdist,
        xdist_args=["-n", workers, "--dist", "loadfile"],
        tail=() if full else ("-m", "not slow"),
    )
    if not ci:
        step.env.update(_OBSERVE_ENV)
    steps.append(step)
    if not gates:
        return steps
    return steps + _gate_steps(
        root, python, workers=workers, full=full, xdist=xdist, ci=ci
    )


def _gate_steps(
    root: Path,
    python: str,
    *,
    workers: str,
    full: bool,
    xdist: bool,
    ci: bool = False,
) -> list[Step]:
    """The static gates of :func:`plan`, in order. ``ci``: nothing is
    written, so ``ruff check`` reports what a project's ``fix = true`` would
    otherwise fix silently (``--no-fix``)."""
    no_fix = ["--no-fix"] if ci else []
    steps: list[Step] = []
    steps += [
        Step(
            "ruff check",
            [python, "-m", "ruff", "check", *no_fix, *_lint_targets(root)],
        ),
        Step(
            "ruff format",
            [python, "-m", "ruff", "format", "--check", *_lint_targets(root)],
        ),
        Step("mypy", [python, "-m", "mypy", *_mypy_targets(root)]),
    ]
    if (root / "architecture_rules.yml").is_file():
        steps.append(Step("sandloom check", call=_in_process(_sandloom_check, [])))
    arch_tests = root / "tests" / "architecture"
    if arch_tests.is_dir():  # the framework tree's own architecture suite
        steps.append(
            _xdist_step(
                "architecture tests",
                [python, "-m", "pytest", "-q"],
                xdist=xdist,
                xdist_args=["-n", workers],
                tail=(str(arch_tests),),
            )
        )
    if (root / ".importlinter").is_file():
        src = root / "src" if (root / "src").is_dir() else root
        steps.append(
            Step(
                "lint-imports",
                [
                    str(Path(python).with_name("lint-imports")),
                    "--config",
                    str(root / ".importlinter"),
                    "--no-cache",
                ],
                cwd=src,
            )
        )
    steps += [
        Step(
            "contract snapshots",
            call=_in_process(_contracts, ["snapshot", "--check"]),
        ),
        Step("route snapshots", call=_in_process(_routes, ["snapshot", "--check"])),
    ]
    if (root / "architecture_rules.yml").is_file():
        steps.append(
            Step(
                "migration drift", [python, "-m", "sandloom.core.cli.migrate", "drift"]
            )
        )
    baseline = root / "tools" / "packaging" / "check_public_api.py"
    if baseline.is_file():  # the framework tree's surface gate
        steps.append(Step("public API baseline", [python, str(baseline)]))
    typing_tests = root / "tests" / "typing"
    if full and typing_tests.is_dir():
        steps.append(
            Step("typing tests", [python, "-m", "pytest", "-q", str(typing_tests)])
        )
    return steps


@dataclass
class Outcome:
    step: Step
    code: int
    seconds: float


def run(steps: list[Step], root: Path) -> list[Outcome]:
    outcomes = []
    for step in steps:
        print(f"\n=== sandloom test: {step.name}\n$ {step.describe()}", flush=True)
        started = time.perf_counter()
        code = step.call() if step.call is not None else _run_command(step, root)
        outcomes.append(Outcome(step, code, time.perf_counter() - started))
    return outcomes


def summary(outcomes: list[Outcome]) -> str:
    lines = ["", "sandloom test summary", "----------------"]
    for o in outcomes:
        status = "PASS" if o.code == 0 else f"FAIL (exit {o.code})"
        code = _STEP_CODES.get(o.step.name)
        hint = f"  -> sandloom explain {code}" if o.code and code else ""
        lines.append(f"  {status:16} {o.step.name:20} {o.seconds:7.1f}s{hint}")
    failed = [o.step.name for o in outcomes if o.code]
    lines.append("")
    lines.append(
        f"sandloom test: FAILED ({', '.join(failed)})"
        if failed
        else "sandloom test: OK"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom test",
        description="Module tests, the fast (or full) suite and the static gates",
    )
    parser.add_argument("module", nargs="?", help="run this module's tests first")
    parser.add_argument("--full", action="store_true", help="include slow tests")
    parser.add_argument("--no-gates", action="store_true", help="skip the static gates")
    parser.add_argument("-n", "--workers", default="auto", help="pytest-xdist workers")
    parser.add_argument("--plan", action="store_true", help="print the steps only")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI: ruff check reports instead of fixing (generated files are "
        "always only checked; `sandloom generate` writes them)",
    )
    order = parser.add_mutually_exclusive_group()
    order.add_argument(
        "--seed",
        type=int,
        default=None,
        help="replay this test-order seed (default: a fresh one, printed)",
    )
    order.add_argument(
        "--no-shuffle",
        action="store_true",
        help="run the tests in file order (default: a seeded random order)",
    )
    args = parser.parse_args(argv)
    seed = None if args.no_shuffle else (args.seed or fresh_seed())
    root = Path.cwd()
    xdist = xdist_available()
    try:
        steps = plan(
            root,
            args.module,
            full=args.full,
            gates=not args.no_gates,
            workers=args.workers,
            ci=args.ci,
            xdist=xdist,
            shuffle_seed=seed,
        )
    except ValueError as exc:
        print(f"sandloom test: {exc}", file=sys.stderr)
        return 2
    if not xdist:
        print(XDIST_MISSING_NOTICE, file=sys.stderr)
    if args.plan:
        for step in steps:
            print(f"{step.name:20} {step.describe()}")
        return 0
    env_file = load_project_test_env(root)
    if env_file is None:
        print(
            "sandloom test: no .env.test found walking up from "
            f"{root}; steps use the current environment",
            file=sys.stderr,
        )
    else:
        print(f"sandloom test: test environment from {env_file} (+ SANDLOOM_TEST_*)")
    if seed is not None:
        print(
            f"sandloom test: test order seed {seed} (replay: "
            f"sandloom test --seed {seed})"
        )
    outcomes = run(steps, root)
    print(summary(outcomes))
    if seed is not None and any(o.code for o in outcomes):
        print(
            f"sandloom test: test order seed {seed} (replay: "
            f"sandloom test --seed {seed})"
        )
    return 1 if any(o.code for o in outcomes) else 0


__all__ = [
    "SHUFFLE_PLUGIN",
    "XDIST_MISSING_NOTICE",
    "Outcome",
    "Step",
    "fresh_seed",
    "main",
    "plan",
    "run",
    "summary",
    "xdist_available",
]
