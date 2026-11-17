"""Build every Sandloom distribution and prove it works installed alone.

This is the local/pre-push equivalent of ``.github/workflows/packages.yml``:

1. each package builds an sdist and a wheel (direct and rebuilt from the
   sdist: same runtime files), with the reviewed license metadata;
2. import layering: no shipped module imports a module of a distribution its
   own does not depend on (function-local and lazy imports included;
   :mod:`distribution_imports`);
3. ``pip install sandloom`` alone, in a clean environment: no Redis/Celery,
   every ``sandloom`` subcommand's ``--help``, ``sandloom docs <name>``,
   ``sandloom new`` and the kernel app factory work; the RPC transport
   smoke runs from the installed wheel; the kernel's test harness (the pytest
   plugin, with the ``testing`` and ``sqlite`` extras) runs two xdist workers
   with no Redis client and so no harness family;
4. ``sandloom[standard]`` (every wheel but the console): the standard root
   ``sandloom_standard.main:app`` (the ``sandloom-standard`` distribution, the
   one ``SANDLOOM_COMPOSITION_ROOT`` names in the image and the profiles)
   composes the installed ``sandloom.modules`` entry points;
5. each module wheel on plain ``sandloom`` (no Celery, no Redis client): the
   app boots with the module loaded, ``/health`` and ``/ready`` answer 200
   over stubbed infrastructure probes, and its ``tasks.py`` declares its
   tasks without importing Celery (KB-01).

``--skip-install`` runs steps 1-2 only (no network).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from tools.packaging import distribution_imports, inspection_smoke, release_packages
else:
    import distribution_imports
    import inspection_smoke
    import release_packages

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "packages"
#: The distributions, from the one list (``packages.json``): what each kind
#: of package is checked for comes from its entry there, never from its name.
PACKAGES = release_packages.NAMES
KERNEL = release_packages.kernel().name
#: Module packages and their ``sandloom.modules`` entry-point names.
MODULES = {
    p.name: (p.module, p.import_name) for p in release_packages.of_kind("module")
}
#: Third-party distributions a module wheel may import through ``sandloom``
#: without declaring them: the stack the kernel promises modules (routes,
#: models, migrations, schemas, outbound HTTP), kept within the kernel's
#: ranges. Every other kernel requirement (argon2-cffi, cryptography, redis,
#: …) is the kernel's implementation: a module importing it declares it.
KERNEL_PROVIDES = ("fastapi", "starlette", "sqlalchemy", "alembic", "pydantic", "httpx")
SMOKE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "JWT_SECRET": "local-package-check-not-a-real-secret",
    "EVENT_BUS_BACKEND": "memory",
}
#: Subcommands whose ``--help`` needs nothing but the arguments.
DOCS_NAMES = ("define_module", "sandloom.sdk.web")


def _run(
    *command: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        check=check,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=capture_output,
    )


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _script(venv: Path, name: str) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / name


def _smoke_environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANDLOOM_")}
    env.pop("VIRTUAL_ENV", None)
    # A checkout's test seed (``.env.test``) states url-only instances that a
    # kernel-only install cannot complete with a driver: the smoke selects none.
    env.pop("INFRA", None)
    return {**env, **SMOKE_ENV}


def _check_license_metadata(payload: bytes, artifact: Path) -> None:
    metadata = BytesParser().parsebytes(payload)
    if metadata.get("License-Expression") != "Apache-2.0":
        raise RuntimeError(f"{artifact.name}: missing Apache-2.0 License-Expression")
    if sorted(metadata.get_all("License-File", [])) != ["LICENSE", "NOTICE"]:
        raise RuntimeError(
            f"{artifact.name}: expected LICENSE and NOTICE metadata entries"
        )


def _check_license_files(wheel: Path, source_archive: Path | None = None) -> None:
    expected = {
        name: (BACKEND_ROOT / name).read_bytes() for name in ("LICENSE", "NOTICE")
    }
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise RuntimeError(f"{wheel.name}: expected one METADATA file")
        metadata_path = metadata_files[0]
        _check_license_metadata(archive.read(metadata_path), wheel)
        prefix = metadata_path.removesuffix("METADATA") + "licenses/"
        for name, payload in expected.items():
            if archive.read(prefix + name) != payload:
                raise RuntimeError(
                    f"{wheel.name}: {name} differs from the reviewed source"
                )
    if source_archive is not None:
        with tarfile.open(source_archive) as archive:
            metadata_files = [
                member.name
                for member in archive.getmembers()
                if len(Path(member.name).parts) == 2
                and member.name.endswith("/PKG-INFO")
            ]
            if len(metadata_files) != 1:
                raise RuntimeError(
                    f"{source_archive.name}: expected one top-level PKG-INFO"
                )
            metadata_path = metadata_files[0]
            metadata = archive.extractfile(metadata_path)
            if metadata is None:
                raise RuntimeError(f"{source_archive.name}: missing PKG-INFO contents")
            _check_license_metadata(metadata.read(), source_archive)
            prefix = metadata_path.removesuffix("PKG-INFO")
            for name, payload in expected.items():
                member = archive.extractfile(prefix + name)
                if member is None or member.read() != payload:
                    raise RuntimeError(
                        f"{source_archive.name}: {name} differs from the reviewed source"
                    )
    print(f"License metadata and notices OK: {wheel.name}", flush=True)


def _contents(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if ".dist-info/" not in name
        }


def _check_contents(package: str, wheel: Path) -> None:
    """Only the package's own import package; no tests, no repo-only files."""
    contents = _contents(wheel)
    names = [n for n in contents if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in names}
    expected = {package.replace("-", "_")}
    if tops != expected:
        raise RuntimeError(f"{wheel.name}: ships {sorted(tops)}, expected {expected}")
    leaked = [n for n in names if "/tests/" in n or n.endswith((".pyc", ".bak"))]
    if leaked:
        raise RuntimeError(f"{wheel.name}: ships repository-only files {leaked[:5]}")
    if package == KERNEL:
        # ADR-0013: the kernel ships no generated stub, DTO copy or event schema
        # of a module (each module wheel carries its own `_generated` copies and
        # `module.snapshot.json`), so a module change never touches this wheel.
        owned = [
            n
            for n in names
            if n.startswith(("sandloom/shared/contracts/", "sandloom/shared/events/"))
            or n == "sandloom/shared/membership.py"
        ]
        owned += [
            n
            for n in names
            if n.endswith((".py", ".json"))
            and (
                contents[n].startswith(b"# Generated by `sandloom ")
                or n.endswith(("module.snapshot.json", "contracts.snapshot.json"))
            )
        ]
        if owned:
            raise RuntimeError(f"{wheel.name}: ships module-owned files {owned[:5]}")
    entry_points = _entry_points(wheel)
    if package in MODULES:
        name, path = MODULES[package]
        if entry_points.get("sandloom.modules") != {name: path}:
            raise RuntimeError(
                f"{wheel.name}: expected the sandloom.modules entry point "
                f"{name} = {path}, found {entry_points}"
            )
    elif "sandloom.modules" in entry_points:
        # The kernel and the standard composition root compose modules; they
        # register none.
        raise RuntimeError(f"{wheel.name}: registers a module, expected none")
    drivers = release_packages.by_name(package).drivers
    if drivers is not None and entry_points.get("sandloom.drivers") != {
        name: driver["target"] for name, driver in drivers.items()
    }:
        raise RuntimeError(
            f"{wheel.name}: expected the {', '.join(drivers)} driver entry point"
        )
    if package == KERNEL:
        check_kernel_backends(wheel)


