"""Fixtures shared by the TrustyAI operator test modules."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes import client
from kubernetes.dynamic import DynamicClient

from utilities.evalhub import (
    CONDITION_READY,
    EVALHUB_NAME,
    EVALHUB_TENANT_LABEL_KEY,
    EVALHUB_TENANT_LABEL_VALUE,
    create_evalhub_instance,
    create_namespace,
    delete_namespace,
    evalhub_api_version,
    seed_instance_tls,
    wait_for_evalhub_condition,
)
from utilities.kube import wait_for_deployment_available
from utilities.trustyai import MODULE_CRD_NAME, MODULE_OPERATOR_DEPLOYMENT, crd_exists, deployment_exists


@pytest.fixture(scope="session")
def modular_trustyai_architecture(api_client: client.ApiClient, apps_client: client.AppsV1Api, namespace: str) -> None:
    """Skip the module checks when the cluster has not adopted the modular operator path.

    On the Kind harness this always holds, because the harness deploys the module
    operator itself. The fixture is what lets the same suite point at a platform
    where TrustyAI is still served by the single operator.
    """
    if not crd_exists(api_client, MODULE_CRD_NAME):
        pytest.skip(
            f"TrustyAI module CRD {MODULE_CRD_NAME} is not installed; "
            "modular architecture tests require the platform module handler"
        )
    if not deployment_exists(apps_client, MODULE_OPERATOR_DEPLOYMENT, namespace):
        pytest.skip(
            f"deployment {namespace}/{MODULE_OPERATOR_DEPLOYMENT} is not present; modular architecture is not enabled"
        )


@pytest.fixture(scope="session")
def evalhub_instance_namespace(core_client: client.CoreV1Api) -> Iterator[str]:
    """Namespace holding the EvalHub instance the tests create, removed afterwards."""
    name = f"trustyai-evalhub-{uuid.uuid4().hex[:6]}"
    create_namespace(core_client, name)
    try:
        yield name
    finally:
        delete_namespace(core_client, name)


@pytest.fixture(scope="session")
def evalhub_instance(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    dynamic_client: DynamicClient,
    api_client: client.ApiClient,
    evalhub_instance_namespace: str,
) -> dict[str, Any]:
    """Create an EvalHub instance from Python and wait until it serves.

    The tests own the instance, its namespace and, because Kind has no service
    CA, its TLS material; the operator owns everything below that.
    """
    seed_instance_tls(core_client, EVALHUB_NAME, evalhub_instance_namespace)
    api_version = evalhub_api_version(api_client)
    create_evalhub_instance(dynamic_client, api_version, EVALHUB_NAME, evalhub_instance_namespace)
    instance = wait_for_evalhub_condition(
        dynamic_client,
        api_version,
        EVALHUB_NAME,
        evalhub_instance_namespace,
        CONDITION_READY,
    )
    wait_for_deployment_available(apps_client, EVALHUB_NAME, evalhub_instance_namespace)
    return instance


@pytest.fixture(scope="session")
def evalhub_tenant_namespace(core_client: client.CoreV1Api, evalhub_instance: dict[str, Any]) -> Iterator[str]:
    """A namespace labelled as an EvalHub tenant, which the operator seeds."""
    name = f"trustyai-evalhub-tenant-{uuid.uuid4().hex[:6]}"
    create_namespace(core_client, name, labels={EVALHUB_TENANT_LABEL_KEY: EVALHUB_TENANT_LABEL_VALUE})
    try:
        yield name
    finally:
        delete_namespace(core_client, name)
