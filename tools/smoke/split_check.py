#!/usr/bin/env python3
"""Split an ``sandloom new`` application into microservices, by configuration only
(CI job ``app-split``; ``tools/ci/ci_local.sh <ref> --jobs app-split`` runs the
same; the slow test ``tests/e2e/test_app_split.py`` runs it with
``--smoke``).

No manual step: a fix-up this script would need is a bug in the template or
the CLI, never something to add here. The steps, in an empty directory:

1. ``sandloom new hub --with identity,tenancy,billing`` against the framework from
   this checkout (``--framework-source path``), then ``uv sync``;
2. two application modules where one calls the other: ``sandloom add module
   catalog|orders``, ``sandloom add contract catalog lookup``, ``sandloom add consumer
   orders catalog.item.created.v1``; ``orders`` calls ``catalog.lookup.v1``
   from a route and records each ``catalog.item.created.v1`` it consumes
   (the only hand-written code: the two bodies);
3. the split, exactly as a developer types it: ``sandloom add deploy identity``,
   ``tenancy``, ``billing``, ``catalog --service catalog``, ``orders example
   --service orders``; a repeated run changes nothing;
4. ``sandloom keys dev-env`` (development keys) and ``docker compose config`` of
   the generated profile (skipped, with a notice, without docker unless
   ``--require-docker``); with docker, the app's own build context (its
   ``.dockerignore``, exported by ``docker build``) holds no secret file,
   although ``deploy/.env.microservices`` and a key file exist;
5. the split invariants ``sandloom test`` checks (its ``schema & deploy`` step,
   SANDLOOM-T014: no problem, no warning) and ``sandloom add deploy --check``: each
   service's ``SANDLOOM_MODULES`` booted alone with exactly its environment
   (settings, startup preflight, composition root, every consumed contract
   bound; no network), as ``sandloom doctor`` boots a process;
6. no Python file changed by the split (``sandloom add deploy`` writes only
   ``deploy/``);
7. ``--smoke``: the real stack. The image is the framework image of this
   checkout plus the app package installed from the app's own build context
   as its Dockerfile does (``pyproject.toml`` + the package into the
   virtualenv, nothing else copied: the app's own Dockerfile installs the
   framework from a registry, which an unpublished checkout is not in); no
   secret file is in it. Then ``docker compose up`` and, through the
   gateway: register (verified in the identity database) / login / an
   organization on tenancy / login again (the token's ``tenant_ids`` come
   from tenancy over RPC) / ``GET /api/v1/orders/catalog-ping`` (orders ->
   catalog over RPC) / a catalog item whose event the orders service
   consumes;
8. ``--smoke``: a service added to the RUNNING stack (``sandloom add module
   notes``, ``sandloom add deploy notes``, new keys, a rebuilt image, ``compose
   up`` on the existing volume, where ``init-databases.sh`` does not run
   again): its migrate one-shot creates its database, and ``GET
   /api/v1/notes/items`` answers through the gateway.

Standard library only: it runs before any virtualenv exists.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = "hub"
SPLIT = (
    "uv run sandloom add deploy identity",
    "uv run sandloom add deploy tenancy",
    "uv run sandloom add deploy billing",
    "uv run sandloom add deploy catalog --service catalog",
    "uv run sandloom add deploy orders example --service orders",
)
SERVICES = {
    "identity": ["sandloom_identity"],
    "tenancy": ["sandloom_tenancy"],
    "billing": ["sandloom_billing"],
    "catalog": ["hub.modules.catalog"],
    "orders": ["hub.modules.orders", "hub.modules.example"],
}

_ORDERS_CALL = '''

@router.get("/catalog-ping")
async def catalog_ping(user: CurrentUser) -> dict[str, bool]:
    """Calls the catalog module's contract (over RPC when split)."""
    return {"catalog": (await lookup.status()).ready}
