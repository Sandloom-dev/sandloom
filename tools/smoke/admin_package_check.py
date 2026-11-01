"""Prove the sandloom-admin package end to end, the way a new developer meets it.

Builds every wheel from this checkout, generates an application for each scenario,
runs the quickstart, creates the first administrator with ``sandloom security
grant-admin``, serves the console from the app and signs in with a real browser.
No Node on the application's side: only this check's browser uses admin/node_modules.

    uv run python tools/smoke/admin_package_check.py \\
        --postgres-url postgresql://user:pass@127.0.0.1:26398 \\
        --redis-url redis://127.0.0.1:26399
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "sandloom",
    "sandloom-identity",
    "sandloom-tenancy",
    "sandloom-billing",
    "sandloom-admin",
)
PASSWORD = "Package-Check-Passw0rd!"  # noqa: S105 - a throwaway local account


@dataclass(frozen=True)
class Scenario:
    name: str
    batteries: str
    modules: str
    port: int
    with_own_screen: bool = False


SCENARIOS = (
    Scenario("onlyadmin", "admin", "identity", 18731),
    Scenario("fullshop", "admin,tenancy,billing", "identity,tenancy,billing", 18732, True),
)


class CheckFailed(RuntimeError):
    pass


def run(
    command: list[str], cwd: Path, env: dict[str, str], *, ok: tuple[int, ...] = (0,)
) -> str:
    print(f"$ {' '.join(command)}", flush=True)
    done = subprocess.run(  # noqa: S603 - fixed commands of this check
        command, cwd=cwd, env=env, check=False, capture_output=True, text=True
    )
    if done.returncode not in ok:
        print(done.stdout[-3000:], done.stderr[-3000:], sep="\n")
        raise CheckFailed(f"exit {done.returncode}: {' '.join(command)}")
    return done.stdout


def fetch(
    origin: str, path: str, *, data: dict | None = None
) -> tuple[int, dict[str, str], str]:
    """One request with the path sent exactly as written: no client normalization."""
    parts = urlsplit(origin)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=20)
    body = json.dumps(data) if data is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    connection.request("POST" if data is not None else "GET", path, body, headers)
    response = connection.getresponse()
    text = response.read().decode("utf-8", "replace")
    received = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, received, text


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise CheckFailed(what)
    print(f"  ok: {what}", flush=True)


def check_http(origin: str, app_dir: Path) -> None:
    status, headers, html = fetch(origin, "/admin/")
    expect(status == 200, "the entry page answers 200")
    expect("script-src 'self'" in headers.get("content-security-policy", ""), "it has a CSP")
    expect(headers.get("x-frame-options") == "DENY", "it refuses framing")
    expect(headers.get("x-content-type-options") == "nosniff", "it refuses sniffing")
    expect(headers.get("cache-control") == "no-store", "the entry page is not cached")
    expect("http://" not in html and "https://" not in html, "it names no other origin")

    status, headers, _ = fetch(origin, "/admin")
    expect(status == 308 and headers["location"] == "/admin/", "/admin redirects")
    status, _, deep = fetch(origin, "/admin/resources/users/42")
    expect(status == 200 and deep == html, "a deep link serves the entry page")

    asset = html.split('src="/admin/')[1].split('"')[0]
    status, headers, _ = fetch(origin, f"/admin/{asset}")
    expect(status == 200 and "immutable" in headers["cache-control"], "assets are cached")
    expect("javascript" in headers["content-type"], "scripts have a script type")
    status, _, _ = fetch(origin, "/admin/assets/gone-0000.js")
    expect(status == 404, "a missing asset is a 404, not HTML")

    for path in (
        "/admin/../../pyproject.toml",
        "/admin/..%2f..%2fapi.py",
        "/admin/%2e%2e/%2e%2e/api.py",
        "/admin/..%2f__init__.py",
        "/admin/....//....//api.py",
    ):
        _, _, text = fetch(origin, path)
        expect(
            "module_router" not in text and "[project]" not in text,
            f"{path} leaks nothing",
        )

    status, headers, manifest = fetch(origin, "/admin/admin-manifest.json")
    expect(status == 200 and headers["cache-control"] == "no-store", "manifest served")
    on_disk = next(app_dir.rglob("shared/admin-manifest.json")).read_text()
    expect(json.loads(manifest) == json.loads(on_disk), "it is the generated file")
    status, _, _ = fetch(origin, "/api/v1/users")
    expect(status == 401, "the APIs still require a sign-in")
    status, _, _ = fetch(origin, "/api/v1/platform/identity/audit")
    expect(status == 401, "the platform views still require a sign-in")


def wait_healthy(origin: str, process: subprocess.Popen, log: Path) -> None:
    for _ in range(150):
        if process.poll() is not None:
            print(log.read_text()[-3000:])
            raise CheckFailed("the application exited during startup")
        try:
            with urlopen(f"{origin}/health/simple", timeout=1) as response:  # noqa: S310 - loopback
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise CheckFailed("the application did not become healthy")


NOTE_MODULE_ADMIN = '''RESOURCES = [
    {
        "name": "notes",
        "label": "Notes",
        "access": {"authenticated": True},
        "list": {"path": "/api/v1/notes/items", "method": "GET"},
        "pagination": {"mode": "offset", "offset": "skip", "limit": "limit", "max_page_size": 50},
        "columns": ["title", "owner_id"],
        "detail": {"path": "/api/v1/notes/items/{item_id}", "method": "GET"},
        "show": ["id", "title", "owner_id"],
        "create": {"path": "/api/v1/notes/items", "method": "POST", "fields": ["title"]},
        "labels": {"id": "ID", "title": "Title", "owner_id": "Owner"},
        "scope": {"parameter": None, "label": "Organization"},
    }
]
'''


def scenario_env(args: argparse.Namespace, scenario: Scenario, base: str) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith(("SANDLOOM_", "VIRTUAL_ENV"))},
        "DATABASE_URL": base.replace("postgresql://", "postgresql+asyncpg://"),
        "CACHE_URL": f"{args.redis_url}/{scenario.port % 10 + 2}",
        "BROKER_URL": f"{args.redis_url}/{scenario.port % 10 + 4}",
        "EVENT_BUS_BACKEND": "memory",
        "BILLING_PROVIDER": "fake",
        "INTERNAL_API_TOKEN": secrets.token_urlsafe(32),
    }


def own_screen(app_dir: Path, app: str, env: dict[str, str]) -> None:
    """A module's admin.py is all it takes to get a screen."""
    module = app_dir / app / "modules" / "notes"
    (module / "admin.py").write_text(NOTE_MODULE_ADMIN)
    # The edit the guide asks for: the generated list takes a limit but no offset.
    api = (module / "api.py").read_text()
    (module / "api.py").write_text(
        api.replace(
            "    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20\n",
            "    user: CurrentUser,\n"
            "    skip: Annotated[int, Query(ge=0)] = 0,\n"
            "    limit: Annotated[int, Query(ge=1, le=100)] = 20,\n",
        ).replace("services.list_items(limit=limit)", "services.list_items(limit=limit, skip=skip)")
    )
    services = (module / "services.py").read_text()
    (module / "services.py").write_text(
        services.replace("async def list_items(*, limit: int)", "async def list_items(*, limit: int, skip: int = 0)")
        .replace(".limit(limit)", ".offset(skip).limit(limit)")
    )
    run(["uv", "run", "sandloom", "generate"], app_dir, env)
    manifest = json.loads((app_dir / app / "shared/admin-manifest.json").read_text())
    expect(
        any(r["name"] == "notes" for r in manifest["resources"]),
        "sandloom generate picked up the module's admin.py",
    )