#: Backend client libraries the kernel wheel never imports (ADR-0016): the
#: Redis and Celery stacks belong to the driver packages ``sandloom-redis`` and
#: ``sandloom-celery``. ``asyncpg`` is the kernel's one backend client, and it
#: stays an extra (``sandloom[postgres]``): imported, never required.
KERNEL_BANNED_IMPORTS = frozenset({"redis", "aioredis", "celery", "kombu"})
#: Requirements the kernel wheel never declares unconditionally: a base
#: requirement installs with ``pip install sandloom``.
KERNEL_BANNED_BASE_REQUIREMENTS = KERNEL_BANNED_IMPORTS | {
    "asyncpg",
    "hiredis",
    "sandloom-redis",
    "sandloom-celery",
}
#: The extra that carries ``asyncpg``.
POSTGRES_EXTRA = "postgres"
#: Redis wire commands as a whole string constant (``execute_command("XADD")``),
#: and a Lua script calling the server (``redis.call(``): protocol code, which
#: lives in ``sandloom_redis``. A name in a comment or a docstring is not one.
#: The stream commands and ``EVALSHA`` (and its ``_RO`` form) are distinctive in
#: any case; ``EVAL``, ``SCRIPT`` and the sorted-set commands (``ZADD``,
#: ``ZRANGE``, ...) are ordinary words in lower case, so only the upper case
#: spelling counts. What follows the command is a subcommand (``GROUP``) or
#: an argument that is not a word (``*``, ``0-0``): a sentence that starts with
#: a command name ("XADD is slow") is prose and passes. A whole-line command
#: with lower-case arguments ("XLEN mystream", "XADD orders * a b") is still
#: caught, for the distinctive commands only: the name and exactly one more word
#: (any), or the name and several words of which at least one starts with a
#: character that is not a letter (``*``, an id, a digit, a parenthesis, a quote,
#: a dash). Caught: "XLEN mystream", "XADD orders * a b", "XACK s g 1-0", and
#: with them prose that has such a word ("XADD is 10x slower", "XLEN is 0", "XLEN
#: (approximate) is bounded"), because an id or a count looks the same: an
#: accepted false alarm, which costs one reword ("XADD is ten times slower") or a
#: move into a comment or a docstring. Not caught: "XACK stream group id" (three
#: plain lower-case words reads as prose, an accepted miss) and any prose whose
#: later words all start with a letter ("XADD is slow."). An all-caps sentence that
#: starts with ``SCRIPT`` or ``EVAL`` ("SCRIPT FAILED: see log") is flagged too. A
#: false alarm costs one reword, a miss lets protocol code into the kernel wheel;
#: a spelling-only check cannot do better. Each of these is pinned by a case of
#: ``tests/architecture/test_release_packages.py``.
_WIRE_COMMAND = re.compile(
    r"(?:(?i:X(?:ADD|READGROUP|READ|AUTOCLAIM|GROUP|ACK|DEL|TRIM|CLAIM|PENDING"
    r"|INFO|LEN|SETID|REVRANGE|RANGE)|EVALSHA(?:_RO)?)"
    r"(?:\s+\S+|\s+(?:\S+\s+)*[^A-Za-z\s].*)?"
    r"|(?:EVAL(?:_RO)?|SCRIPT|Z(?:ADD|REM|RANGE|REVRANGE|RANGEBYSCORE"
    r"|REMRANGEBYSCORE|POPMIN|POPMAX|CARD|SCORE|INCRBY|COUNT)))"
    r"(?:\s+(?:[A-Z][A-Z_]*\b|[^A-Za-z\s]).*)?",
    re.DOTALL,
)
_LUA_CALL = re.compile(r"\bredis\.(?:p?call)\(")


