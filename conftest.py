"""Shared pytest fixtures.

The tests talk to a live cluster, by default the Kind cluster the Makefile
deploys the operators into. Override the target with KUBE_CONTEXT and
NAMESPACE when running against a different cluster or namespace.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.kube import context_from_env, load_clients, namespace_from_env

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def kube_context() -> str:
    """Kubernetes context the tests run against."""
    return context_from_env()


@pytest.fixture(scope="session")
def namespace() -> str:
    """Namespace the TrustyAI operators are deployed into."""
    return namespace_from_env()


@pytest.fixture(scope="session")
def clients(kube_context: str) -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Core and Apps API clients for the target cluster."""
    return load_clients(context=kube_context)


@pytest.fixture(scope="session")
def core_client(clients: tuple[client.CoreV1Api, client.AppsV1Api]) -> client.CoreV1Api:
    return clients[0]


@pytest.fixture(scope="session")
def apps_client(clients: tuple[client.CoreV1Api, client.AppsV1Api]) -> client.AppsV1Api:
    return clients[1]


@pytest.fixture(scope="session")
def api_client(core_client: client.CoreV1Api) -> client.ApiClient:
    """The raw API client the typed clients share, for discovery-style APIs."""
    return core_client.api_client


@pytest.fixture(scope="session")
def dynamic_client(api_client: client.ApiClient) -> DynamicClient:
    """Dynamic client for custom resources the typed clients do not model."""
    return DynamicClient(api_client)


@pytest.fixture(scope="session", autouse=True)
def report_target(clients: tuple[client.CoreV1Api, client.AppsV1Api], kube_context: str, namespace: str) -> None:
    """Log what the session is pointed at, so the report says where it ran."""
    core_client, _ = clients
    LOGGER.info(
        "target: context=%s namespace=%s server=%s",
        kube_context,
        namespace,
        core_client.api_client.configuration.host,
    )
    try:
        version = client.VersionApi(core_client.api_client).get_code()
    except ApiException as exc:  # a version probe must not fail the run
        LOGGER.warning("could not read the server version: %s", exc.reason)
    else:
        LOGGER.info("server version: %s", version.git_version)


# --- report grouping -------------------------------------------------------
#
# Every test carries a `module`, `workload` or `evalhub` marker. The run is
# ordered by them so the HTML report reads as one block per component, with a
# Scope column naming the block its first row starts.

SCOPE_MARKERS = ("evalhub", "module", "workload")
SCOPE_TITLES = {
    "evalhub": "EvalHub",
    "module": "Module operator",
    "workload": "Workload operator",
}
SCOPE_UNMARKED = "other"

_SCOPES: dict[str, str] = {}
_SCOPE_STARTS: set[str] = set()
_OUTCOMES: dict[str, str] = {}


def _scope_of(item: pytest.Item) -> str:
    for marker in SCOPE_MARKERS:
        if item.get_closest_marker(marker):
            return marker
    return SCOPE_UNMARKED


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Order the run by scope so the report groups the tests by component."""
    order = {scope: index for index, scope in enumerate((*SCOPE_MARKERS, SCOPE_UNMARKED))}
    for item in items:
        _SCOPES[item.nodeid] = _scope_of(item)
    items.sort(key=lambda item: order[_SCOPES[item.nodeid]])
    seen: set[str] = set()
    for item in items:
        scope = _SCOPES[item.nodeid]
        if scope not in seen:
            _SCOPE_STARTS.add(item.nodeid)
            seen.add(scope)


def pytest_html_results_table_header(cells: list[str]) -> None:
    """Add the Scope column to the HTML report's results table."""
    cells.insert(1, "<th>Scope</th>")


def pytest_html_results_table_row(report: pytest.TestReport, cells: list[str]) -> None:
    """Name the scope on the first row of each block, separate the blocks."""
    scope = _SCOPES.get(report.nodeid, SCOPE_UNMARKED)
    title = SCOPE_TITLES.get(scope, scope)
    if report.nodeid in _SCOPE_STARTS:
        cells.insert(
            1,
            f'<td class="col-scope" style="border-top:2px solid #6c757d; white-space:nowrap;">'
            f"<strong>{title}</strong></td>",
        )
    else:
        cells.insert(1, '<td class="col-scope"></td>')


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record one outcome per test, so the summary counts what actually ran."""
    if report.when == "call":
        _OUTCOMES[report.nodeid] = report.outcome
    elif report.when == "setup" and report.outcome != "passed":
        _OUTCOMES.setdefault(report.nodeid, report.outcome)


def pytest_html_results_summary(
    prefix: list[str], summary: list[str], postfix: list[str], session: pytest.Session
) -> None:
    """Say how the report is grouped, above the results table."""
    counts: dict[str, dict[str, int]] = {}
    for nodeid, outcome in _OUTCOMES.items():
        scope = _SCOPES.get(nodeid, SCOPE_UNMARKED)
        counts.setdefault(scope, {})
        counts[scope][outcome] = counts[scope].get(outcome, 0) + 1

    def describe(scope: str) -> str:
        outcomes = counts[scope]
        parts = [
            f"{outcomes[outcome]} {outcome}" for outcome in ("passed", "failed", "skipped") if outcomes.get(outcome)
        ]
        title = SCOPE_TITLES.get(scope, scope)
        return f"{title} ({', '.join(parts)})"

    blocks = [describe(scope) for scope in (*SCOPE_MARKERS, SCOPE_UNMARKED) if counts.get(scope)]
    if blocks:
        prefix.append(f'<p class="col-scope-summary">Grouped by scope: {"; ".join(blocks)}</p>')