def run_scenario(args: argparse.Namespace, scenario: Scenario, wheels: Path, work: Path) -> None:
    print(f"\n=== scenario {scenario.name}: --with {scenario.batteries}", flush=True)
    app = scenario.name
    database = f"admincheck_{app}_{secrets.token_hex(4)}"
    base_env = dict(os.environ)
    run(
        [
            str(Path(sys.executable).with_name("sandloom")), "new", app,
            "--with", scenario.batteries, "--module", "notes", "--dir", str(work),
        ],
        ROOT,
        base_env,
    )
    app_dir = work / app
    pyproject = app_dir / "pyproject.toml"
    expect("sandloom-admin" in pyproject.read_text(), "the app depends on sandloom-admin")
    expect(
        '"admin"' in (app_dir / app / "asgi.py").read_text(), "asgi.py loads the console"
    )
    # An app generated from a checkout points at it; the check wants the real wheels.
    project = re.sub(
        r"\[tool\.uv\.sources\]\n(?:[^\[\n].*\n)*", "", pyproject.read_text()
    )
    sources = "\n[tool.uv.sources]\n" + "".join(
        f'{name} = {{ path = "{next(wheels.glob(name.replace("-", "_") + "-*.whl"))}" }}\n'
        for name in PACKAGES
    )
    pyproject.write_text(project + sources)
    env = scenario_env(args, scenario, f"{args.postgres_url}/{database}")
    origin = f"http://127.0.0.1:{scenario.port}"
    create = (
        "import asyncio, asyncpg, sys\n"
        "async def main():\n"
        f"    c = await asyncpg.connect('{args.postgres_url}/postgres')\n"
        f"    await c.execute('{{}} DATABASE \"{database}\"'.format(sys.argv[1]))\n"
        "    await c.close()\n"
        "asyncio.run(main())\n"
    )
    run(["uv", "run", "python", "-c", create, "CREATE"], ROOT, base_env)
    process = None
    try:
        run(["uv", "sync", "-q"], app_dir, env)
        installed = run(["uv", "pip", "list"], app_dir, env)
        expect("sandloom-admin" in installed, "the wheel is installed")
        expect(
            "editable" not in installed.split("sandloom-admin")[1].splitlines()[0],
            "from the wheel, not the source tree",
        )
        wheel_console = list((app_dir / ".venv").rglob("sandloom_admin/console/index.html"))
        expect(len(wheel_console) == 1, "the wheel carries the built console")
        run(["uv", "run", "sandloom", "generate"], app_dir, env)
        run(["uv", "run", "sandloom", "generate", "--check"], app_dir, env)
        if scenario.with_own_screen:
            own_screen(app_dir, app, env)
        run(["uv", "run", "sandloom", "migrate", "upgrade-all", "--apply"], app_dir, env)

        log = work / f"{app}.log"
        with log.open("w") as out:
            process = subprocess.Popen(  # noqa: S603 - the generated app's own entry point
                ["uv", "run", "uvicorn", f"{app}.asgi:app", "--port", str(scenario.port)],  # noqa: S607
                cwd=app_dir, env=env, stdout=out, stderr=subprocess.STDOUT,
            )
        wait_healthy(origin, process, log)
        check_http(origin, app_dir)

        admin, member = f"owner@{app}.example.com", f"member@{app}.example.com"
        for address in (admin, member):
            status, _, _ = fetch(
                origin, "/auth/register",
                data={"email": address, "full_name": "Check", "password": PASSWORD},
            )
            expect(status == 202, f"{address} registered")
        status, _, _ = fetch(
            origin, "/auth/login", data={"email": admin, "password": PASSWORD}
        )
        expect(status == 403, "an unverified account cannot sign in")

        grant = ["uv", "run", "sandloom", "security", "grant-admin"]
        missing = subprocess.run(  # noqa: S603 - fixed command
            [*grant, "--email", "nobody@example.com"],
            cwd=app_dir, env=env, capture_output=True, text=True, check=False,
        )
        expect(
            missing.returncode != 0 and "register" in missing.stderr,
            "an unknown address names the next step",
        )
        output = run([*grant, "--email", admin], app_dir, env)
        expect("dry-run" in output, "grant-admin is a dry run by default")
        status, _, _ = fetch(
            origin, "/auth/login", data={"email": admin, "password": PASSWORD}
        )
        expect(status == 403, "a dry run changed nothing")
        run([*grant, "--email", admin, "--apply", "--reason", "first admin"], app_dir, env)
        run([*grant, "--email", admin, "--apply", "--reason", "run twice"], app_dir, env)
        status, _, body = fetch(
            origin, "/auth/login", data={"email": admin, "password": PASSWORD}
        )
        expect(status == 200, "the administrator signs in")
        token = json.loads(body)["access_token"]
        request = Request(  # noqa: S310 - loopback
            f"{origin}/api/v1/platform/identity/audit",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=10) as response:  # noqa: S310 - loopback
            audit = json.loads(response.read())
        expect(
            any(r["action"] == "identity.user.platform_admin_granted" for r in audit),
            "the grant is in the audit log",
        )

        browser_env = {
            **os.environ,
            "SANDLOOM_CONSOLE_ORIGIN": origin,
            "SANDLOOM_CONSOLE_ADMIN": admin,
            "SANDLOOM_CONSOLE_MEMBER": member,
            "SANDLOOM_CONSOLE_PASSWORD": PASSWORD,
            "SANDLOOM_CONSOLE_MODULES": scenario.modules,
            **({"SANDLOOM_CONSOLE_APP_RESOURCE": "Notes"} if scenario.with_own_screen else {}),
        }
        run(
            [
                shutil.which("npx") or "npx", "playwright", "test",
                "--config", "playwright.package.config.ts", "--reporter=line",
            ],
            ROOT / "admin",
            browser_env,
        )
        print("  ok: the browser checks passed", flush=True)
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        drop = (
            "import asyncio, asyncpg\n"
            "async def main():\n"
            f"    c = await asyncpg.connect('{args.postgres_url}/postgres')\n"
            f"    await c.execute('DROP DATABASE IF EXISTS \"{database}\" WITH (FORCE)')\n"
            "    await c.close()\n"
            "asyncio.run(main())\n"
        )
        run(["uv", "run", "python", "-c", drop], ROOT, base_env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--only", default=None, help="one scenario by name")
    parser.add_argument("--keep", action="store_true", help="keep the generated apps")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="sandloom-admin-check-"))
    wheels = work / "wheels"
    try:
        for name in PACKAGES:
            run(
                ["uv", "build", "--wheel", "-q", "--out-dir", str(wheels), f"packages/{name}"],
                ROOT,
                dict(os.environ),
            )
        for scenario in SCENARIOS:
            if args.only in (None, scenario.name):
                run_scenario(args, scenario, wheels, work)
    except CheckFailed as exc:
        print(f"\nadmin package check: FAILED: {exc}\n(apps kept in {work})")
        return 1
    print("\nadmin package check: OK")
    if args.keep:
        print(f"apps kept in {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