def _wire_code_strings(source: str) -> list[str]:
    """String constants of ``source`` that are Redis protocol code."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and (_WIRE_COMMAND.fullmatch(node.value) or _LUA_CALL.search(node.value))
    ]


def check_kernel_backends(wheel: Path) -> None:
    """The kernel wheel names no Redis or Celery and requires no database client.

    Three rules, read from the built wheel: no ``.py`` file imports ``redis``,
    ``aioredis``, ``celery`` or ``kombu`` (at any depth, guarded or not: the
    Redis server of ``sandloom dev`` is described by ``sandloom-redis``); no
    code string is a Redis wire command or a Lua call; and no
    base requirement installs a Redis, Celery or ``asyncpg`` distribution, with
    ``asyncpg`` declared by the ``postgres`` extra (the kernel serves SQLite
    without it)."""
    built = distribution_imports.read_wheel(wheel)
    problems: list[str] = []
    for module, source in sorted(built.modules.items()):
        imports = distribution_imports.module_imports(
            source, module, is_package=module in built.packages
        )
        problems.extend(
            f"{module}:{found.line} imports {found.target}"
            for found in imports
            if found.target.split(".")[0] in KERNEL_BANNED_IMPORTS
        )
        problems.extend(
            f"{module} carries Redis protocol code {text[:40]!r}"
            for text in _wire_code_strings(source)
        )
    required = built.requires & KERNEL_BANNED_BASE_REQUIREMENTS
    if required:
        problems.append(f"base requirements {sorted(required)} (extras only)")
    postgres = {name for name, _extras in built.optional.get(POSTGRES_EXTRA, set())}
    if "asyncpg" not in postgres:
        problems.append(f"asyncpg is not declared by the {POSTGRES_EXTRA!r} extra")
    if problems:
        raise RuntimeError(
            f"{wheel.name}: the kernel wheel is not backend-free:\n  "
            + "\n  ".join(problems)
        )


def _entry_points(wheel: Path) -> dict[str, dict[str, str]]:
    with zipfile.ZipFile(wheel) as archive:
        found = [
            n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")
        ]
        if not found:
            return {}
        text = archive.read(found[0]).decode()
    groups: dict[str, dict[str, str]] = {}
    group = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            groups[group] = {}
        elif "=" in line and group:
            key, value = (part.strip() for part in line.split("=", 1))
            groups[group][key] = value
    return groups


def build(out: Path) -> dict[str, Path]:
    """sdist + wheel of every package; returns ``{package: rebuilt wheel}``.

    The packages build side by side (each into its own files; the build hook
    writes only temporary directories); a package's two builds stay in order.
    """
    wheels = out / "wheels"
    wheels.mkdir()

    def build_one(package: str) -> Path:
        _run("uv", "build", "--out-dir", str(wheels), str(PACKAGE_ROOT / package))
        # Compare the wheel rebuilt from a standalone sdist (uv's default)
        # with the direct monorepo wheel; the runtime files must match.
        direct = out / "direct" / package
        _run(
            "uv", "build", "--wheel", "--out-dir", str(direct),
            str(PACKAGE_ROOT / package),
        )  # fmt: skip
        stem = package.replace("-", "_")
        rebuilt = next(wheels.glob(f"{stem}-*.whl"))
        original = next(direct.glob("*.whl"))
        source_archive = next(wheels.glob(f"{stem}-*.tar.gz"))
        _check_license_files(rebuilt, source_archive)
        _check_license_files(original)
        if _contents(rebuilt) != _contents(original):
            raise RuntimeError(f"{package}: sdist roundtrip changed wheel contents")
        _check_contents(package, rebuilt)
        return rebuilt

    with ThreadPoolExecutor(max_workers=len(PACKAGES)) as pool:
        return dict(zip(PACKAGES, pool.map(build_one, PACKAGES), strict=True))


def check_layering(wheels: dict[str, Path]) -> None:
    problems = distribution_imports.check(
        [distribution_imports.read_wheel(w) for w in wheels.values()],
        first_party=[p.import_name for p in release_packages.PACKAGES],
        provides={KERNEL: KERNEL_PROVIDES},
    )
    if problems:
        raise RuntimeError(
            "distribution import layering broken:\n  " + "\n  ".join(problems)
        )
    print(f"Import layering OK across {len(wheels)} distributions", flush=True)


def _subcommands(sandloom: Path, env: dict[str, str], cwd: Path) -> list[str]:
    help_text = _run(str(sandloom), "--help", env=env, cwd=cwd, capture_output=True)
    match = re.search(r"\{([a-z,_-]+)\}", help_text.stdout)
    if match is None:
        raise RuntimeError(
            f"no subcommand list in `sandloom --help`:\n{help_text.stdout}"
        )
    return match.group(1).split(",")


def _check_battery_handoff(sandloom: Path, env: dict[str, str], work: Path) -> None:
    """``sandloom new --with <batteries>`` from the bare CLI hands the missing
    module packages over to ``uv sync`` (one app directory each, side by side)."""

    def new_with(batteries: str) -> subprocess.CompletedProcess[str]:
        app = "probe_" + batteries.replace(",", "_")
        return _run(
            str(sandloom),
            "new",
            app,
            "--with",
            batteries,
            "--dir",
            str(work),
            env=env,
            cwd=work,
            capture_output=True,
        )

    batteries = [p.module for p in release_packages.standard_set() if p.module]
    every = (*batteries, ",".join(batteries))
    with ThreadPoolExecutor(max_workers=len(every)) as pool:
        created_apps = list(pool.map(new_with, every))
    for chosen, created in zip(every, created_apps, strict=True):
        if "ModuleLoadError" in created.stdout or "run `uv sync`" not in created.stdout:
            raise RuntimeError(
                f"bare CLI with {chosen}: unclear dependency handoff:\n{created.stdout}"
            )
        if created.stdout.index("uv run sandloom generate") > created.stdout.index(
            "uv run sandloom test"
        ):
            raise RuntimeError("initial generation must precede the app's CI gate")


def check_lean_install(temp: Path, wheel: Path) -> None:
    """``pip install sandloom`` alone: every CLI entry point works."""
    venv = temp / "lean"
    _run("uv", "venv", str(venv))
    # --compile-bytecode: as `pip install` does; else every cold CLI process
    # below compiles the same modules at once.
    _run(
        "uv", "pip", "install", "--compile-bytecode",
        "--python", str(_python(venv)), str(wheel),
    )  # fmt: skip
    installed = json.loads(
        _run(
            "uv", "pip", "list", "--python", str(_python(venv)), "--format", "json",
            capture_output=True,
        ).stdout
    )  # fmt: skip
    names = {str(item["name"]).lower() for item in installed}
    leaked = names & {
        "redis",
        "celery",
        # The `postgres` extra: the kernel serves SQLite without it.
        "asyncpg",
        "sentry-sdk",
        # The `otel` extra: imported only when TRACING_URL is set.
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
        "opentelemetry-exporter-otlp-proto-grpc",
        # Every other distribution of the list: extras and batteries only.
        *(p.name for p in release_packages.PACKAGES if p.kind != "kernel"),
    }
    if leaked:
        raise RuntimeError(
            f"optional distributions in a lean install: {sorted(leaked)}"
        )
    env = _smoke_environment()
    work = temp / "work"
    work.mkdir()
    sandloom = _script(venv, "sandloom")
    commands = _subcommands(sandloom, env, work)
    if len(commands) < 20:
        raise RuntimeError(f"too few subcommands found: {commands}")

    def help_failure(command: str) -> str | None:
        print(f"+ sandloom {command} --help", flush=True)
        done = subprocess.run(
            [str(sandloom), command, "--help"],
            env=env,
            cwd=work,
            text=True,
            capture_output=True,
            check=False,
        )
        if not done.returncode:
            return None
        tail = "\n    ".join((done.stderr or done.stdout).strip().splitlines()[-4:])
        return f"sandloom {command} --help (exit {done.returncode}):\n    {tail}"

    # Each --help is a cold interpreter: run them side by side (one per CPU),
    # the failures reported in the subcommand order.
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        broken = [f for f in pool.map(help_failure, commands) if f is not None]
    if broken:
        raise RuntimeError("lean install: subcommands fail:\n  " + "\n  ".join(broken))
    # The rest of the lean checks are independent of each other (their own
    # directories under `work`, the RPC smoke in its own temporary one): side
    # by side; the first failure, in this order, is raised after all ran.
    with ThreadPoolExecutor(max_workers=6) as pool:
        lean_checks: list[Future[Any]] = [
            pool.submit(_check_docs, sandloom, env, work),
            pool.submit(inspection_smoke.check, _python(venv), work),
            pool.submit(_check_new_app, sandloom, env, work),
            pool.submit(_check_battery_handoff, sandloom, env, work),
            pool.submit(_check_kernel_factory, venv, env, work),
            # The internal-RPC transport from the installed lean wheel: aiohttp
            # probe + live call, then the httpx fallback on an internals mismatch.
            pool.submit(
                _run,
                sys.executable,
                str(BACKEND_ROOT / "tools/packaging/check_wheel_rpc_transport.py"),
                "--python",
                str(_python(venv)),
            ),
        ]
    for check in lean_checks:
        check.result()


def _check_docs(sandloom: Path, env: dict[str, str], work: Path) -> None:
    for name in DOCS_NAMES:
        shown = _run(
            str(sandloom), "docs", name, env=env, cwd=work, capture_output=True
        )
        if name.rsplit(".", 1)[-1] not in shown.stdout:
            raise RuntimeError(f"`sandloom docs {name}` printed:\n{shown.stdout}")


def _check_new_app(sandloom: Path, env: dict[str, str], work: Path) -> None:
    _run(str(sandloom), "version", env=env, cwd=work)
    _run(str(sandloom), "new", "probe", "--dir", str(work), env=env, cwd=work)
    pyproject = (work / "probe" / "pyproject.toml").read_text()
    if '"sandloom' not in pyproject:
        raise RuntimeError("the generated app does not depend on sandloom")


def _check_kernel_factory(venv: Path, env: dict[str, str], work: Path) -> None:
    _run(
        str(_python(venv)),
        "-c",
        # Every driver module the kernel wheel ships imports without its
        # client library; one that moved to a driver package is not in the
        # wheel, so it is not imported here.
        "import importlib, pkgutil; "
        "import sandloom, sandloom.sdk, sandloom.core.drivers as drivers; "
        "[importlib.import_module(f'{drivers.__name__}.{m.name}') "
        "for m in pkgutil.iter_modules(drivers.__path__)]; "
        # The kernel names no driver and never ``memory``, and a lean install
        # has no composition that selects one: the factory and the lean root
        # refuse at startup (SANDLOOM-B037), they do not boot without a cache.
        "from sandloom.core.app_factory import create_app\n"
        "from sandloom.core.error_codes import code_for_exception\n"
        "def refused(build):\n"
        "    try:\n"
        "        build()\n"
        "    except BaseException as exc:\n"
        "        assert code_for_exception(exc) == 'SANDLOOM-B037', repr(exc)\n"
        "        return\n"
        "    raise AssertionError('a lean install must refuse to boot: B037')\n"
        "refused(create_app)\n"
        "refused(lambda: importlib.import_module('sandloom.main'))\n"
        "print('lean sandloom OK: CLI, kernel factory, lean root refused: B037')",
        cwd=work,
        env={**env, "SANDLOOM_MODULES": "[]"},
    )


#: An application that composes no module, on SQLite: what a lean install's
#: test harness (``sandloom.core.testing.plugin``) runs when no driver package
#: is installed.
_HARNESS_APP = {
    "pyproject.toml": (
        "[tool.pytest.ini_options]\n"
        'pythonpath = ["."]\n'
        'addopts = ["-p", "sandloom.core.testing.plugin", "-p", "no:cacheprovider"]\n'
        'asyncio_mode = "auto"\n'
        'sandloom_composition_root = "fakeroot"\n'
    ),
    "fakeroot.py": (
        "class _Core:\n"
        "    loaded_modules = []\n\n"
        "    def get_service(self, name):\n"
        "        return None\n\n\n"
        "class _State:\n"
        "    core = _Core()\n\n\n"
        "class _App:\n"
        "    state = _State()\n"
        "    dependency_overrides = {}\n\n\n"
        "app = _App()\n"
    ),
    ".env.test": (
        "ENVIRONMENT=test\n"
        "JWT_SECRET=harness_probe_secret_value_long_enough_0123\n"
        "DATABASE_URL=sqlite+aiosqlite:///{db}\n"
        'INFRA=\'{"cache":{"default":{"url":"redis://127.0.0.1:1/0"}}}\'\n'
    ),
    "test_lean_harness.py": (
        "import os\n"
        "import sys\n\n"
        "from sandloom.core.testing.families import harness_families\n\n\n"
        "def test_a_worker_has_its_own_database_and_no_family():\n"
        '    worker = os.environ.get("PYTEST_XDIST_WORKER", "")\n'
        '    assert worker.startswith("gw"), worker\n'
        '    assert os.environ["DATABASE_URL"].endswith(f"_{worker}.db")\n'
        "    assert harness_families() == ()\n"
        '    assert "redis://127.0.0.1:1/0" in os.environ["INFRA"]\n\n\n'
        "def test_no_redis_module_is_loaded():\n"
        '    assert not [m for m in sys.modules if m.split(".")[0] in '
        '("redis", "sandloom_redis", "sandloom_celery")]\n'
    ),
}


def check_lean_test_harness(temp: Path, wheel: Path) -> None:
    """The kernel's test harness (``sandloom.core.testing.plugin``) installed
    with the kernel wheel alone and its ``testing`` and ``sqlite`` extras: no
    Redis client and no driver package, so no harness family. The plugin loads,
    and two xdist workers each run on their own SQLite database. Run in the
    wheel as installed, so a harness module that named a driver package or
    the Redis client at import (or whose lookup of one fails) fails here."""
    venv = temp / "lean-harness"
    _run("uv", "venv", str(venv))
    _run(
        "uv", "pip", "install", "--python", str(_python(venv)),
        f"sandloom[testing,sqlite] @ {wheel.as_uri()}",
    )  # fmt: skip
    probe = _run(
        str(_python(venv)),
        "-c",
        "import importlib.util as u, sys; "
        "print([m for m in ('redis', 'sandloom_redis', 'sandloom_celery') "
        "if u.find_spec(m)])",
        capture_output=True,
    )
    if probe.stdout.strip() != "[]":
        raise RuntimeError(f"lean test harness venv has Redis: {probe.stdout}")
    app = temp / "lean-harness-app"
    app.mkdir()
    for name, text in _HARNESS_APP.items():
        (app / name).write_text(text.replace("{db}", str(app / "run_test.db")))
    env = {
        k: v
        for k, v in _smoke_environment().items()
        if k not in {"DATABASE_URL", "INFRA", "JWT_SECRET"}
        and not k.startswith("PYTEST_")
    }
    done = _run(
        str(_python(venv)), "-m", "pytest", "-q", "-n", "2",
        cwd=app, env=env, capture_output=True, check=False,
    )  # fmt: skip
    if done.returncode or "2 passed" not in done.stdout:
        raise RuntimeError(
            "lean install: the test harness does not run without Redis:\n"
            f"{done.stdout[-1500:]}\n{done.stderr[-800:]}"
        )


def _standard_third_party() -> list[str]:
    """Third-party distributions the kernel's ``standard`` extra pulls in.

    Read from the kernel's ``pyproject.toml`` (nested ``sandloom[...]`` extras
    followed; the workspace's own distributions skipped), so the check names no
    client library: what the extra requires is what must import."""
    extras = tomllib.loads((PACKAGE_ROOT / KERNEL / "pyproject.toml").read_text())[
        "project"
    ]["optional-dependencies"]
    found: set[str] = set()
    seen: set[str] = set()
    pending = ["standard"]
    while pending:
        extra = pending.pop()
        if extra in seen:
            continue
        seen.add(extra)
        for requirement in extras[extra]:
            name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            if name is None:
                raise RuntimeError(f"unreadable requirement: {requirement!r}")
            canonical = re.sub(r"[-_.]+", "-", name.group()).lower()
            if canonical == KERNEL:
                inner = re.search(r"\[([^\]]*)\]", requirement)
                pending += (
                    [e.strip() for e in inner.group(1).split(",")] if inner else []
                )
            elif canonical not in release_packages.NAMES:
                found.add(canonical)
    return sorted(found)


def check_standard_install(temp: Path, wheels: dict[str, Path]) -> None:
    """``sandloom[standard]``: the standard root composes the module entry points.

    ``sandloom_standard.main`` (what ``SANDLOOM_COMPOSITION_ROOT`` names in the
    image and the profiles) builds the three standard modules, and the kernel's
    composition resolution returns that root's core."""
    venv = temp / "standard"
    _run("uv", "venv", str(venv))
    kernel = wheels[KERNEL]
    members = release_packages.standard_set()
    root = release_packages.of_kind("root")[0]
    loaded = sorted(p.import_name for p in members if p.kind == "module")
    third_party = _standard_third_party()
    driver_modules = sorted(
        {
            spec["target"].split(":")[0]
            for p in members
            for spec in (p.drivers or {}).values()
        }
    )
    _run(
        "uv", "pip", "install", "--compile-bytecode", "--python", str(_python(venv)),
        f"sandloom[standard] @ {kernel.resolve().as_uri()}",
        *(str(wheels[p.name]) for p in members),
    )  # fmt: skip
    _run(
        str(_python(venv)),
        "-c",
        # What the standard set provides must import: the third-party
        # distributions of the kernel's `standard` extra (by their top-level
        # packages) and the modules of standard-set driver packages.
        "import importlib, importlib.metadata as md; "
        "owners = md.packages_distributions(); "
        f"[importlib.import_module(m) for m in {driver_modules!r}]; "
        f"[importlib.import_module(top) for dist in {third_party!r} "
        "for top, owned in owners.items() "
        "if dist in {o.replace('_', '-').replace('.', '-').lower() for o in owned}]; "
        f"missing = [d for d in {third_party!r} if not any(d in "
        "{o.replace('_', '-').replace('.', '-').lower() for o in owned} "
        "for owned in owners.values())]; "
        "assert not missing, missing; "
        f"import {root.import_name}.main as m; "
        "from sandloom.core.composition import load_composition_core; "
        "assert type(m.app).__name__ == 'FastAPI'; "
        f"assert m.core.loaded_modules == {loaded!r}, "
        "m.core.loaded_modules; "
        "assert load_composition_core() is m.core; "
        "print('standard set OK:', m.core.loaded_modules)",
        cwd=temp,
        env={
            **_smoke_environment(),
            "SANDLOOM_COMPOSITION_ROOT": f"{root.import_name}.main",
        },
    )


