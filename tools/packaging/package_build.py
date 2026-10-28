"""Build the distributions from one source tree, excluding runtime tests.

The repository owns source files; package projects own distribution metadata.
``[tool.sandloom.sources]`` maps source paths to wheel paths and
``[tool.sandloom.exclude] paths`` drops wheel paths (repository-only files).
An sdist is self-contained and rebuilds with Hatch alone. Editable installs
put ``packages/editable/<distribution>`` on ``sys.path``: one symlink to the
distribution's own import package, without copied runtime files and without
exposing another distribution's package.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _excluded(target: Path, excluded: list[Path]) -> bool:
    return any(target == path or path in target.parents for path in excluded)


class CustomBuildHook(BuildHookInterface):
    _temporary: tempfile.TemporaryDirectory[str] | None = None

    def _config(self) -> dict:
        root = Path(self.root)
        return tomllib.loads((root / "pyproject.toml").read_text())["tool"]["sandloom"]

    def _runtime_files(self) -> dict[str, str]:
        root = Path(self.root)
        config = self._config()
        excluded = [Path(p) for p in config.get("exclude", {}).get("paths", [])]
        files: dict[str, str] = {}
        for origin, destination in config["sources"].items():
            source = (root / origin).resolve()
            paths = [source] if source.is_file() else sorted(source.rglob("*"))
            for path in paths:
                if not path.is_file():
                    continue
                relative = Path() if source.is_file() else path.relative_to(source)
                if set(relative.parts) & {"tests", "__pycache__", ".pytest_cache"}:
                    continue
                if path.suffix in {".pyc", ".pyo"} or path.name.endswith(".bak"):
                    continue
                target = (
                    Path(destination)
                    if source.is_file()
                    else Path(destination) / relative
                )
                if _excluded(target, excluded):
                    continue
                files[str(path)] = target.as_posix()
        return files

    def _top_level(self) -> list[str]:
        """The import packages this distribution ships (its source roots)."""
        return sorted(
            {Path(dest).parts[0] for dest in self._config()["sources"].values()}
        )

    def initialize(self, version: str, build_data: dict) -> None:
        # Keep standalone package notices aligned with the canonical root copies.
        # Sdists retain local copies and remove this monorepo-only hook below.
        repository = Path(__file__).resolve().parents[2]
        for filename in ("LICENSE", "NOTICE"):
            if (Path(self.root) / filename).read_bytes() != (
                repository / filename
            ).read_bytes():
                raise ValueError(
                    f"{Path(self.root).name}/{filename} differs from the root copy; "
                    "synchronize the package license files before building"
                )
        if self.target_name == "wheel" and version == "editable":
            # The .pth (dev-mode-dirs) serves the live tree; hatch still needs
            # one selected path, an empty directory named like the package.
            self._temporary = tempfile.TemporaryDirectory(prefix="sandloom-editable-")
            build_data["force_include_editable"] = {
                self._temporary.name: self._top_level()[0]
            }
            return
        build_data.setdefault("force_include", {}).update(self._runtime_files())
        if self.target_name != "sdist":
            return
        source = (Path(self.root) / "pyproject.toml").read_text()
        # The extracted source has its own package tree and no monorepo hook.
        packages = ", ".join(f'"{name}"' for name in self._top_level())
        source = re.sub(
            r"(?ms)^\[tool\.hatch\.build\.targets\.wheel\]\n.*?(?=^\[|\Z)",
            f"[tool.hatch.build.targets.wheel]\npackages = [{packages}]\n\n",
            source,
        )
        source = re.sub(
            r"(?ms)^\[tool\.hatch\.build\.hooks\.custom\]\n.*?(?=^\[|\Z)", "", source
        )
        source = re.sub(r"(?ms)^\[tool\.sandloom\.sources\]\n.*?(?=^\[|\Z)", "", source)
        source = re.sub(r"(?ms)^\[tool\.sandloom\.exclude\]\n.*?(?=^\[|\Z)", "", source)
        # A comment block right before a removed table would dangle.
        source = re.sub(r"(?m)(^#[^\n]*\n)+(?=\Z)", "", source).rstrip() + "\n"
        self._temporary = tempfile.TemporaryDirectory(prefix="sandloom-sdist-")
        project = Path(self._temporary.name) / "pyproject.toml"
        project.write_text(source)
        build_data["force_include"][str(project)] = "pyproject.toml"

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
