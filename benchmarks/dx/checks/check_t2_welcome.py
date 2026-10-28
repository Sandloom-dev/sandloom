"""t2_welcome — exactly-once consumer + endpoint (experiment B)."""

import re

import pytest
from _dx import (
    app_mode,
    changed_files,
    consumers,
    deploy_dir,
    event_classes,
    module_dir,
    module_tests,
    routes,
    text,
)

WELCOME = module_dir("welcome")


def test_fn_consumer_on_user_registered() -> None:
    """A view of identity.user_registered.v1 and an @on handler for it."""
    assert "identity.user_registered.v1" in event_classes(WELCOME).values()
    assert consumers(WELCOME)


def test_fn_status_endpoint() -> None:
    assert any(r.method == "GET" for r in routes(WELCOME))


def test_fn_tests_written() -> None:
    assert module_tests(WELCOME)


def test_dev_exactly_once_claim() -> None:
    """Exactly-once effect needs a DB-atomic claim, not the cache dedupe:
    @on claims each envelope id unless it opts out with durable=False."""
    src = text(WELCOME)
    handlers = consumers(WELCOME)
    assert (
        (handlers and all("durable=False" not in deco for _, deco in handlers))
        or "durable_inbox(" in src
        or re.search(r"unique\s*=\s*True|UniqueConstraint|primary_key\s*=\s*True", src)
    )


def test_dev_email_sent_through_notifier() -> None:
    """Email goes through the notification capability, never an SDK/smtplib."""
    src = text(WELCOME)
    assert "notifier(" in src and "smtplib" not in src


def test_dev_end_to_end_consumer_test() -> None:
    """A test drives the event through the registered consumer, not the bare handler."""
    tests = "\n".join(p.read_text() for p in module_tests(WELCOME))
    assert re.search(r"\bdeliver\(|\.publish\(|\.drain\(\)|bus\.", tests)


#: The module's API, as the gateway must route it in the microservices profile.
WELCOME_API = "/api/v1/welcome"
LOCATION_RE = re.compile(r"\blocation\s+(=|\^~|~\*?)?\s*(\S+)\s*\{")


def gateway_locations(conf: str) -> list[tuple[str, str, str]]:
    """``location`` blocks of an nginx config: (modifier, path, block body)."""
    found = []
    for m in LOCATION_RE.finditer(conf):
        depth, i = 1, m.end()
        while i < len(conf) and depth:
            depth += {"{": 1, "}": -1}.get(conf[i], 0)
            i += 1
        found.append((m.group(1) or "", m.group(2), conf[m.end() : i - 1]))
    return found


def routes_welcome(modifier: str, path: str, body: str) -> bool:
    """A proxying location for the module itself (not the 404 catch-all, not a
    mention in a comment or a compose file) that matches its API prefix."""
    if "welcome" not in path or "proxy_pass" not in body:
        return False
    probes = (WELCOME_API, WELCOME_API + "/status")
    if modifier.startswith("~"):
        flags = re.I if modifier == "~*" else 0
        return any(re.search(path.strip("\"'"), p, flags) for p in probes)
    if modifier == "=":
        return path in probes
    return any(p.startswith(path.rstrip("/")) for p in probes)


def test_dev_reachable_in_microservices_profile() -> None:
    """The gateway routes the new module's API: a ``location`` for
    ``/api/v1/welcome`` in ``deploy/gateway/nginx.conf`` (what ``sandloom add
    deploy`` writes), not just any mention of "welcome" under ``deploy/``.

    An ``sandloom new`` app ships no microservices profile (no gateway): the rule
    does not apply there unless the agent created one."""
    conf = deploy_dir() / "gateway" / "nginx.conf"
    if app_mode() and not conf.is_file():
        pytest.skip("the app has no microservices profile (no deploy/gateway)")
    text_ = conf.read_text(errors="replace") if conf.is_file() else ""
    assert any(routes_welcome(*loc) for loc in gateway_locations(text_)), (
        f"no gateway location routes {WELCOME_API}",
        [p for p in changed_files() if p.startswith("deploy/")],
    )