#: Run by the venv's interpreter in :func:`check_plain_module_install`; argv[1]
#: is the module's import path. The infrastructure probes are replaced by
#: passing ones (no database or cache runs here): what is checked is that the
#: module boots and the probes answer, not the services behind them.
PLAIN_MODULE_SMOKE = """
import asyncio, importlib, importlib.util, sys
import httpx
from sandloom.core.tasks.registry import declared_task
module = sys.argv[1]
try:
    import sandloom.main as m
except Exception as exc:
    # A module that requires a capability whose driver is an extra (identity
    # requires the cache: redis-py) refuses with SANDLOOM-B037, naming the
    # extra; any other failure is one.
    text = str(exc)
    assert "[SANDLOOM-B037]" in text and "pip install 'sandloom[" in text, text
    assert "celery" not in sys.modules, "celery imported on plain sandloom"
    print(f"plain sandloom + {module} refused, SANDLOOM-B037: {text[:300]}")
    raise SystemExit(0)
assert m.core.loaded_modules == [module], m.core.loaded_modules
assert "celery" not in sys.modules, "celery imported on plain sandloom"
async def ok():
    from sandloom.core.descriptors import HealthResult
    return HealthResult(status="ok")
async def probe():
    async with m.app.router.lifespan_context(m.app):
        for name in list(m.core.health_checks):
            m.core.register_health_check(name, ok)
        transport = httpx.ASGITransport(app=m.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return {p: await c.get(p) for p in ("/health", "/ready")}
responses = asyncio.run(probe())
has_tasks = importlib.util.find_spec(module + ".tasks") is not None
# A module that boots here has no unmet required taskqueue (B037 refuses one,
# KB-IC-1), so readiness never probes the queue (KB-IC-3): "unused". Its
# tasks are declared without Celery (checked below).
expected = {"/health": 200, "/ready": 200}
codes = {p: r.status_code for p, r in responses.items()}
assert codes == expected, (codes, responses["/ready"].json())
taskqueue = responses["/ready"].json()["taskqueue_status"]
assert taskqueue == "unused", taskqueue
declared = []
if has_tasks:
    tasks = importlib.import_module(module + ".tasks")
    for task in vars(tasks).values():
        name = getattr(task, "name", None)
        if isinstance(name, str) and declared_task(name) is not None:
            declared.append(name)
    assert declared, f"{module}.tasks declares no task"
assert "celery" not in sys.modules, "celery imported by a module's tasks"
print(f"plain sandloom + {module} OK: {codes}, taskqueue {taskqueue}; "
      f"tasks declared: {len(declared)}")
"""


