"""Build-time hardening of the Debian runtime; never removes essential packages."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ADMIN_PACKAGES = frozenset({"apt", "libapt-pkg7.0", "mount", "passwd", "adduser"})


def packages_to_remove(records: str) -> list[str]:
    selected = []
    for line in records.splitlines():
        package, essential, protected = line.split("\t")
        name = package.partition(":")[0]
        if name not in ADMIN_PACKAGES:
            continue
        if essential == "yes" or protected == "yes":
            raise ValueError(f"Refusing essential or protected package: {package}")
        selected.append(name)
    return selected


def strip_privileged_bits(roots: tuple[Path, ...]) -> None:
    for root in roots:
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                    path.chmod(stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID))


def main() -> None:
    records = subprocess.check_output(
        [
            "/usr/bin/dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Essential}\t${Protected}\n",
        ],
        text=True,
    )
    packages = packages_to_remove(records)
    if set(packages) != ADMIN_PACKAGES:
        raise ValueError("Review hardening before changing the Debian base")
    if packages:
        # dpkg respects dependency and essential/protected guards. No force
        # flags or autoremove: native Python extensions can need libstdc++.
        subprocess.run(
            [
                "/usr/bin/dpkg",
                "--purge",
                "apt",
                "libapt-pkg7.0",
                "mount",
                "adduser",
                "passwd",
            ],
            check=True,
        )
    strip_privileged_bits((Path("/usr"), Path("/opt")))
    print("Runtime admin packages removed:", ", ".join(packages))


if __name__ == "__main__":
    main()
