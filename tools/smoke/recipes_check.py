#!/usr/bin/env python3
"""Run the ``sandloom docs`` recipes in a generated application (CI job
``app-recipes``; ``tools/ci/ci_local.sh <ref> --jobs app-recipes`` runs the
same; the slow test ``tests/e2e/test_app_recipes.py`` runs it).

No manual step: a fix-up this script would need is a bug in a recipe, the
template or the CLI, never something to add here. In an empty directory:

1. ``sandloom new hub --with identity,tenancy,billing`` against the framework from
   this checkout (``--framework-source path``), then ``uv sync``;
2. for every recipe with ``# file:`` blocks (``sandloom.sdk._prim.recipes.
   recipe_parts``): ``uv run sandloom add module <module>`` (and ``<consumer>``
   when the recipe names one), the recipe's own ``uv run sandloom add ...`` /
   ``uv run sandloom migrate new ...`` lines in order, then each of its files,
   written verbatim with the placeholders filled in;
3. ``uv run sandloom generate`` (the recipe files changed declarations after
   the last ``sandloom add``), then ``uv run sandloom test`` on the whole app:
   every recipe's tests, lint, format,
   mypy, ``sandloom check``, the generated files and the migration drift gate;
4. ``sandloom docs`` from the installed packages, in the app: a bundled contract
   and event, the listings, and an app event a recipe produced.

Services. ``--postgres-url`` names a server: the test database
``<db-prefix>_test`` is dropped and recreated (``SANDLOOM_TEST_DATABASE_URL``)
and Redis indexes ``--redis-db-base`` .. ``+1`` hold the test cache and
broker; the test workers allocate their own indexes
(``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``). Without it the app's ``.env.test``
defaults apply (the framework's deploy/compose/test.yml).

    python3 tools/smoke/recipes_check.py \\
        --postgres-url postgresql://test_user:test_password@localhost:5433 \\
        --redis-url redis://localhost:6380 --redis-db-base 0

Standard library only: it runs before any virtualenv exists.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = "hub"
#: The module each recipe is run in (``<module>``; ``<consumer>``: FEED).
MODULES = {
    "test-service-or-task": "counts",
    "call-contract": "exports",
    "projection-from-platform-events": "team",
    "event-v2": "notices",
    "periodic-task": "cleanup",
}
FEED = "feed"
#: ``sandloom docs`` lines that must answer in the app (exit 0) with this text.
DOCS = (
    ("billing.credits.v1", "operation_key"),
    ("BILLING_ENTITLEMENTS_V1", "subscription_claims"),
    ("tenancy.member_added.v2", "version_field='version'"),
    ("contracts", "tenancy.membership.v1"),
    ("events", "tenancy.member_removed.v2"),
    ("notices.item.created.v2", "owner_id"),
    ("recipe periodic-task", "sandloom add task"),
)


class RecipesError(Exception):
    pass


def camel(name: str) -> str:
    return "".join(part.title() for part in name.split("_"))


def fill(text: str, module: str) -> str:
    return (
        text.replace("<app>", APP)
        .replace("<module>", module)
        .replace("<Module>", camel(module))
        .replace("<consumer>", FEED)
        .replace("<Consumer>", camel(FEED))
    )


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo: Path = args.repo.resolve()
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT"}
        }
        self.cwd = Path.cwd()
        self.timings: list[tuple[str, float, str]] = []

    def step(self, name: str, fn, *a, **kw):  # type: ignore[no-untyped-def]
        print(f"\n=== recipes: {name}", flush=True)
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
        proc = subprocess.run(  # noqa: S602 - the recipe's own command lines
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
            raise RecipesError(f"`{command}` failed (exit {proc.returncode})")
        return proc.stdout or ""

    def python(self, code: str) -> str:
        proc = subprocess.run(  # noqa: S603 - fixed argv; code is this script's own
            ["uv", "run", "--no-sync", "python", "-c", code],  # noqa: S607 - uv from PATH
            cwd=self.cwd,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RecipesError(f"python step failed:\n{proc.stdout}{proc.stderr}")
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
        self.admin_url = f"{pg}/{a.admin_db}"
        self.test_db = f"{a.db_prefix}_test"
        self.env.update(
            {
                "SANDLOOM_TEST_DATABASE_URL": f"{asyncpg_url}/{self.test_db}",
                "SANDLOOM_TEST_CACHE_URL": f"{redis}/{a.redis_db_base}",
                "SANDLOOM_TEST_BROKER_URL": f"{redis}/{a.redis_db_base + 1}",
            }
        )
        self.env.setdefault("SANDLOOM_TEST_REDIS_DB_STRIDE", "auto")

    def fresh_test_database(self) -> None:
        if not self.args.postgres_url:
            return
        name = self.test_db
        self.python(
            "import asyncio, asyncpg\n"
            "async def main():\n"
            f"    c = await asyncpg.connect({self.admin_url!r})\n"
            f"    await c.execute('DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)')\n"
            f"    await c.execute('CREATE DATABASE \"{name}\"')\n"
            "    await c.close()\n"
            "asyncio.run(main())\n"
        )

    # --------------------------------------------------------------- recipes
    def recipe(self, name: str, module: str) -> None:
        steps = json.loads(
            self.python(
                "import json\n"
                "from sandloom.sdk._prim.recipes import recipe_parts\n"
                f"print(json.dumps(recipe_parts({name!r})))\n"
            )
        )
        if not any(kind == "file" for kind, _what, _body in steps):
            raise RecipesError(f"recipe {name!r} has no `# file:` block")
        self.sh(f"uv run sandloom add module {module}")
        if "<consumer>" in json.dumps(steps).replace("<Consumer>", "<consumer>"):
            self.sh(f"uv run sandloom add module {FEED}")
        modules = self.cwd / APP / "modules"
        for kind, what, body in steps:  # in the recipe's order
            if kind == "command":
                self.sh(fill(what, module))
                continue
            path = modules / fill(what, module)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fill(body, module), encoding="utf-8")
            print(f"wrote {path.relative_to(self.cwd)}")

    def docs(self, names: list[str]) -> None:
        for argv, expected in DOCS:
            if argv.startswith("notices.") and "event-v2" not in names:
                continue  # the app's own event exists only after that recipe
            out = self.sh(f"uv run sandloom docs {argv}", capture=True)
            if expected not in out:
                raise RecipesError(f"`sandloom docs {argv}` lacks {expected!r}")

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        a = self.args
        work = a.workdir or Path(tempfile.mkdtemp(prefix="sandloom-recipes-"))
        work.mkdir(parents=True, exist_ok=True)
        if any(work.iterdir()):
            raise RecipesError(f"{work} is not empty")
        self.cwd = work.resolve()
        self.configure_services()
        self.step(
            "sandloom new",
            self.sh,
            f"uv run --project {shlex.quote(str(self.repo))} sandloom new "
            f"{APP} --with identity,tenancy,billing --framework-source path "
            f"--framework-path {shlex.quote(str(self.repo))}",
        )
        self.cwd = self.cwd / APP
        self.step("uv sync", self.sh, "uv sync")
        self.step("fresh test database", self.fresh_test_database)
        names = a.only or list(MODULES)
        for name in names:
            self.step(f"recipe {name}", self.recipe, name, MODULES[name])
        self.step("uv run sandloom generate", self.sh, "uv run sandloom generate")
        out = self.step("uv run sandloom test", self.sh, "uv run sandloom test", capture=True)
        if "sandloom test: OK" not in out:
            raise RecipesError("sandloom test did not report OK")
        self.step("sandloom docs in the app", self.docs, names)

    def summary(self, ok: bool) -> str:
        rows = ["", "recipes summary", "---------------"]
        for name, secs, status in self.timings:
            rows.append(f"  {status:5} {secs:7.1f}s  {name}")
        total = sum(s for _, s, _ in self.timings)
        rows.append(f"\nrecipes: {'OK' if ok else 'FAILED'} ({total:.0f}s)")
        return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=REPO, help="framework repository")
    p.add_argument("--workdir", type=Path, help="an empty directory (default: temp)")
    p.add_argument("--postgres-url", help="server URL without a database (CI)")
    p.add_argument("--admin-db", default="postgres", help="database to connect to")
    p.add_argument("--redis-url", default="redis://localhost:6380")
    p.add_argument("--redis-db-base", type=int, default=0)
    p.add_argument("--db-prefix", default="sandloom_test_recipes")
    p.add_argument(
        "--only", action="append", choices=sorted(MODULES), help="one recipe (repeat)"
    )
    runner = Runner(p.parse_args(argv))
    try:
        runner.run()
    except RecipesError as exc:
        print(f"\nrecipes: {exc}", file=sys.stderr)
        print(runner.summary(False))
        return 1
    print(runner.summary(True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
