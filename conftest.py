"""Shared pytest fixtures.

The tests talk to a live cluster, by default the Kind cluster the Makefile
deploys the operators into. Override the target with KUBE_CONTEXT and
NAMESPACE when running against a different cluster or namespace.
"""

from __future__ import annotations

import pytest
from kubernetes import client

from utilities.kube import context_from_env, load_clients, namespace_from_env


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
