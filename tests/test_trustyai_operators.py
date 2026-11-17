"""Health checks for the TrustyAI operators deployed by the Kind harness.

The workload operator is deployed by the module operator from the manifests
baked into the module image, so both Deployments appearing and staying Ready is
the first end-to-end signal that the harness works.
"""

from __future__ import annotations

import logging

import pytest
from kubernetes import client

from utilities.kube import assert_deployment_healthy, container_images, read_deployment

LOGGER = logging.getLogger(__name__)

OPERATOR_DEPLOYMENTS = [
    pytest.param("trustyai-operator-module-controller-manager", id="module-operator"),
    pytest.param("trustyai-service-operator-controller-manager", id="workload-operator"),
]

EXPECTED_REPLICAS = 1


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_exists(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s exists", namespace, name)
    deployment = read_deployment(apps_client, name=name, namespace=namespace)
    LOGGER.info(
        "deployment %s/%s exists: %s replica(s), image(s) %s",
        namespace,
        name,
        deployment.spec.replicas,
        container_images(deployment),
    )

    assert deployment.metadata.name == name
    assert deployment.spec.replicas, f"deployment {namespace}/{name} asks for no replicas"


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_has_single_replica(apps_client: client.AppsV1Api, namespace: str, name: str) -> None:
    LOGGER.info("checking that deployment %s/%s asks for %d replica", namespace, name, EXPECTED_REPLICAS)
    deployment = read_deployment(apps_client, name=name, namespace=namespace)
    LOGGER.info(
        "deployment %s/%s wants %s replica(s), %s ready",
        namespace,
        name,
        deployment.spec.replicas,
        deployment.status.ready_replicas or 0,
    )

    assert deployment.spec.replicas == EXPECTED_REPLICAS, (
        f"deployment {namespace}/{name} asks for {deployment.spec.replicas} replica(s), expected {EXPECTED_REPLICAS}"
    )


@pytest.mark.parametrize("name", OPERATOR_DEPLOYMENTS)
def test_deployment_is_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, namespace: str, name: str
) -> None:
    LOGGER.info("checking that deployment %s/%s and its pods are healthy", namespace, name)
    assert_deployment_healthy(apps_client, core_client, name=name, namespace=namespace)
