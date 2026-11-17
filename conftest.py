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
