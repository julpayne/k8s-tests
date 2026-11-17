"""Health checks for the TrustyAI operators deployed by the Kind harness.

The workload operator is deployed by the module operator from the manifests
baked into the module image, so both Deployments appearing and staying Ready is
the first end-to-end signal that the harness works.
"""

from __future__ import annotations

import pytest
from kubernetes import client

from utilities.kube import assert_deployment_healthy, read_deployment

OPERATOR_DEPLOYMENTS = [
    pytest.param("trustyai-operator-module-controller-manager", id="module-operator"),
    pytest.param("trustyai-service-operator-controller-manager", id="workload-operator"),
]


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_exists(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    deployment = read_deployment(apps_client, name=name, namespace=namespace)

    assert deployment.metadata.name == name
    assert deployment.spec.replicas, f"deployment {namespace}/{name} asks for no replicas"


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_is_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str, name: str
) -> None:
    assert_deployment_healthy(apps_client, core_client, name=name, namespace=namespace)
