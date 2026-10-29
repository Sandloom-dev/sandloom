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
4. ``sandloom[standard]`` (every wheel): ``sandloom.main:app`` composes the
   installed ``sandloom.modules`` entry points.

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
from email.parser import BytesParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import distribution_imports

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "packages"
PACKAGES = (
    "sandloom",
    "sandloom-identity",
    "sandloom-tenancy",
    "sandloom-billing",
)
#: Module packages and their ``sandloom.modules`` entry-point names.
MODULES = {
    "sandloom-identity": ("identity", "sandloom_identity"),
    "sandloom-tenancy": ("tenancy", "sandloom_tenancy"),
    "sandloom-billing": ("billing", "sandloom_billing"),
}
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
        raise RuntimeError(f"{wheel.name}: the kernel registers no module")


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
    """sdist + wheel of every package; returns ``{package: rebuilt wheel}``."""
    wheels = out / "wheels"
    wheels.mkdir()
    built: dict[str, Path] = {}
    for package in PACKAGES:
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
        built[package] = rebuilt
    return built


def check_layering(wheels: dict[str, Path]) -> None:
    problems = distribution_imports.check(
        [distribution_imports.read_wheel(w) for w in wheels.values()],
        first_party=["sandloom", *(path for _n, path in MODULES.values())],
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


def check_lean_install(temp: Path, wheel: Path) -> None:
    """``pip install sandloom`` alone: every CLI entry point works."""
    venv = temp / "lean"
    _run("uv", "venv", str(venv))
    _run("uv", "pip", "install", "--python", str(_python(venv)), str(wheel))
    installed = json.loads(
        _run(
            "uv", "pip", "list", "--python", str(_python(venv)), "--format", "json",
            capture_output=True,
        ).stdout
    )  # fmt: skip
    names = {str(item["name"]).lower() for item in installed}
    leaked = names & {"redis", "celery", "sentry-sdk", "sandloom-identity"}
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
    broken: list[str] = []
    for command in commands:
        print(f"+ sandloom {command} --help", flush=True)
        done = subprocess.run(
            [str(sandloom), command, "--help"],
            env=env,
            cwd=work,
            text=True,
            capture_output=True,
            check=False,
        )
        if done.returncode:
            tail = "\n    ".join((done.stderr or done.stdout).strip().splitlines()[-4:])
            broken.append(
                f"sandloom {command} --help (exit {done.returncode}):\n    {tail}"
            )
    if broken:
        raise RuntimeError("lean install: subcommands fail:\n  " + "\n  ".join(broken))
    for name in DOCS_NAMES:
        shown = _run(
            str(sandloom), "docs", name, env=env, cwd=work, capture_output=True
        )
        if name.rsplit(".", 1)[-1] not in shown.stdout:
            raise RuntimeError(f"`sandloom docs {name}` printed:\n{shown.stdout}")
    _run(str(sandloom), "version", env=env, cwd=work)
    _run(str(sandloom), "new", "probe", "--dir", str(work), env=env, cwd=work)
    pyproject = (work / "probe" / "pyproject.toml").read_text()
    if '"sandloom' not in pyproject:
        raise RuntimeError("the generated app does not depend on sandloom")
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
        "print('lean sandloom OK: CLI, kernel factory, empty standard root')",
        cwd=work,
        env={**env, "SANDLOOM_MODULES": "[]"},
    )
    # The internal-RPC transport from the installed lean wheel: aiohttp
    # probe + live call, then the httpx fallback on an internals mismatch.
    _run(
        sys.executable,
        str(BACKEND_ROOT / "tools" / "packaging" / "check_wheel_rpc_transport.py"),
        "--python",
        str(_python(venv)),
    )


def check_standard_install(temp: Path, wheels: dict[str, Path]) -> None:
    """Every wheel (``sandloom[standard]``): the modules compose by entry point."""
    venv = temp / "standard"
    _run("uv", "venv", str(venv))
    kernel = wheels["sandloom"]
    _run(
        "uv", "pip", "install", "--python", str(_python(venv)),
        f"sandloom[standard] @ {kernel.resolve().as_uri()}",
        *(str(w) for p, w in wheels.items() if p != "sandloom"),
    )  # fmt: skip
    _run(
        str(_python(venv)),
        "-c",
        "import redis, celery; import sandloom.main as m; "
        "assert type(m.app).__name__ == 'FastAPI'; "
        "assert m.core.loaded_modules == "
        "['sandloom_billing', 'sandloom_identity', 'sandloom_tenancy'], "
        "m.core.loaded_modules; "
        "print('standard set OK:', m.core.loaded_modules)",
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
            check_lean_install(temp, wheels["sandloom"])
            check_standard_install(temp, wheels)
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