def check_plain_module_install(
    temp: Path, wheels: dict[str, Path], package: str
) -> None:
    """One module wheel on plain ``sandloom``: boots, probes answer, tasks declare.

    ``/health`` and ``/ready`` are 200 (``taskqueue_status: unused``: a
    module that boots here uses no queue on the request path), and a task of
    a module with a ``tasks.py`` declares its tasks without Celery. A module
    that requires a capability whose driver is not installed here refuses to boot with
    SANDLOOM-B037 naming the extra (KB-IC-1), and nothing else.

    The module's own dependencies only (no ``sandloom`` extra): a module whose
    ``tasks.py`` uses ``sandloom.sdk.tasks`` must load and declare its tasks
    without Celery.
    """
    venv = temp / f"plain-{package}"
    _run("uv", "venv", str(venv))
    _run(
        "uv", "pip", "install", "--compile-bytecode", "--python", str(_python(venv)),
        # The smoke environment names a PostgreSQL database: asyncpg is the
        # kernel's `postgres` extra (no Redis, no Celery).
        f"sandloom[postgres] @ {wheels[KERNEL].resolve().as_uri()}",
        str(wheels[package]),
    )  # fmt: skip
    installed = json.loads(
        _run(
            "uv", "pip", "list", "--python", str(_python(venv)), "--format", "json",
            capture_output=True,
        ).stdout
    )  # fmt: skip
    leaked = {str(item["name"]).lower() for item in installed} & {"celery", "redis"}
    if leaked:
        raise RuntimeError(f"{package} on plain sandloom installs {sorted(leaked)}")
    work = temp / f"plain-{package}-work"
    work.mkdir()
    _run(
        str(_python(venv)), "-c", PLAIN_MODULE_SMOKE, MODULES[package][1],
        cwd=work,
        env={**_smoke_environment(), "HEALTH_CACHE_TTL_SECONDS": "0"},
    )  # fmt: skip


