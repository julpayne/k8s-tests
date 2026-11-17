"""Thin helpers over the Kubernetes Python client used by the tests."""

from __future__ import annotations

import os

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

DEFAULT_CONTEXT = "kind-trustyai"
DEFAULT_NAMESPACE = "system"


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
    return client.CoreV1Api(), client.AppsV1Api()


def read_deployment(apps_client: client.AppsV1Api, name: str, namespace: str) -> client.V1Deployment:
    """Return the named Deployment, failing with the harness hint when absent."""
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


def _pods_for(deployment: client.V1Deployment, core_client: client.CoreV1Api, namespace: str) -> list[client.V1Pod]:
    selector = deployment.spec.selector.match_labels or {}
    label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    return core_client.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items


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
    problems = []

    available = status.available_replicas or 0
    if available < desired:
        problems.append(f"available replicas {available}/{desired}")

    conditions = {condition.type: condition for condition in status.conditions or []}
    available_condition = conditions.get("Available")
    if available_condition is None:
        problems.append("Available condition is absent")
    elif available_condition.status != "True":
        problems.append(f"Available condition is {available_condition.status} ({available_condition.reason})")

    pods = _pods_for(deployment, core_client, namespace)
    if not pods:
        problems.append("no pods match the deployment selector")
    for pod in pods:
        problems.extend(_pod_problems(pod))

    if problems:
        details = "\n  - ".join(problems)
        pytest.fail(f"deployment {namespace}/{name} is not healthy:\n  - {details}", pytrace=False)
