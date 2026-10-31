#!/usr/bin/env python3
"""Run the documented quickstart end to end in an empty directory (CI job
``quickstart``; ``tools/ci/ci_local.sh <ref> --jobs quickstart`` runs the same).

No manual step: a fix-up this script would need is a bug in the template or
the CLI, never something to add here. The steps:

1. ``uv run --project <repo>/backend sandloom new <app> --framework-source path
   --framework-path <repo>`` in an empty directory (the framework from this
   checkout), as ``docs/getting-started/index.md`` documents;
2. every line ``sandloom new`` prints under "Next steps", verbatim and in order
   (``sandloom.core.cli.project.QUICKSTART`` is their one source, and the docs
   are tested against it):
   ``cd``, ``uv sync``, ``uv run sandloom generate``, the dev services,
   ``uv run sandloom add module <m>``,
   ``uv run sandloom test`` (every step must PASS), ``uv run sandloom migrate
   upgrade-all --apply`` and ``uv run uvicorn <app>.asgi:app --reload``;
   right after ``uv sync``, the app's ``sandloom`` must be this checkout (the
   live tree of the editable sources, or files byte-identical to it): a
   stale cached build of the same version fails here;
3. with the app running: ``GET /health`` answers 200, and a token minted
   with the testing kit (``sandloom.sdk.testing.as_user``, the app's own
   ``JWT_SECRET``) creates and reads an item of the added module;
4. ``uv run sandloom test --ci`` (the generated app's CI gate) — also once on the
   fresh app, after installing dependencies, generating files and starting
   the dev services. ``--cli`` can select a kernel-only CLI to exercise the
   isolated uvx path; ``--profile`` selects any subset of the standard batteries.
   ``--dist-dir`` uses reviewed wheels via find-links and registry requirements,
   verifies installed package versions and bytes, and refuses editable imports.

Services. Without ``--postgres-url`` the dev-services line runs as printed
(``docker compose ... up -d --wait``, the generated compose file). With it
(CI, a shared machine) that line is replaced by the given servers: a fresh
database ``<db-prefix>_dev`` for the app (dropped and recreated, so the
migrations run on an empty database), ``<db-prefix>_test`` for the test
harness (``SANDLOOM_TEST_DATABASE_URL``) and Redis indexes ``--redis-db-base``
.. ``+3`` (app cache/broker, test cache/broker); the test workers allocate
their own indexes (``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``).

    python3 tools/smoke/quickstart_check.py \\
        --postgres-url postgresql://test_user:test_password@localhost:5433 \\
        --redis-url redis://localhost:6380 --redis-db-base 0

Standard library only: it runs before any virtualenv exists.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class QuickstartError(Exception):
    pass


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo: Path = args.repo.resolve()
        self.timings: list[tuple[str, float, str]] = []
        self.env = {
            k: v
            for k, v in os.environ.items()
            # the caller's virtualenv / project must not leak into the app's uv
            if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"}
        }
        self.dist_dir: Path | None = getattr(args, "dist_dir", None)
        if self.dist_dir:
            self.env["UV_FIND_LINKS"] = str(self.dist_dir.resolve())
            self.env["UV_NO_SOURCES"] = "true"
        self.cwd = Path.cwd()
        self.server: subprocess.Popen[bytes] | None = None
        self.base_url = ""
        self.module = ""

    # ------------------------------------------------------------------ util
    def step(self, name: str, fn, *a, **kw):  # type: ignore[no-untyped-def]
        print(f"\n=== quickstart: {name}", flush=True)
        started = time.perf_counter()
        try:
            result = fn(*a, **kw)
        except Exception:
            self.timings.append((name, time.perf_counter() - started, "FAIL"))
            raise
        self.timings.append((name, time.perf_counter() - started, "PASS"))
        return result

    def sh(self, command: str, *, capture: bool = False) -> str:
        print(f"$ {command}   (in {self.cwd})", flush=True)
        proc = subprocess.run(  # noqa: S602 - the documented quickstart line, verbatim
            command,
            shell=True,
            cwd=self.cwd,
            env=self.env,
            check=False,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
        if capture:
            print(proc.stdout, end="", flush=True)
        if proc.returncode != 0:
            raise QuickstartError(f"`{command}` failed (exit {proc.returncode})")
        return proc.stdout or ""

    def python(self, code: str) -> str:
        """Run ``code`` with the app's interpreter (after ``uv sync``)."""
        proc = subprocess.run(  # noqa: S603 - fixed argv; code is this script's own
            ["uv", "run", "--no-sync", "python", "-c", code],  # noqa: S607 - uv from PATH, as the quickstart
            cwd=self.cwd,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise QuickstartError(f"python step failed:\n{proc.stdout}{proc.stderr}")
        return proc.stdout.strip()

    # -------------------------------------------------------------- services
    def configure_services(self) -> None:
        a = self.args
        if not a.postgres_url:
            return
        pg = a.postgres_url.rstrip("/").replace(
            "postgresql+asyncpg://", "postgresql://"
        )
        asyncpg_url = pg.replace("postgresql://", "postgresql+asyncpg://", 1)
        redis = a.redis_url.rstrip("/")
        base = a.redis_db_base
        self.admin_url = f"{pg}/{a.admin_db}"
        self.dev_db = f"{a.db_prefix}_dev"
        self.env.update(
            {
                # the app (migrate, uvicorn, the token): .env's values lose to these
                "DATABASE_URL": f"{asyncpg_url}/{self.dev_db}",
                "CACHE_URL": f"{redis}/{base}",
                "BROKER_URL": f"{redis}/{base + 1}",
                # the tests (sandloom test): applied on top of .env.test
                "SANDLOOM_TEST_DATABASE_URL": f"{asyncpg_url}/{a.db_prefix}_test",
                "SANDLOOM_TEST_CACHE_URL": f"{redis}/{base + 2}",
                "SANDLOOM_TEST_BROKER_URL": f"{redis}/{base + 3}",
            }
        )
        self.env.setdefault("SANDLOOM_TEST_REDIS_DB_STRIDE", "auto")

    def provided_services(self) -> None:
        """The dev-services line, on the given servers: a fresh dev database."""
        dev = self.dev_db
        print(f"(services given: fresh database {dev}; Redis {self.args.redis_url})")
        self.python(
            "import asyncio, asyncpg\n"
            "async def main():\n"
            f"    c = await asyncpg.connect({self.admin_url!r})\n"
            f"    await c.execute('DROP DATABASE IF EXISTS \"{dev}\" WITH (FORCE)')\n"
            f"    await c.execute('CREATE DATABASE \"{dev}\"')\n"
            "    await c.close()\n"
            "asyncio.run(main())\n"
        )

    # ------------------------------------------------------------- framework
    def check_framework(self) -> None:
        """The app runs THIS checkout's framework, never a stale build (a
        cached wheel of the same version): every installed ``sandloom`` file is
        byte-identical to its source, or ``sandloom`` is the live tree (editable)."""
        installed = json.loads(
            self.python(
                "import json, pathlib, sandloom\n"
                "from importlib.metadata import distributions\n"
                "files = {}\n"
                "names = ('sandloom', 'sandloom-identity', "
                "'sandloom-tenancy', 'sandloom-billing')\n"
                "for d in distributions():\n"
                "    if (d.metadata['Name'] or '') in names:\n"
                "        for f in d.files or []:\n"
                "            if f.parts[0] in tuple("
                "n.replace('-', '_') for n in names) "
                "and f.suffix != '.pyc':\n"
                "                files[str(f)] = str(f.locate())\n"
                "print(json.dumps({'init': "
                "str(pathlib.Path(sandloom.__file__).resolve()), 'files': files}))\n"
            ).splitlines()[-1]
        )
        backend = self.repo
        live = backend / "src" / "sandloom" / "__init__.py"
        stale: list[str] = []
        for rel, located in sorted(installed["files"].items()):
            if rel.startswith("sandloom/core/migrations/core_chain/"):
                source = backend / "migrations/core" / rel.split("/", 4)[4]
            else:
                source = backend / "src" / rel
            if not source.is_file() or (
                source.read_bytes() != Path(located).read_bytes()
            ):
                stale.append(rel)
        is_live = Path(installed["init"]) == live.resolve()
        if self.dist_dir and is_live:
            raise QuickstartError(
                "wheel quickstart imported editable framework sources"
            )
        if not is_live and not installed["files"]:
            raise QuickstartError(
                f"sandloom is imported from {installed['init']}, not from {backend}"
            )
        if stale:
            raise QuickstartError(
                f"the installed framework differs from {backend} (a stale build?): "
                + ", ".join(stale[:10])
                + (f" (+{len(stale) - 10} more)" if len(stale) > 10 else "")
            )
        how = (
            "the live tree (editable)"
            if is_live
            else f"{len(installed['files'])} installed files"
        )
        print(f"framework: {how} matches {backend}")
        if self.dist_dir:
            expected = {
                name: tomllib.loads(
                    (backend / "packages" / name / "pyproject.toml").read_text()
                )["project"]["version"]
                for name in ("sandloom", *(f"sandloom-{m}" for m in self.batteries))
            }
            observed = json.loads(
                self.python(
                    "import json; from importlib.metadata import version; "
                    f"names = {list(expected)!r}; "
                    "print(json.dumps({name: version(name) for name in names}))"
                ).splitlines()[-1]
            )
            if observed != expected:
                raise QuickstartError(
                    f"installed versions {observed} differ from "
                    f"reviewed wheels {expected}"
                )
            print(f"installed release versions verified: {observed}")

    # ----------------------------------------------------------------- serve
    def serve(self, command: str) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # uvicorn reads UVICORN_* for every CLI option: the line stays verbatim
        env = {**self.env, "UVICORN_PORT": str(port), "UVICORN_HOST": "127.0.0.1"}
        print(f"$ {command}   (in {self.cwd}, UVICORN_PORT={port})", flush=True)
        self.server = subprocess.Popen(  # noqa: S602 - the documented quickstart line, verbatim
            command, shell=True, cwd=self.cwd, env=env, start_new_session=True
        )
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.args.serve_timeout
        last = ""
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise QuickstartError(f"the app exited ({self.server.returncode})")
            try:
                status, body = self.http("GET", "/health")
            except OSError as exc:
                last = str(exc)
                time.sleep(0.5)
                continue
            if status == 200:
                print(f"GET /health -> 200 {body[:200]}")
                return
            last = f"{status} {body[:300]}"
            time.sleep(0.5)
        raise QuickstartError(f"GET /health never answered 200 (last: {last})")

    def stop_server(self) -> None:
        if self.server is None or self.server.poll() is not None:
            return
        os.killpg(self.server.pid, signal.SIGTERM)
        try:
            self.server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(self.server.pid, signal.SIGKILL)
            self.server.wait()

    def http(
        self, method: str, path: str, body: object = None, headers: dict | None = None
    ) -> tuple[int, str]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 - the local app only
            self.base_url + path, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def round_trip(self, module: str) -> None:
        auth = self.python(
            "import uuid\n"
            "from sandloom.sdk.testing import as_user\n"
            "print(as_user(tenant_id=uuid.uuid4())[1]['Authorization'])\n"
        ).splitlines()[-1]
        headers = {"Authorization": auth, "Content-Type": "application/json"}
        path = f"/api/v1/{module}/items"
        status, body = self.http("POST", path, {"title": "quickstart"}, headers)
        print(f"POST {path} -> {status} {body[:200]}")
        if status != 201:
            raise QuickstartError(f"POST {path}: {status} {body}")
        item = json.loads(body)
        status, body = self.http("GET", f"{path}/{item['id']}", headers=headers)
        print(f"GET {path}/{item['id']} -> {status} {body[:200]}")
        if status != 200 or json.loads(body).get("title") != "quickstart":
            raise QuickstartError(f"GET {path}/<id>: {status} {body}")
        status, _ = self.http("POST", path, {"title": "x"})
        print(f"POST {path} without a token -> {status}")
        if status != 401:
            raise QuickstartError(f"an anonymous POST answered {status}, not 401")

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        a = self.args
        work = a.workdir or Path(tempfile.mkdtemp(prefix="sandloom-quickstart-"))
        work.mkdir(parents=True, exist_ok=True)
        if any(work.iterdir()):
            raise QuickstartError(f"{work} is not empty")
        self.cwd = work.resolve()
        self.configure_services()
        cli = (
            shlex.quote(str(a.cli.resolve()))
            if a.cli
            else f"uv run --project {shlex.quote(str(self.repo))} sandloom"
        )
        batteries = "identity,tenancy,billing" if a.profile == "all" else a.profile
        self.batteries = [] if a.profile == "bare" else batteries.split(",")
        selection = "--no-batteries" if a.profile == "bare" else f"--with {batteries}"
        source = (
            "--framework-source pypi"
            if self.dist_dir
            else "--framework-source path --framework-path "
            + shlex.quote(str(self.repo))
        )
        new = f"{cli} new {a.app} {source} {selection}"
        out = self.step("sandloom new", self.sh, new, capture=True)
        if "ModuleLoadError" in out:
            raise QuickstartError(
                "sandloom new tried loading uninstalled app dependencies"
            )
        if "Next steps:" not in out:
            raise QuickstartError("sandloom new printed no 'Next steps:'")
        lines = [
            line.split("#", 1)[0].strip()
            for line in out.split("Next steps:", 1)[1].splitlines()
        ]
        self.module = ""
        try:
            for line in filter(None, lines):
                self.do_line(line)
            if not self.module:
                raise QuickstartError("the quickstart adds no module")
            if self.server is None:
                raise QuickstartError("the quickstart never starts the app")
            self.step("uv run sandloom test --ci", self.sh, "uv run sandloom test --ci")
            self.step("cold project graph", self.sh, "uv run sandloom graph")
            self.step(
                "owned module map", self.sh, f"uv run sandloom context {self.module}"
            )
            self.step(
                "offline architecture export",
                self.sh,
                "uv run sandloom explore --no-open --no-generate",
            )
        finally:
            self.stop_server()

    def do_line(self, line: str) -> None:
        """One printed quickstart line (its comment stripped)."""
        if line.startswith("cd "):
            self.cwd = (self.cwd / shlex.split(line)[1]).resolve()
            self.timings.append((line, 0.0, "PASS"))
        elif line.startswith("docker compose"):
            if self.args.postgres_url:
                self.step(f"{line} (given servers)", self.provided_services)
            else:
                self.step(line, self.sh, line)
            # Not a quickstart line: a fresh app passes its own CI gate.
            self.step(
                "(fresh app, extra) uv run sandloom test --ci",
                self.sh,
                "uv run sandloom test --ci",
            )
        elif "uvicorn" in line:
            self.step(line, self.serve, line)
            self.step("authenticated round trip", self.round_trip, self.module)
            self.stop_server()
        else:
            if line.startswith("uv run sandloom add module "):
                self.module = line.rsplit(" ", 1)[1]
            gate = line == "uv run sandloom test"
            out = self.step(line, self.sh, line, capture=gate)
            if gate and "sandloom test: OK" not in out:
                raise QuickstartError("sandloom test did not report OK")
            if line == "uv sync":
                # Not a quickstart line: the app got this checkout's framework.
                self.step(
                    "(extra) the installed framework matches the checkout",
                    self.check_framework,
                )

    def summary(self, ok: bool) -> str:
        rows = ["", "quickstart summary", "------------------"]
        for name, secs, status in self.timings:
            rows.append(f"  {status:5} {secs:7.1f}s  {name}")
        total = sum(s for _, s, _ in self.timings)
        rows.append(f"\nquickstart: {'OK' if ok else 'FAILED'} ({total:.0f}s)")
        return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=REPO, help="framework repository")
    p.add_argument("--workdir", type=Path, help="an empty directory (default: temp)")
    p.add_argument("--app", default="demo", help="application name (default: demo)")
    p.add_argument(
        "--cli", type=Path, help="standalone CLI executable without batteries"
    )
    p.add_argument(
        "--profile",
        choices=(
            "bare",
            "identity",
            "tenancy",
            "billing",
            "identity,tenancy",
            "identity,billing",
            "tenancy,billing",
            "all",
        ),
        default="bare",
    )
    p.add_argument(
        "--dist-dir",
        type=Path,
        help="validated wheels: use registry requirements with local find-links, "
        "never editable sources",
    )
    p.add_argument("--postgres-url", help="server URL without a database (CI)")
    p.add_argument("--admin-db", default="postgres", help="database to connect to")
    p.add_argument("--redis-url", default="redis://localhost:6380")
    p.add_argument("--redis-db-base", type=int, default=0)
    p.add_argument("--db-prefix", default="sandloom_test_quickstart")
    p.add_argument("--serve-timeout", type=float, default=90.0)
    args = p.parse_args(argv)
    if args.dist_dir and (not args.cli or not args.dist_dir.is_dir()):
        p.error(
            "--dist-dir requires an existing wheel directory and "
            "--cli installed from its sandloom wheel"
        )
    runner = Runner(args)
    try:
        runner.run()
    except QuickstartError as exc:
        print(f"\nquickstart: {exc}", file=sys.stderr)
        print(runner.summary(False))
        return 1
    print(runner.summary(True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