#: Run by the venv's interpreter in :func:`check_sqlite_install`: argv[1] is
#: ``boot`` (the app starts on a SQLite file) or ``refuse`` (a PostgreSQL URL
#: stops the startup).
SQLITE_SMOKE = """
import asyncio, importlib.util, sys
import httpx
from sandloom.core.app_factory import create_app
assert importlib.util.find_spec("asyncpg") is None, "asyncpg is installed"
async def probe():
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get("/health")).status_code
if sys.argv[1] == "boot":
    status = asyncio.run(probe())
    assert status == 200, status
    banned = ("asyncpg", "psycopg2")
    drivers = sorted(m for m in sys.modules if m.split(".")[0] in banned)
    assert not drivers, f"imported at boot: {drivers}"
    print("kernel + sqlite extra booted: /health 200, no PostgreSQL driver imported")
else:
    create_app()
"""

#: The drivers a kernel with no cache, event bus or Redis boots on: the
#: built-in memory ones, selected the way an operator selects them (INFRA).
SQLITE_INFRA = json.dumps(
    {
        "database": {"default": {"driver": "sqlalchemy"}},
        "cache": {"default": {"driver": "memory"}},
        "eventbus": {"default": {"driver": "memory"}},
        "atomic_store": {"security": {"driver": "memory"}},
    }
)


