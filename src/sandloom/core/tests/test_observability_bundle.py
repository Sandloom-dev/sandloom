"""The shipped observability bundle (deploy/observability/) matches the kernel.

Every metric a Grafana panel or a Prometheus rule names must be registered by
the kernel with the labels the query uses — a renamed metric or label fails
here instead of silently blanking a dashboard or never firing an alert. The
alert index in docs/operate/operator-reference.md must list exactly the shipped alerts with
their severities, and the committed dashboards must be what the generator
produces. ``deploy/observability/check.sh`` adds promtool (config, rules, rule
unit tests) on top; it runs in CI's deploy-profiles job.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import REGISTRY

REPO = Path(__file__).resolve().parents[4]
OBS = REPO / "deploy" / "observability"
RULES = sorted((OBS / "prometheus" / "rules").glob("*.yml"))
DASHBOARDS = sorted((OBS / "grafana" / "dashboards").glob("*.json"))
OPERATIONS = REPO / "docs" / "operate" / "operator-reference.md"

pytestmark = pytest.mark.skipif(
    not OBS.is_dir(), reason="deploy/observability not in this checkout"
)

#: Modules that define Prometheus metrics at import time.
METRIC_MODULES = (
    "sandloom.core.observability.metrics",
    "sandloom.core.auth.hashing",
    "sandloom.core.auth.marks",
    "sandloom.core.auth.verification",
    "sandloom.core.tasks.relay",
    "sandloom.core.eventing.bus_metrics",
    "sandloom.core.eventing.memory_bus",
    "sandloom.core.eventing.pubsub_bus",
    "sandloom.core.eventing.streams_bus",
    "sandloom.core.eventing.streams_transport",
    "sandloom.core.eventing.memory_transport",
    "sandloom.core.eventing.consumer_inbox",
    "sandloom.core.unit_of_work.dispatch",
    "sandloom.core.contract_cache_shadow",
)

#: Series Prometheus itself synthesises for every target.
SYNTHETIC = {"up": frozenset({"job", "instance"})}

#: Labels Prometheus attaches to every scraped series.
TARGET_LABELS = frozenset({"job", "instance"})

_PROMQL_WORDS = frozenset(
    {
        "and",
        "or",
        "unless",
        "bool",
        "offset",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "inf",
        "nan",
    }
)


def _registered() -> dict[str, frozenset[str]]:
    """Series name -> labels a query may use on it, from the live registry."""
    for module in METRIC_MODULES:
        importlib.import_module(module)
    series: dict[str, frozenset[str]] = {}
    # ``_names_to_collectors`` maps every exported series name (``_total``,
    # ``_bucket``, …) to its collector; prometheus_client has no public API
    # exposing a collector's label names.
    for name, collector in REGISTRY._names_to_collectors.items():
        own = set(getattr(collector, "_labelnames", ()))
        if "instance" in own:  # scraped as exported_instance (honor_labels: false)
            own.add("exported_instance")
        if name.endswith("_bucket"):
            own.add("le")
        series[name] = frozenset(own | TARGET_LABELS)
    return series


def _recorded() -> dict[str, frozenset[str]]:
    """Recording rules -> labels they keep (every SLI aggregates ``by (job)``)."""
    out: dict[str, frozenset[str]] = {}
    for path in RULES:
        for group in yaml.safe_load(path.read_text())["groups"]:
            for rule in group["rules"]:
                if "record" in rule:
                    out[rule["record"]] = frozenset({"job"})
    return out


def _alert_rules() -> list[dict[str, Any]]:
    return [
        rule
        for path in RULES
        for group in yaml.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]


_SELECTOR = re.compile(r"([A-Za-z_:][\w:]*)?\s*\{([^{}]*)\}")
_MATCHER = re.compile(r"(\w+)\s*(?:=~|!~|!=|=)")
_GROUPING = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)"
)
_TOKEN = re.compile(r"(?<![\w:$.])([A-Za-z_:][\w:]*)(?![\w:])(?!\s*\()")


def parse_promql(expr: str) -> tuple[dict[str, set[str]], set[str]]:
    """``(metric name -> matcher label keys, grouping labels)`` of ``expr``.

    A deliberately small scanner, not a PromQL parser: enough for the
    selectors, ranges and ``by (...)`` clauses the bundle uses.
    """
    refs: dict[str, set[str]] = {}
    text = re.sub(r"\$\{?\w+\}?", "X", expr)  # Grafana variables

    def _selector(match: re.Match[str]) -> str:
        name = match.group(1) or ""
        keys = set(_MATCHER.findall(match.group(2)))
        if name:
            refs.setdefault(name, set()).update(keys)
        return f" {name} "

    text = _SELECTOR.sub(_selector, text)
    text = re.sub(r"\[[^\]]*\]", " ", text)  # ranges / subqueries
    grouping: set[str] = set()
    for labels in _GROUPING.findall(text):
        grouping.update(label.strip() for label in labels.split(",") if label.strip())
    text = _GROUPING.sub(" ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    for token in _TOKEN.findall(text):
        if token.lower() not in _PROMQL_WORDS and not re.fullmatch(r"[eE]\d*", token):
            refs.setdefault(token, set())
    return refs, grouping


def _bundle_queries() -> list[tuple[str, str]]:
    """``(where, expr)`` for every rule expression and dashboard target."""
    queries: list[tuple[str, str]] = []
    for path in RULES:
        for group in yaml.safe_load(path.read_text())["groups"]:
            for rule in group["rules"]:
                name = rule.get("alert") or rule.get("record")
                queries.append((f"{path.name}:{name}", rule["expr"]))
    for path in DASHBOARDS:
        board = json.loads(path.read_text())
        for panel in board["panels"]:
            for target in panel.get("targets", ()):
                queries.append((f"{path.name}:{panel['title']}", target["expr"]))
        for var in board["templating"]["list"]:
            if var.get("type") == "query":
                definition = var["definition"]
                inner = re.match(r"label_values\((.*),\s*\w+\)$", definition)
                assert inner, f"{path.name}: unsupported variable query {definition}"
                queries.append((f"{path.name}:${var['name']}", inner.group(1)))
    return queries


def test_bundle_is_not_empty() -> None:
    assert len(RULES) >= 2
    assert len(DASHBOARDS) >= 8
    assert len(_alert_rules()) >= 20


def test_parser_sees_metrics_labels_and_grouping() -> None:
    refs, grouping = parse_promql(
        "histogram_quantile(0.99, sum by (le, peer) (rate("
        'sandloom_rpc_client_request_duration_seconds_bucket{job=~"$job", outcome="ok"}'
        "[$__rate_interval]))) > 0.25 and on (job) outbox_dead_total"
    )
    assert refs == {
        "sandloom_rpc_client_request_duration_seconds_bucket": {"job", "outcome"},
        "outbox_dead_total": set(),
    }
    assert grouping == {"le", "peer", "job"}


def test_every_referenced_metric_and_label_exists() -> None:
    known = {**_registered(), **_recorded(), **SYNTHETIC}
    problems: list[str] = []
    for where, expr in _bundle_queries():
        refs, grouping = parse_promql(expr)
        assert refs, f"{where}: no metric found in {expr!r}"
        usable: set[str] = {"le"}
        for name, keys in refs.items():
            if name not in known:
                problems.append(f"{where}: unknown metric {name}")
                continue
            usable |= known[name]
            if bad := keys - known[name]:
                problems.append(f"{where}: {name} has no label(s) {sorted(bad)}")
        if bad := grouping - usable - TARGET_LABELS:
            problems.append(f"{where}: grouping by unknown label(s) {sorted(bad)}")
    assert not problems, "\n".join(problems)


def test_alerts_have_severity_and_runbook() -> None:
    names = [rule["alert"] for rule in _alert_rules()]
    assert len(names) == len(set(names)), "duplicate alert names"
    for rule in _alert_rules():
        assert rule["labels"]["severity"] in {"page", "warning", "info"}, rule["alert"]
        annotations = rule["annotations"]
        assert annotations.get("summary"), rule["alert"]
        url = annotations.get("runbook_url", "")
        assert url.startswith("https://"), rule["alert"]
        doc, _, anchor = url.partition("/blob/main/")[2].partition("#")
        target = REPO / doc
        assert target.is_file(), f"{rule['alert']}: runbook {doc} missing"
        headings = {
            re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
            for line in target.read_text().splitlines()
            if line.startswith("#")
        }
        assert anchor in headings, f"{rule['alert']}: no heading #{anchor} in {doc}"


def _documented_alerts() -> dict[str, str]:
    text = OPERATIONS.read_text()
    section = text.split("\n### Alerts\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(Sandloom\w+)` \| (\w+) \|", section, flags=re.MULTILINE)
    return dict(rows)


def test_operations_alert_index_matches_rules() -> None:
    shipped = {r["alert"]: r["labels"]["severity"] for r in _alert_rules()}
    documented = _documented_alerts()
    assert documented == shipped, (
        "docs/operate/operator-reference.md → Alerts must list every shipped alert with its "
        f"severity.\nonly in rules: {sorted(shipped.keys() - documented.keys())}"
        f"\nonly in docs: {sorted(documented.keys() - shipped.keys())}"
        f"\nseverity differs: "
        f"{sorted(k for k in shipped.keys() & documented.keys() if shipped[k] != documented[k])}"
    )


def test_dashboards_are_valid_and_generated() -> None:
    uids: set[str] = set()
    for path in DASHBOARDS:
        board = json.loads(path.read_text())
        assert board["uid"] not in uids, f"duplicate uid {board['uid']}"
        uids.add(board["uid"])
        ids = [panel["id"] for panel in board["panels"]]
        assert len(ids) == len(set(ids)), f"{path.name}: duplicate panel ids"
    result = subprocess.run(
        [sys.executable, str(OBS / "build_dashboards.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_slo_board_targets_match_burn_alerts() -> None:
    """The burn-rate alerts and the SLO board use the same example budget."""
    slo = (OBS / "prometheus" / "rules" / "sandloom-slo.yml").read_text()
    board = (OBS / "grafana" / "dashboards" / "sandloom-slo.json").read_text()
    assert "14.4 * 0.001" in slo and "6 * 0.001" in slo
    for record in (
        "sandloom:sli_read_errors:ratio_rate",
        "sandloom:sli_write_errors:ratio_rate",
    ):
        assert f'{record}1h{{job=~\\"$job\\"}} / 0.001' in board


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_promtool_rules_and_unit_tests() -> None:  # pragma: no cover - optional
    rules = [str(p) for p in RULES]
    subprocess.run(["promtool", "check", "rules", *rules], check=True)
    subprocess.run(
        ["promtool", "test", "rules", "sandloom-rules.test.yml"],
        cwd=OBS / "prometheus" / "tests",
        check=True,
    )


def test_overlay_is_opt_in_and_mounts_the_bundle() -> None:
    overlay = REPO / "deploy" / "docker-compose.observability.yml"
    services = yaml.safe_load(overlay.read_text())["services"]
    assert set(services) == {"prometheus", "grafana"}
    for name, service in services.items():
        assert service.get("profiles") == ["observability"], (
            f"{name} must stay behind the observability profile (never started "
            "by a plain `up`)"
        )
        for port in service.get("ports", ()):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port}"
        for volume in service.get("volumes", ()):
            source = volume.split(":", 1)[0]
            if source.startswith("."):
                assert (overlay.parent / source).exists(), f"{name}: {source}"
    config = yaml.safe_load((OBS / "prometheus" / "prometheus.yml").read_text())
    (sandloom_job,) = [
        j for j in config["scrape_configs"] if j["job_name"] == "sandloom"
    ]
    assert sandloom_job["http_headers"]["X-Internal-Token"]["files"] == [
        "/run/secrets/metrics_token"
    ]
