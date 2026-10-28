"""Build same-patch official/PBS controls from an exported cb278f8 source tree."""

import argparse
import hashlib
import subprocess
from pathlib import Path

PIN = "cb278f8da77bfa9f235b16c8df92236bb6b1b2c5"


def official_dockerfile(source):
    if "ARG PYTHON_VERSION=3.12.15" not in source:
        raise ValueError(
            "this control pins CPython 3.12.15; update both variants deliberately"
        )
    start = source.index("FROM ${BASE_IMAGE} AS python")
    end = source.index("# ---------- Stage 1:")
    control = """FROM python:3.12.15-slim-trixie AS python
# Same patch and Debian release, identical deps/project/runtime stages.
RUN mkdir -p /opt/python && ln -s /usr/local /opt/python/current \\
    && python3 -m compileall -q -j 0 /usr/local/lib/python3.12

"""
    return source[:start] + control + source[end:]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    original = (a.source / "deploy/docker/Dockerfile").read_text()
    control = a.out / "Dockerfile.official"
    control.write_text(official_dockerfile(original))
    print(
        "control_sha256", hashlib.sha256(control.read_bytes()).hexdigest(), flush=True
    )
    for variant, dockerfile in (
        ("official", control),
        ("pbs", a.source / "deploy/docker/Dockerfile"),
    ):
        with (a.out / f"build-{variant}.log").open("w") as log:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--target",
                    "production",
                    "--label",
                    "org.opencontainers.image.revision=" + PIN,
                    "-t",
                    f"sandloom-perf:cb278f8-{variant}",
                    "-f",
                    str(dockerfile.resolve()),
                    str((a.source).resolve()),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        print("built", variant, flush=True)


if __name__ == "__main__":
    main()