def check_sqlite_install(temp: Path, wheel: Path) -> None:
    """``sandloom[sqlite]`` alone, with no ``postgres`` extra: boots on SQLite.

    The kernel without asyncpg starts on a SQLite file (memory cache, event
    bus and security store: nothing else installed), imports neither asyncpg
    nor psycopg2 doing it, and refuses a PostgreSQL URL naming the extra
    (SANDLOOM-B037).
    """
    venv = temp / "sqlite"
    _run("uv", "venv", str(venv))
    _run(
        "uv", "pip", "install", "--compile-bytecode", "--python", str(_python(venv)),
        f"sandloom[sqlite] @ {wheel.resolve().as_uri()}",
    )  # fmt: skip
    work = temp / "sqlite-work"
    work.mkdir()
    env = {
        **_smoke_environment(),
        "DATABASE_URL": f"sqlite+aiosqlite:///{work / 'app.db'}",
        "INFRA": SQLITE_INFRA,
        "SANDLOOM_MODULES": "[]",
        "SANDLOOM_SERVICE_NAME": "package-check",
    }
    _run(str(_python(venv)), "-c", SQLITE_SMOKE, "boot", cwd=work, env=env)
    refused = _run(
        str(_python(venv)), "-c", SQLITE_SMOKE, "refuse",
        cwd=work,
        env={**env, "DATABASE_URL": SMOKE_ENV["DATABASE_URL"]},
        capture_output=True,
        check=False,
    )  # fmt: skip
    if (
        refused.returncode == 0
        or "pip install 'sandloom[postgres]'" not in refused.stderr
        or "SANDLOOM-B037" not in refused.stderr
    ):
        raise RuntimeError(
            "a PostgreSQL URL without asyncpg must refuse, naming the postgres "
            f"extra (exit {refused.returncode}):\n{refused.stderr[-800:]}"
        )