'''

#: The uv image the framework Dockerfile builds with (installs the app).
UV_IMAGE = "ghcr.io/astral-sh/uv:0.13.4"
#: Key files ``sandloom keys generate --out-dir`` writes.
_KEY_FILES = {"jwt_private_key", "rpc_private_key", "rpc_dh_private_key"}


def secret_files(root: Path) -> list[str]:
    """Env files (but ``.env.example``), private key and PEM files under
    ``root`` (sandloom.core.cli.image_secrets.is_secret; stdlib here)."""
    out = []
    for path in sorted(root.rglob("*")):
        name = path.name
        if path.is_dir() or name == ".env.example":
            continue
        if (
            name == ".env"
            or name.startswith(".env.")
            or name in _KEY_FILES
            or name.endswith(".pem")
        ):
            out.append(path.relative_to(root).as_posix())
    return out


#: Run inside the image: the secret files under /app and the virtualenv (a
#: PEM file only under /app: libraries ship public CA bundles).
_IMAGE_SCAN = (
    "import json, pathlib; "
    "names = {'jwt_private_key', 'rpc_private_key', 'rpc_dh_private_key'}; "
    "hits = [str(p) for root in ('/app', '/opt/venv') "
    "for p in pathlib.Path(root).rglob('*') if p.is_file() and p.name != "
    "'.env.example' and (p.name == '.env' or p.name.startswith('.env.') "
    "or p.name in names or (root == '/app' and p.name.endswith('.pem')))]; "
    "print(json.dumps(hits))"
)

_CHECK = """
import json, sys
from pathlib import Path
from sandloom.core.cli.run_tests import load_project_test_env
root = Path('.').resolve()
load_project_test_env(root)
from sandloom.core.composition import load_composition_core
from sandloom.core.testing import deploy_profile as dp
from sandloom.core.testing.static_checks import all_problems
warnings = []
core = load_composition_core()
problems = all_problems(root, core, warnings=warnings)
compose = dp.load_profile(Path('deploy') / dp.PROFILE)
print(json.dumps({'problems': problems, 'warnings': warnings,
                  'services': dp.http_services(compose)}))
"""


class SplitError(Exception):
    pass


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
        self.project = f"sandloom-split-{secrets.token_hex(3)}"
        self.compose_up = False

    # ------------------------------------------------------------------ util
    def step(self, name: str, fn, *a, **kw):  # type: ignore[no-untyped-def]
        print(f"\n=== split: {name}", flush=True)
        started = time.perf_counter()
        try:
            result = fn(*a, **kw)
        except Exception:
            self.timings.append((name, time.perf_counter() - started, "FAIL"))
            raise
        self.timings.append((name, time.perf_counter() - started, "PASS"))
        return result

    def sh(
        self, command: str, *, capture: bool = False, env: dict | None = None
    ) -> str:
        print(f"$ {command}   (in {self.cwd})", flush=True)
        proc = subprocess.run(  # noqa: S602 - the documented commands, verbatim
            command,
            shell=True,
            cwd=self.cwd,
            env={**self.env, **(env or {})},
            check=False,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
        if capture:
            print(proc.stdout, end="", flush=True)
        if proc.returncode != 0:
            raise SplitError(f"`{command}` failed (exit {proc.returncode})")
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
            raise SplitError(f"python step failed:\n{proc.stdout}{proc.stderr}")
        return proc.stdout.strip()

    # ------------------------------------------------------------- the app
    def new_app(self) -> None:
        self.sh(
            f"uv run --project {shlex.quote(str(self.repo))} sandloom new "
            f"{APP} --with identity,tenancy,billing --framework-source path "
            f"--framework-path {shlex.quote(str(self.repo))}"
        )
        self.cwd = self.cwd / APP
        self.sh("uv sync")

    def modules(self) -> None:
        for command in (
            "uv run sandloom add module catalog",
            "uv run sandloom add module orders",
            "uv run sandloom add contract catalog lookup",
            "uv run sandloom add consumer orders catalog.item.created.v1",
        ):
            self.sh(command)
        orders = self.cwd / APP / "modules" / "orders"
        api = (orders / "api.py").read_text()
        api = api.replace(
            "from sandloom.sdk import CurrentUser, module_router\n",
            "from sandloom.sdk import Contract, CurrentUser, module_router\n"
            f"from {APP}.shared.contracts.catalog_lookup_v1 import "
            "CATALOG_LOOKUP_V1\n",
        ).replace(
            "router = module_router()\n",
            "router = module_router()\nlookup = Contract(CATALOG_LOOKUP_V1)\n"
            + _ORDERS_CALL,
            1,
        )
        (orders / "api.py").write_text(api)
        consumers = (orders / "consumers.py").read_text()
        consumers = consumers.replace(
            "from sandloom.sdk import Event, on\n",
            "from sandloom.sdk import Event, db, on\n\n"
            "from .models.item import OrdersItem\n",
        ).replace(
            '    """Runs once per event (durable), '
            'with the event\'s tenant bound."""\n',
            '    """Runs once per event (durable), with the event\'s tenant bound."""\n'
            '    title = f"catalog:{event.item_id}"\n'
            "    db().add(OrdersItem(title=title, owner_id=event.item_id))\n",
        )
        (orders / "consumers.py").write_text(consumers)
        if "Contract(CATALOG_LOOKUP_V1)" not in api or "OrdersItem(" not in consumers:
            raise SplitError("the module scaffolds changed: update split_check.py")
        self.sh("uv run ruff format --quiet .")

    def snapshot_python(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.cwd)): p.read_text()
            for p in self.cwd.rglob("*.py")
            if ".venv" not in p.parts
        }

    # ------------------------------------------------------------ the split
    def split(self) -> None:
        for command in SPLIT:
            self.sh(command)
        again = self.sh(SPLIT[-1], capture=True)
        if "nothing changed" not in again:
            raise SplitError("a repeated `sandloom add deploy` changed the profile")

    def keys_and_config(self) -> None:
        self.sh("uv run sandloom keys dev-env --out deploy/.env.microservices")
        if shutil.which("docker") is None:
            if self.args.require_docker:
                raise SplitError("docker is required (--require-docker)")
            print("(docker not found: `docker compose config` skipped)")
            return
        self.sh(
            "docker compose --env-file deploy/.env.microservices "
            "-f deploy/compose/microservices.yml config --quiet"
        )
        self.context_has_no_secret()

    def context_has_no_secret(self) -> None:
        """The app's own build context, filtered by its ``.dockerignore`` as
        Docker does it (exported: nothing pulled or run), holds no secret
        although the split wrote them into the project."""
        self.sh("uv run sandloom keys generate --out-dir deploy/keys")
        planted = secret_files(self.cwd / "deploy")
        if "deploy/.env.microservices" not in [f"deploy/{f}" for f in planted]:
            raise SplitError("the split wrote no deploy/.env.microservices")
        scratch = Path(tempfile.mkdtemp(prefix="sandloom-split-context-"))
        try:
            (scratch / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
            self.sh(
                f"docker build -q -f {shlex.quote(str(scratch / 'Dockerfile'))} "
                f"--output type=local,dest={shlex.quote(str(scratch / 'ctx'))} ."
            )
            leaked = secret_files(scratch / "ctx")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if leaked:
            raise SplitError(f"the build context ships secret files: {leaked}")
        print(f"build context: no secret file ({len(planted)} kept out)")

    def checks(self) -> None:
        out = json.loads(self.python(_CHECK).splitlines()[-1])
        print(json.dumps(out, indent=2))
        if out["services"] != SERVICES:
            raise SplitError(f"services {out['services']} != {SERVICES}")
        if out["problems"] or out["warnings"]:
            raise SplitError("the split profile fails the invariants (SANDLOOM-T014)")
        booted = self.sh(
            "uv run sandloom add deploy --check --env-file deploy/.env.microservices",
            capture=True,
        )
        if [f"boot {name}: OK" for name in SERVICES] != [
            line for line in booted.splitlines() if line.startswith("boot ")
        ]:
            raise SplitError("not every service boots alone")

    # ----------------------------------------------------------------- smoke
    def smoke(self) -> None:
        tag = f"{self.project}:local"
        base = f"{self.project}-base:local"
        octet = 100 + secrets.randbelow(100)
        self.compose_env = {
            "SANDLOOM_IMAGE": tag,
            "GATEWAY_PORT": "127.0.0.1:0",
            "SANDLOOM_SUBNET": f"10.231.{octet}.0/24",
            "SANDLOOM_GATEWAY_IP": f"10.231.{octet}.10",
            "SANDLOOM_IP_RANGE": f"10.231.{octet}.128/25",
        }
        self.compose = (
            f"docker compose -p {self.project} --env-file deploy/.env.microservices "
            "-f deploy/compose/microservices.yml"
        )
        self.drop_env_file_variables()
        self.compose_up = True  # from here on, teardown removes what it made
        self.sh(
            f"docker build -q -t {base} --target production "
            f"-f {shlex.quote(str(self.repo / 'deploy/docker/Dockerfile'))} "
            f"{shlex.quote(str(self.repo))}"
        )
        self.build_app_image(base, tag)
        self.sh(f"{self.compose} up -d --no-build --wait", env=self.compose_env)
        port = self.sh(
            f"{self.compose} port gateway 8080", capture=True, env=self.compose_env
        )
        host_port = port.strip().splitlines()[-1].rsplit(":", 1)[1]
        self.gateway = f"http://127.0.0.1:{host_port}"
        self.round_trip()
        self.add_a_service_to_the_running_stack(base, tag)

    def drop_env_file_variables(self) -> None:
        """A variable in the caller's environment wins over --env-file in
        compose: drop every one the dev env file sets (an exported
        SANDLOOM_ACCEPTED_RISKS without fake_psp_in_prod stopped billing)."""
        keys = Path(self.cwd / "deploy" / ".env.microservices").read_text()
        for line in keys.splitlines():
            if "=" in line and not line.startswith("#"):
                self.env.pop(line.split("=", 1)[0].strip(), None)

    def build_app_image(self, base: str, tag: str) -> None:
        """The framework image of this checkout plus the app, built from the
        APP'S OWN context (its .dockerignore applies) the way its Dockerfile
        does: pyproject.toml and the package installed into the virtualenv,
        nothing else copied. No secret file may be in the image."""
        scratch = Path(tempfile.mkdtemp(prefix="sandloom-split-image-"))
        dockerfile = scratch / "Dockerfile"
        dockerfile.write_text(
            f"FROM {UV_IMAGE} AS uv\n"
            f"FROM {base} AS build\n"
            "USER root\n"
            "COPY --from=uv /uv /usr/local/bin/uv\n"
            "WORKDIR /src\n"
            "COPY pyproject.toml ./\n"
            f"COPY {APP} ./{APP}\n"
            "RUN UV_PYTHON_DOWNLOADS=never uv pip install "
            "--python /opt/venv/bin/python --no-deps .\n"
            f"FROM {base}\n"
            "COPY --from=build /opt/venv /opt/venv\n"
            "ENV GUNICORN_BIND=0.0.0.0:8000\n"
            'CMD ["gunicorn", "-c", "python:sandloom.core.cli.gunicorn_conf", '
            f'"{APP}.asgi:app"]\n'
        )
        try:
            self.sh(f"docker build -q -t {tag} -f {shlex.quote(str(dockerfile))} .")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        found = self.sh(
            f"docker run --rm --entrypoint python {tag} -c {shlex.quote(_IMAGE_SCAN)}",
            capture=True,
        )
        leaked = json.loads(found.strip().splitlines()[-1])
        if leaked:
            raise SplitError(f"the image ships secret files: {leaked}")

    def add_a_service_to_the_running_stack(self, base: str, tag: str) -> None:
        """The "add a module later" guide on the RUNNING stack: the Postgres
        volume exists, so init-databases.sh does not run again; the new
        service's migrate one-shot creates its database."""
        print("\n=== split: a service added to the running stack", flush=True)
        self.sh("uv run sandloom add module notes")
        self.sh("uv run sandloom add deploy notes --service notes")
        (self.cwd / "deploy" / ".env.microservices").unlink()
        self.sh("uv run sandloom keys dev-env --out deploy/.env.microservices")
        self.drop_env_file_variables()
        self.build_app_image(base, tag)
        self.sh(f"{self.compose} up -d --no-build --wait", env=self.compose_env)
        # nginx reads its config at start: the guide's step for new routes.
        self.sh(f"{self.compose} restart gateway", env=self.compose_env)
        port = self.sh(
            f"{self.compose} port gateway 8080", capture=True, env=self.compose_env
        )
        host_port = port.strip().splitlines()[-1].rsplit(":", 1)[1]
        self.gateway = f"http://127.0.0.1:{host_port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.http("GET", "/health")[0] == 200:
                    break
            except OSError:  # the gateway is still starting
                pass
            time.sleep(1)
        self.sh(
            f"{self.compose} exec -T db psql -v ON_ERROR_STOP=1 -U sandloom -d "
            f'{APP}_notes -c "SELECT 1"',
            env=self.compose_env,
        )
        auth = {**self.login(*self.account), "X-Tenant-Id": self.org}
        self.expect(
            "GET /api/v1/notes/items (the added service)",
            self.http("GET", "/api/v1/notes/items", headers=auth),
            200,
        )
        print("the added service migrated its own new database and answers")

    def http(
        self, method: str, path: str, body: object = None, headers: dict | None = None
    ) -> tuple[int, str]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 - the local gateway only
            self.gateway + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def expect(self, what: str, got: tuple[int, str], status: int) -> dict:
        print(f"{what} -> {got[0]} {got[1][:200]}")
        if got[0] != status:
            raise SplitError(f"{what}: {got[0]} {got[1][:500]}")
        return json.loads(got[1]) if got[1] else {}

    def login(self, email: str, password: str) -> dict[str, str]:
        body = {"email": email, "password": password}
        token = self.expect(
            "POST /auth/login (identity)",
            self.http("POST", "/auth/login", body),
            200,
        )["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def round_trip(self) -> None:
        email = f"split-{secrets.token_hex(4)}@example.com"
        password = f"Spl1t-{secrets.token_hex(8)}!"
        self.expect(
            "POST /auth/register (identity)",
            self.http(
                "POST",
                "/auth/register",
                {"email": email, "full_name": "Split", "password": password},
            ),
            202,
        )
        verify_sql = (
            "UPDATE users SET is_verified = true, email_verified_at = now() "
            "WHERE email = :'email';"
        )
        self.sh(
            f"{self.compose} exec -T db psql -v ON_ERROR_STOP=1 "
            f"-v email={shlex.quote(email)} -U sandloom -d {APP}_identity <<'SQL'\n"
            + verify_sql
            + "\nSQL",
            env=self.compose_env,
        )
        auth = self.login(email, password)
        org = self.expect(
            "POST /api/v1/orgs (tenancy)",
            self.http(
                "POST",
                "/api/v1/orgs",
                {"name": "Split", "slug": f"split-{secrets.token_hex(4)}"},
                auth,
            ),
            201,
        )["id"]
        # tenant_ids come from tenancy.claims.v1, over RPC to tenancy.
        auth = {**self.login(email, password), "X-Tenant-Id": org}
        self.account, self.org = (email, password), org
        ping = self.expect(
            "GET /api/v1/orders/catalog-ping (orders -> catalog over RPC)",
            self.http("GET", "/api/v1/orders/catalog-ping", headers=auth),
            200,
        )
        if ping != {"catalog": True}:
            raise SplitError(f"catalog-ping answered {ping}")
        item = self.expect(
            "POST /api/v1/catalog/items (catalog)",
            self.http("POST", "/api/v1/catalog/items", {"title": "split"}, auth),
            201,
        )["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, body = self.http("GET", "/api/v1/orders/items", headers=auth)
            if status == 200 and any(
                i.get("title") == f"catalog:{item}" for i in json.loads(body)
            ):
                print(f"orders consumed catalog.item.created.v1 for {item}")
                return
            time.sleep(2)
        raise SplitError(f"orders never consumed the event of item {item}")

    def teardown(self) -> None:
        if not self.compose_up:
            return
        if self.args.keep:
            print(f"(--keep: compose project {self.project} left running)")
            return
        subprocess.run(  # noqa: S602 - this script's own compose project
            f"{self.compose} logs --no-color --tail=40 orders catalog",
            shell=True,
            cwd=self.cwd,
            env={**self.env, **self.compose_env},
            check=False,
        )
        subprocess.run(  # noqa: S602 - this script's own compose project
            f"{self.compose} down -v --remove-orphans",
            shell=True,
            cwd=self.cwd,
            env={**self.env, **self.compose_env},
            check=False,
        )
        subprocess.run(  # noqa: S603 - the two images this run built
            [
                shutil.which("docker") or "/usr/bin/false",
                "image",
                "rm",
                f"{self.project}:local",
                f"{self.project}-base:local",
            ],
            check=False,
        )

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        work = self.args.workdir or Path(tempfile.mkdtemp(prefix="sandloom-split-"))
        work.mkdir(parents=True, exist_ok=True)
        if any(work.iterdir()):
            raise SplitError(f"{work} is not empty")
        self.cwd = work.resolve()
        self.step("sandloom new + uv sync", self.new_app)
        self.step(
            "two modules: orders calls catalog and consumes its event", self.modules
        )
        before = self.snapshot_python()
        self.step("sandloom add deploy (the split)", self.split)
        if self.snapshot_python() != before:
            raise SplitError("the split changed Python files")
        self.step("sandloom keys dev-env + docker compose config", self.keys_and_config)
        self.step("invariants (SANDLOOM-T014) + every service boots alone", self.checks)
        if self.args.smoke:
            try:
                self.step("docker compose up + calls through the gateway", self.smoke)
            finally:
                self.teardown()

    def summary(self, ok: bool) -> str:
        rows = ["", "split summary", "-------------"]
        rows += [f"  {s:5} {t:7.1f}s  {n}" for n, t, s in self.timings]
        return "\n".join(rows) + f"\n\nsplit: {'OK' if ok else 'FAILED'}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", type=Path, default=REPO, help="framework repository")
    p.add_argument("--workdir", type=Path, help="an empty directory (default: temp)")
    p.add_argument("--smoke", action="store_true", help="docker compose up + calls")
    p.add_argument("--keep", action="store_true", help="leave the stack running")
    p.add_argument(
        "--require-docker",
        action="store_true",
        help="fail instead of skipping `docker compose config` without docker",
    )
    runner = Runner(p.parse_args(argv))
    try:
        runner.run()
    except SplitError as exc:
        print(f"\nsplit: {exc}", file=sys.stderr)
        print(runner.summary(False))
        return 1
    print(runner.summary(True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
