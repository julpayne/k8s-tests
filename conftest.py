"""Shared pytest fixtures.

The tests talk to a live cluster, by default the Kind cluster the Makefile
deploys the operators into. Override the target with KUBE_CONTEXT and
NAMESPACE when running against a different cluster or namespace.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.kube import KEEP_RESOURCES_VAR, context_from_env, keep_resources, load_clients, namespace_from_env

LOGGER = logging.getLogger(__name__)

# The report is named after the project, not after the file pytest-html writes it
# to, and it is prefixed with the project logo.
REPORT_TITLE = "k8s-tests"
REPORT_LOGO = Path(__file__).resolve().parent / "docs" / "trustyai.png"
_TEMP_DIRS: list[Path] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-resources",
        action="store_true",
        default=False,
        help="leave the namespaces, the LLM emulator and the job resources on the cluster for inspection",
    )


def pytest_configure(config: pytest.Config) -> None:
    """`--keep-resources` and KEEP_RESOURCES=1 do the same thing, so the fixtures check one place."""
    if config.getoption("keep_resources"):
        os.environ[KEEP_RESOURCES_VAR] = "1"
    if config.option.htmlpath:
        _install_report_logo(config)


def pytest_unconfigure(config: pytest.Config) -> None:
    for directory in _TEMP_DIRS:
        shutil.rmtree(directory, ignore_errors=True)
    _TEMP_DIRS.clear()


def pytest_html_report_title(report: object) -> None:
    """Name the report after the project instead of the file it is written to."""
    report.title = REPORT_TITLE


def _install_report_logo(config: pytest.Config) -> None:
    """Prefix the report title with the logo, embedded so the file stays portable."""
    if not REPORT_LOGO.exists():
        LOGGER.warning("no report logo at %s; the title will be written without one", REPORT_LOGO)
        return
    encoded = base64.b64encode(REPORT_LOGO.read_bytes()).decode()
    css = (
        "/* k8s-tests: the project logo before the report title. */\n"
        "h1#title::before {\n"
        '  content: "";\n'
        "  display: inline-block;\n"
        "  width: 2.2em;\n"
        "  height: 2.2em;\n"
        "  margin-right: 0.4em;\n"
        "  vertical-align: middle;\n"
        '  background: url("data:image/png;base64,'
        f'{encoded}") no-repeat center / contain;\n'
        "}\n"
    )
    directory = Path(tempfile.mkdtemp(prefix="k8s-tests-report-"))
    _TEMP_DIRS.append(directory)
    path = directory / "report-logo.css"
    path.write_text(css)
    # pytest-html appends the content of every --css file to the report style, so
    # the logo is inlined into the self-contained report rather than linked.
    config.option.css.append(str(path))
    LOGGER.info("prefixing the report title with %s", REPORT_LOGO)


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
    if keep_resources():
        LOGGER.warning(
            "KEEP_RESOURCES is set: teardown will leave the EvalHub instance, its tenant namespace, the "
            "evaluation job and the LLM emulator on the cluster for inspection"
        )
        LOGGER.warning(
            "a re-run will find them: every instance is named 'evalhub' and keeps reconciling every "
            "tenant-labelled namespace, and the namespaces accumulate. Delete them before a clean run: "
            "kubectl get ns -o name | grep trustyai-evalhub | xargs -r kubectl delete, and "
            "kubectl -n %s delete deploy,svc -l app=vllm-emulator",
            namespace,
        )
    try:
        version = client.VersionApi(core_client.api_client).get_code()
    except ApiException as exc:  # a version probe must not fail the run
        LOGGER.warning("could not read the server version: %s", exc.reason)
    else:
        LOGGER.info("server version: %s", version.git_version)


# --- report grouping -------------------------------------------------------
#
# Every test carries an `evalhub`, `emulator`, `module` or `workload` marker. The run is
# ordered by them so the HTML report reads as one block per component, with a
# Scope column naming the block its first row starts.

SCOPE_MARKERS = ("evalhub", "emulator", "module", "workload")
SCOPE_TITLES = {
    "evalhub": "EvalHub",
    "emulator": "LLM emulator",
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
    if keep_resources():
        prefix.append(
            '<p class="col-keep-resources">KEEP_RESOURCES is set: the namespaces, the LLM emulator and the job '
            "resources were left on the cluster. <code>kubectl get ns | grep trustyai-evalhub</code> shows which "
            "ones. A re-run finds them again - the instance keeps reconciling every tenant-labelled namespace, "
            "so delete them first if you want a clean run.</p>"
        )