def check_console_install(
    temp: Path, wheels: dict[str, Path], package: release_packages.Package
) -> None:
    """The console's wheel serves it with no build step, and only when installed."""
    console = package.console or {}
    others = [release_packages.by_name(n) for n in console["with"]]
    root = release_packages.of_kind("root")[0]
    loaded = sorted(p.import_name for p in (package, *others) if p.kind == "module")
    venv = temp / f"console-{package.name}"
    _run("uv", "venv", str(venv))
    _run(
        "uv",
        "pip",
        "install",
        "--compile-bytecode",
        "--python",
        str(_python(venv)),
        f"sandloom[{console['extra']}] @ {wheels[KERNEL].resolve().as_uri()}",
        # identity issues tokens: it boots only under a root that carries its
        # trust map (SANDLOOM-B035), the standard root in this wheel.
        *(str(wheels[p.name]) for p in (*others, package)),
    )
    _run(
        str(_python(venv)),
        "-c",
        f"from pathlib import Path; import {package.import_name}; "
        f"console = Path({package.import_name}.__file__).parent / 'console'; "
        "assert (console / 'index.html').is_file(); "
        "assert (console / 'third-party-licenses.txt').is_file(); "
        f"assert not (console / '{package.module}-manifest.json').exists(); "
        f"import {root.import_name}.main as m; "
        f"assert m.core.loaded_modules == {loaded!r}, "
        "m.core.loaded_modules; "
        "assert any(getattr(r, 'path', '') == "
        f"{console['route']!r} for r in m.app.routes); "
        f"print('{package.module} console OK:', m.core.loaded_modules)",
        cwd=temp,
        env=_smoke_environment(),
    )


def check_driver_install(
    temp: Path, wheels: dict[str, Path], package: release_packages.Package
) -> None:
    """Install the driver with the kernel wheel and load its entry points."""
    venv = temp / f"driver-{package.name}"
    _run("uv", "venv", str(venv))
    _run(
        "uv", "pip", "install", "--python", str(_python(venv)),
        str(wheels[KERNEL]), str(wheels[package.name]),
    )  # fmt: skip
    for name, driver in (package.drivers or {}).items():
        _run(
            str(_python(venv)), "-c",
            "from importlib.metadata import entry_points; "
            f"from sandloom.driverkit import {driver['provider']}; "
            f"(driver,) = entry_points(group='sandloom.drivers', name={name!r}); "
            f"assert issubclass(driver.load(), {driver['provider']})",
            cwd=temp,
            env=_smoke_environment(),
        )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install", action="store_true", help="build and layering only"
    )
    parser.add_argument(
        "--dist-dir", type=Path, help="preserve the exact fully validated archives"
    )
    args = parser.parse_args(argv)
    _run(sys.executable, str(BACKEND_ROOT / "tools/packaging/sync_release.py"))
    if args.dist_dir is not None and args.skip_install:
        parser.error("--dist-dir requires the installed-package checks")
    with tempfile.TemporaryDirectory(prefix="sandloom-package-check-") as raw:
        temp = Path(raw)
        wheels = build(temp)
        archives = sorted(
            [*(temp / "wheels").glob("*.whl"), *(temp / "wheels").glob("*.tar.gz")]
        )
        checksums = {
            file.name: hashlib.sha256(file.read_bytes()).hexdigest()
            for file in archives
        }
        check_layering(wheels)
        if not args.skip_install:
            # Independent clean environments: checked side by side; the first
            # failure (in this order) is raised once all of them finished.
            with ThreadPoolExecutor(max_workers=len(PACKAGES) + 2) as pool:
                checks = [
                    pool.submit(check_lean_install, temp, wheels[KERNEL]),
                    pool.submit(check_lean_test_harness, temp, wheels[KERNEL]),
                    pool.submit(check_sqlite_install, temp, wheels[KERNEL]),
                    pool.submit(check_standard_install, temp, wheels),
                    *(
                        pool.submit(check_console_install, temp, wheels, p)
                        for p in release_packages.PACKAGES
                        if p.console
                    ),
                    *(
                        pool.submit(check_driver_install, temp, wheels, p)
                        for p in release_packages.of_kind("driver")
                    ),
                    *(
                        pool.submit(check_plain_module_install, temp, wheels, p)
                        for p in MODULES
                    ),
                ]
            for check in checks:
                check.result()
        if args.dist_dir is not None:
            if any(
                hashlib.sha256(file.read_bytes()).hexdigest() != checksums[file.name]
                for file in archives
            ):
                raise RuntimeError("Package archives changed during validation")
            args.dist_dir.mkdir(parents=True, exist_ok=False)
            for file in archives:
                shutil.copyfile(file, args.dist_dir / file.name)
            (args.dist_dir / "sha256.json").write_text(
                json.dumps(checksums, indent=2) + "\n"
            )
    print("All package distribution checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
