"""Run inside a Linux runtime image: inventory and enforce its security boundary.

Reads package metadata and code/component presence without loading Sandloom or
application settings. --enforce additionally exercises the intended write paths.
This is evidence, not a CVE ignore policy or an exploitability certification.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path

TOOLS = (
    "apt",
    "apt-get",
    "apt-key",
    "mount",
    "umount",
    "useradd",
    "passwd",
    "getfattr",
    "setfattr",
    "getfacl",
    "setfacl",
    "chacl",
    "bzip2recover",
    "systemd",
    "systemd-homed",
    "systemd-oomd",
    "systemd-journald",
    "journalctl",
    "systemd-tmpfiles",
    "nscd",
    "infocmp",
    "tic",
    "diff3",
    "perl",
    "nsenter",
    "chfn",
    "chsh",
    "newuidmap",
)


def privileged_files() -> list[str]:
    """List regular SUID/SGID files without following directory or file links."""
    found = []
    for root in ("/usr", "/opt"):
        for directory, _, files in os.walk(root, followlinks=False):
            for name in files:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                    found.append(str(path))
    return sorted(found)


def inventory() -> dict:
    """Collect installed components and the current process's runtime boundary."""
    rows = subprocess.check_output(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${source:Package}\t${db:Status-Status}\n",
        ],
        text=True,
    )
    packages = []
    for line in rows.splitlines():
        name, version, source, status = line.split("\t")
        if status == "installed":
            packages.append({"name": name, "version": version, "source": source})
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        separator = fields.index("-")
        if fields[4] not in ("/", "/tmp", "/app/data"):
            continue
        mounts.append(
            {
                "path": fields[4],
                "options": fields[5].split(","),
                "filesystem": fields[separator + 1],
                "super_options": [
                    o
                    for o in fields[separator + 3].split(",")
                    if o.startswith(("size=", "mode=", "uid=", "gid="))
                ],
            }
        )
    symbols = {}
    for library, names in {
        "z": ["gzwrite", "gzprintf", "crc32_combine"],
        "sqlite3": ["sqlite3changeset_apply_v3", "sqlite3changeset_apply_v2"],
    }.items():
        selected = ctypes.util.find_library(library)
        if not selected:
            raise RuntimeError(f"Expected runtime library missing: {library}")
        loaded = ctypes.CDLL(selected)
        symbols[library] = {name: hasattr(loaded, name) for name in names}
    hashes = {}
    for line in Path("/proc/self/maps").read_text().splitlines():
        path = line.split()[-1]
        if path.startswith("/") and ("/libz.so." in path or "/libsqlite3.so." in path):
            hashes[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    import zlib

    systemd_paths = [
        "/usr/lib/systemd/systemd",
        "/lib/systemd/systemd",
        "/usr/lib/systemd/systemd-homed",
        "/usr/lib/systemd/systemd-oomd",
        "/usr/lib/systemd/systemd-journald",
    ]
    sqlite = sqlite3.connect(":memory:")
    sqlite_modules = [r[0] for r in sqlite.execute("pragma module_list")]
    sqlite_options = [r[0] for r in sqlite.execute("pragma compile_options")]
    sqlite.close()
    provenance = {}
    for name in ("chfn", "chsh"):
        path = shutil.which(name)
        if path:
            owner = subprocess.check_output(
                ["dpkg-query", "-S", path], text=True
            ).split(": ")[0]
            provenance[name] = subprocess.check_output(
                ["dpkg-query", "-W", "-f=${source:Package}", owner], text=True
            )
        else:
            provenance[name] = None
    perl_roots = []
    if shutil.which("perl"):
        perl_roots = subprocess.check_output(
            ["perl", "-e", 'print join("\\n", @INC)'], text=True
        ).splitlines()
    return {
        "schema": 1,
        "machine": platform.machine(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "no_new_privileges": int(status["NoNewPrivs"].strip()),
        "cap_effective": int(status["CapEff"].strip(), 16),
        "cap_bounding": int(status["CapBnd"].strip(), 16),
        "mounts": mounts,
        "privileged_files": privileged_files(),
        "tools": {name: shutil.which(name) for name in TOOLS},
        "packages": packages,
        "symbols": symbols,
        "library_sha256": hashes,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_compile_options": sqlite_options,
        "sqlite_modules": sqlite_modules,
        "systemd_paths": {p: Path(p).exists() for p in systemd_paths},
        "sqlite_zipfile_extensions": [
            str(p)
            for base in ("/usr/lib", "/usr/local/lib", "/opt/venv")
            for p in Path(base).rglob("*zipfile*.so")
        ],
        "tool_source_packages": provenance,
        "perl_modules": {
            name: any((Path(root) / name).is_file() for root in perl_roots)
            for name in ["Archive/Tar.pm", "Pod/Text.pm", "File/Temp.pm"]
        },
    }


def enforce(data: dict) -> None:
    """Exercise writable storage and reject privilege or filesystem escapes."""
    checks = {
        "nonroot UID 10001": data["uid"] == 10001,
        "no-new-privileges": data["no_new_privileges"] == 1,
        "no effective or bounding capabilities": data["cap_effective"]
        == data["cap_bounding"]
        == 0,
        "no SUID/SGID files": not data["privileged_files"],
        "read-only root": any(
            m["path"] == "/" and "ro" in m["options"] for m in data["mounts"]
        ),
        "bounded private non-executable tmpfs": any(
            m["path"] == "/tmp"
            and m["filesystem"] == "tmpfs"
            and {"nosuid", "nodev", "noexec"} <= set(m["options"])
            and any(o.startswith("size=") for o in m["super_options"])
            for m in data["mounts"]
        ),
        "runtime admin tools absent": all(
            data["tools"][name] is None
            for name in ["apt", "apt-get", "mount", "useradd", "passwd"]
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("Runtime boundary failed: " + ", ".join(failed))
    for directory in ("/tmp", "/app/data"):
        with tempfile.NamedTemporaryFile(dir=directory) as handle:
            handle.write(b"runtime-boundary")
            handle.flush()
    path = Path("/app/.runtime-write-probe")
    try:
        path.write_bytes(b"forbidden")
    except OSError as error:
        if error.errno != errno.EROFS:
            raise RuntimeError("Expected a read-only filesystem error") from error
    else:
        path.unlink()
        raise RuntimeError("Application root is writable")
    with tempfile.NamedTemporaryFile(dir="/tmp") as handle:
        handle.write(b"#!/bin/sh\nexit 0\n")
        handle.flush()
        os.chmod(handle.name, 0o700)
        try:
            subprocess.run([handle.name], check=True)
        except OSError as error:
            if error.errno != errno.EACCES:
                raise
        else:
            raise RuntimeError("/tmp permits execution")
    data["boundary_checks"] = "pass"


def main() -> int:
    """Emit inventory even when boundary enforcement fails, with a failing exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()
    data = inventory()
    result = 0
    if args.enforce:
        try:
            enforce(data)
        except Exception as error:
            # The CLI preserves collected evidence; failures still stop CI.
            data["boundary_checks"] = "fail"
            data["boundary_failure"] = f"{type(error).__name__}: {error}"
            result = 1
    print(json.dumps(data, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
