"""Thin helpers over the Kubernetes Python client used by the tests."""

from __future__ import annotations

import logging
import os
import time

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

DEFAULT_CONTEXT = "kind-trustyai"
DEFAULT_NAMESPACE = "system"

LOGGER = logging.getLogger(__name__)


def context_from_env() -> str:
    """Kubernetes context under test; the Makefile creates `kind-trustyai`."""
    return os.environ.get("KUBE_CONTEXT", DEFAULT_CONTEXT)


def namespace_from_env() -> str:
    """Namespace under test; the Makefile deploys into `system`."""
    return os.environ.get("NAMESPACE", DEFAULT_NAMESPACE)


def load_clients(context: str) -> tuple[client.CoreV1Api, client.AppsV1Api]:
    """Build API clients for `context`.

    A missing kubeconfig or context means the harness was never pointed at a
    cluster, so the suite skips instead of failing. An unreachable API server is
    a genuine failure and surfaces from the first API call.
    """
    try:
        config.load_kube_config(context=context)
    except (ConfigException, FileNotFoundError) as exc:
        pytest.skip(f"no usable kubeconfig for context {context!r}: {exc}")
    api_client = client.ApiClient()
    LOGGER.info("using kubeconfig context %s (%s)", context, api_client.configuration.host)
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client)


def read_deployment(apps_client: client.AppsV1Api, name: str, namespace: str) -> client.V1Deployment:
    """Return the named Deployment, failing with the harness hint when absent."""
    LOGGER.info("reading deployment %s/%s", namespace, name)
    try:
        return apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
    # Outside the handler, so the API error does not get chained onto the failure.
    pytest.fail(
        f"deployment {namespace}/{name} was not found on context {context_from_env()!r}; "
        "run 'make up' or 'make deploy' first, or point KUBE_CONTEXT/NAMESPACE at a cluster that has it",
        pytrace=False,
    )


def container_images(deployment: client.V1Deployment) -> list[str]:
    """Images the Deployment's pod template runs, in container order."""
    return [container.image for container in deployment.spec.template.spec.containers]


def _label_selector(deployment: client.V1Deployment) -> str:
    selector = deployment.spec.selector.match_labels or {}
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def _pods_for(deployment: client.V1Deployment, core_client: client.CoreV1Api, namespace: str) -> list[client.V1Pod]:
    return core_client.list_namespaced_pod(namespace=namespace, label_selector=_label_selector(deployment)).items


def _describe_conditions(deployment: client.V1Deployment) -> str:
    conditions = deployment.status.conditions or []
    return ", ".join(f"{condition.type}={condition.status} ({condition.reason})" for condition in conditions) or "none"


def _describe_pod(pod: client.V1Pod) -> str:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for status in statuses if status.ready)
    restarts = sum(status.restart_count or 0 for status in statuses)
    return f"phase={pod.status.phase} containers={ready}/{len(statuses)} ready restarts={restarts}"


def _container_not_ready_reason(status: client.V1ContainerStatus) -> str:
    state = status.state
    if state is None:
        return "no state reported"
    if state.waiting is not None:
        return f"waiting: {state.waiting.reason}"
    if state.terminated is not None:
        return f"terminated: {state.terminated.reason}"
    return "running but not ready"


def _pod_problems(pod: client.V1Pod) -> list[str]:
    name = pod.metadata.name
    problems = []
    if pod.status.phase != "Running":
        problems.append(f"pod/{name} phase is {pod.status.phase}")
    for container_status in pod.status.container_statuses or []:
        if not container_status.ready:
            reason = _container_not_ready_reason(container_status)
            problems.append(f"pod/{name} container {container_status.name} is not ready ({reason})")
    return problems


def assert_deployment_healthy(
    apps_client: client.AppsV1Api, core_client: client.CoreV1Api, name: str, namespace: str
) -> None:
    """Fail with a readable summary unless the Deployment and its pods are healthy."""
    deployment = read_deployment(apps_client, name, namespace)
    desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
    status = deployment.status
    problems: list[str] = []

    LOGGER.info("%s/%s images: %s", namespace, name, container_images(deployment))
    LOGGER.info("%s/%s conditions: %s", namespace, name, _describe_conditions(deployment))
    available = status.available_replicas or 0
    LOGGER.info(
        "%s/%s replicas: %d/%d available, %d/%d ready, %d updated",
        namespace,
        name,
        available,
        desired,
        status.ready_replicas or 0,
        desired,
        status.updated_replicas or 0,
    )
    if available < desired:
        problems.append(f"available replicas {available}/{desired}")

    conditions = {condition.type: condition for condition in status.conditions or []}
    available_condition = conditions.get("Available")
    if available_condition is None:
        problems.append("Available condition is absent")
    else:
        LOGGER.info(
            "%s/%s Available condition: %s (%s)",
            namespace,
            name,
            available_condition.status,
            available_condition.reason,
        )
        if available_condition.status != "True":
            problems.append(f"Available condition is {available_condition.status} ({available_condition.reason})")

    pods = _pods_for(deployment, core_client, namespace)
    LOGGER.info(
        "%s/%s pods: %d matching selector %s", namespace, name, len(pods), _label_selector(deployment) or "<empty>"
    )
    if not pods:
        problems.append("no pods match the deployment selector")
    for pod in pods:
        LOGGER.info("%s/%s pod %s: %s", namespace, name, pod.metadata.name, _describe_pod(pod))
        problems.extend(_pod_problems(pod))

    if problems:
        details = "\n  - ".join(problems)
        pytest.fail(f"deployment {namespace}/{name} is not healthy:\n  - {details}", pytrace=False)
    LOGGER.info("%s/%s is healthy", namespace, name)


def wait_for_deployment_available(
    apps_client: client.AppsV1Api,
    name: str,
    namespace: str,
    timeout: int = 300,
    interval: int = 5,
) -> client.V1Deployment:
    """Poll until the Deployment has finished rolling out."""
    deadline = time.monotonic() + timeout
    deployment = read_deployment(apps_client, name=name, namespace=namespace)
    while True:
        desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        status = deployment.status
        ready = status.ready_replicas or 0
        updated = status.updated_replicas or 0
        # A complete rollout, not just one available replica: while an old
        # ReplicaSet is still draining, two pods exist and a test that looks at
        # this Deployment's pods sees a moving target.
        if ready == desired and updated == desired and not status.unavailable_replicas:
            LOGGER.info("%s/%s is rolled out: %d/%d ready", namespace, name, ready, desired)
            return deployment
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "%s/%s rolling out: %d/%d ready, %d/%d updated: %s",
            namespace,
            name,
            ready,
            desired,
            updated,
            desired,
            _describe_conditions(deployment),
        )
        time.sleep(interval)
        deployment = read_deployment(apps_client, name=name, namespace=namespace)
    pytest.fail(
        f"deployment {namespace}/{name} did not become available within {timeout}s: {_describe_conditions(deployment)}",
        pytrace=False,
    )
