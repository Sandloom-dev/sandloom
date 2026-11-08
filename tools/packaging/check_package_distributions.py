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
   smoke runs from the installed wheel;
4. ``sandloom[standard]`` (every wheel but the console): the standard root
   ``sandloom_standard.main:app`` (the ``sandloom-standard`` distribution, the
   one ``SANDLOOM_COMPOSITION_ROOT`` names in the image and the profiles)
   composes the installed ``sandloom.modules`` entry points;
5. each module wheel on plain ``sandloom`` (no Celery, no Redis client): the
   app boots with the module loaded, ``/health`` and ``/ready`` answer 200
   over stubbed infrastructure probes, and enqueueing one of its tasks
   refuses with ``TaskQueueNotInstalled`` (KB-01).

``--skip-install`` runs steps 1-2 only (no network).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import distribution_imports
import inspection_smoke

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "packages"
PACKAGES = (
    "sandloom",
    "sandloom-standard",
    "sandloom-identity",
    "sandloom-tenancy",
    "sandloom-billing",
    "sandloom-admin",
)
#: Module packages and their ``sandloom.modules`` entry-point names.
MODULES = {
    "sandloom-identity": ("identity", "sandloom_identity"),
    "sandloom-tenancy": ("tenancy", "sandloom_tenancy"),
    "sandloom-billing": ("billing", "sandloom_billing"),
    "sandloom-admin": ("admin", "sandloom_admin"),
}
#: Third-party distributions a module wheel may import through ``sandloom``
#: without declaring them: the stack the kernel promises modules (routes,
#: models, migrations, schemas, outbound HTTP), kept within the kernel's
#: ranges. Every other kernel requirement (argon2-cffi, cryptography, redis,
#: …) is the kernel's implementation: a module importing it declares it.
KERNEL_PROVIDES = ("fastapi", "starlette", "sqlalchemy", "alembic", "pydantic", "httpx")
SMOKE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
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
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        check=True,
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
    names = [n for n in _contents(wheel) if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in names}
    expected = {package.replace("-", "_")}
    if tops != expected:
        raise RuntimeError(f"{wheel.name}: ships {sorted(tops)}, expected {expected}")
    leaked = [n for n in names if "/tests/" in n or n.endswith((".pyc", ".bak"))]
    if leaked:
        raise RuntimeError(f"{wheel.name}: ships repository-only files {leaked[:5]}")
    if package == "sandloom":
        # SANDLOOM-C012: the generated copies of the standard modules' contract
        # DTOs, their stubs and event schemas ship with the kernel, so every
        # caller process has them.
        for required in (
            "sandloom/shared/contracts/identity_dto.py",
            "sandloom/shared/contracts/tenancy_dto.py",
            "sandloom/shared/contracts/tenancy_membership_v1.py",
            "sandloom/shared/events/identity.user_registered.v1.json",
        ):
            if required not in names:
                raise RuntimeError(f"{wheel.name}: does not ship {required}")
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
        first_party=[
            "sandloom",
            "sandloom_standard",
            *(path for _n, path in MODULES.values()),
        ],
        provides={"sandloom": KERNEL_PROVIDES},
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

    every = ("identity", "tenancy", "billing", "identity,tenancy,billing")
    with ThreadPoolExecutor(max_workers=len(every)) as pool:
        created_apps = list(pool.map(new_with, every))
    for batteries, created in zip(every, created_apps, strict=True):
        if "ModuleLoadError" in created.stdout or "run `uv sync`" not in created.stdout:
            raise RuntimeError(
                f"bare CLI with {batteries}: unclear dependency handoff:\n{created.stdout}"
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
        "sentry-sdk",
        "sandloom-identity",
        "sandloom-standard",
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
        lean_checks = [
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
        "import sandloom, sandloom.sdk, sandloom.core.drivers.bootstrap; "
        "import sandloom.core.drivers.redis_cache; "
        "import sandloom.core.drivers.celery_taskqueue; "
        "import sandloom.core.drivers.event_bus_driver; "
        "from sandloom.core.app_factory import create_app; "
        "assert type(create_app()).__name__ == 'FastAPI'; "
        "import sandloom.main as m; assert m.core.loaded_modules == []; "
        "print('lean sandloom OK: CLI, kernel factory, empty lean root')",
        cwd=work,
        env={**env, "SANDLOOM_MODULES": "[]"},
    )


def check_standard_install(temp: Path, wheels: dict[str, Path]) -> None:
    """``sandloom[standard]``: the standard root composes the module entry points.

    ``sandloom_standard.main`` (what ``SANDLOOM_COMPOSITION_ROOT`` names in the
    image and the profiles) builds the three standard modules, and the kernel's
    composition resolution returns that root's core."""
    venv = temp / "standard"
    _run("uv", "venv", str(venv))
    kernel = wheels["sandloom"]
    _run(
        "uv", "pip", "install", "--compile-bytecode", "--python", str(_python(venv)),
        f"sandloom[standard] @ {kernel.resolve().as_uri()}",
        *(str(w) for p, w in wheels.items() if p not in ("sandloom", "sandloom-admin")),
    )  # fmt: skip
    _run(
        str(_python(venv)),
        "-c",
        "import redis, celery; import sandloom_standard.main as m; "
        "from sandloom.core.composition import load_composition_core; "
        "assert type(m.app).__name__ == 'FastAPI'; "
        "assert m.core.loaded_modules == "
        "['sandloom_billing', 'sandloom_identity', 'sandloom_tenancy'], "
        "m.core.loaded_modules; "
        "assert load_composition_core() is m.core; "
        "print('standard set OK:', m.core.loaded_modules)",
        cwd=temp,
        env={
            **_smoke_environment(),
            "SANDLOOM_COMPOSITION_ROOT": "sandloom_standard.main",
        },
    )


#: Run by the venv's interpreter in :func:`check_plain_module_install`; argv[1]
#: is the module's import path. The infrastructure probes are replaced by
#: passing ones (no database or cache runs here): what is checked is that the
#: module boots and the probes answer, not the services behind them.
PLAIN_MODULE_SMOKE = """
import asyncio, importlib, importlib.util, sys
import httpx
from sandloom.sdk._prim.tasks import TaskQueueNotInstalled, _UnqueuedTask
module = sys.argv[1]
import sandloom.main as m
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
            return {p: (await c.get(p)).status_code for p in ("/health", "/ready")}
codes = asyncio.run(probe())
assert codes == {"/health": 200, "/ready": 200}, codes
refused = []
if importlib.util.find_spec(module + ".tasks") is not None:
    tasks = importlib.import_module(module + ".tasks")
    for task in vars(tasks).values():
        if isinstance(task, _UnqueuedTask):
            try:
                task.delay()
            except TaskQueueNotInstalled:
                refused.append(task.name)
            else:
                raise AssertionError(f"{task.name}.delay() did not refuse")
    assert refused, f"{module}.tasks registers no task"
print(f"plain sandloom + {module} OK: /health, /ready 200; enqueue refused:",
      len(refused))
"""


def check_plain_module_install(
    temp: Path, wheels: dict[str, Path], package: str
) -> None:
    """One module wheel on plain ``sandloom``: boots, probes answer, enqueue refuses.

    The module's own dependencies only (no ``sandloom`` extra): a module whose
    ``tasks.py`` uses ``sandloom.sdk.tasks.shared_task`` must load without
    Celery, and its tasks refuse to enqueue with ``TaskQueueNotInstalled``.
    """
    venv = temp / f"plain-{package}"
    _run("uv", "venv", str(venv))
    _run(
        "uv", "pip", "install", "--compile-bytecode", "--python", str(_python(venv)),
        str(wheels["sandloom"]), str(wheels[package]),
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


def check_admin_install(temp: Path, wheels: dict[str, Path]) -> None:
    """The console's wheel serves it with no build step, and only when installed."""
    venv = temp / "admin"
    _run("uv", "venv", str(venv))
    _run(
        "uv",
        "pip",
        "install",
        "--compile-bytecode",
        "--python",
        str(_python(venv)),
        f"sandloom[admin] @ {wheels['sandloom'].resolve().as_uri()}",
        *(str(wheels[name]) for name in ("sandloom-identity", "sandloom-admin")),
    )
    _run(
        str(_python(venv)),
        "-c",
        "from pathlib import Path; import sandloom_admin; "
        "console = Path(sandloom_admin.__file__).parent / 'console'; "
        "assert (console / 'index.html').is_file(); "
        "assert (console / 'third-party-licenses.txt').is_file(); "
        "assert not (console / 'admin-manifest.json').exists(); "
        "import sandloom.main as m; "
        "assert m.core.loaded_modules == ['sandloom_admin', 'sandloom_identity'], "
        "m.core.loaded_modules; "
        "assert any(getattr(r, 'path', '') == '/admin/{path:path}' for r in m.app.routes); "
        "print('admin console OK:', m.core.loaded_modules)",
        cwd=temp,
        env=_smoke_environment(),
    )


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
            with ThreadPoolExecutor(max_workers=3 + len(MODULES)) as pool:
                checks = [
                    pool.submit(check_lean_install, temp, wheels["sandloom"]),
                    pool.submit(check_standard_install, temp, wheels),
                    pool.submit(check_admin_install, temp, wheels),
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
