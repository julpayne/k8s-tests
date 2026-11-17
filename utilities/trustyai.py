"""Helpers for the TrustyAI module and workload operator checks.

The checks cover the modular stack: the module operator, the workload operator it
deploys, and the images both run. They stay client-only on purpose: the Kind
harness has no platform operator, no OLM and no CSV, so a check that would need
one of those skips with a reason instead of being faked.

Deployments are read through the Apps API, ConfigMaps through the Core API and
the TrustyAI custom resource through the dynamic client.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

LOGGER = logging.getLogger(__name__)

MODULE_API_GROUP = "components.platform.opendatahub.io"
MODULE_KIND = "TrustyAI"
MODULE_CRD_NAME = "trustyais.components.platform.opendatahub.io"
MODULE_CR_NAME = "default-trustyai"
MODULE_OPERATOR_DEPLOYMENT = "trustyai-operator-module-controller-manager"
WORKLOAD_OPERATOR_DEPLOYMENT = "trustyai-service-operator-controller-manager"

# Env var through which the module operator tells itself which workload operator
# image to render. The harness patches it in hack/deploy.sh.
WORKLOAD_IMAGE_RELATED_ENV = "RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE"
# Image variables the Makefile exports for the images it built.
MODULE_IMAGE_VAR = "MODULE_IMG"
WORKLOAD_IMAGE_VAR = "WORKLOAD_IMG"

# ConfigMap written by the TrustyAI operator itself.
WORKLOAD_CONFIG_MAP = "trustyai-service-operator-config"
WORKLOAD_IMAGE_CONFIG_KEY = "trustyaiOperatorImage"

CONDITION_READY = "Ready"
CONDITION_PROVISIONING_SUCCEEDED = "ProvisioningSucceeded"

DEFAULT_WAIT_TIMEOUT = 300
WAIT_INTERVAL = 5


def crd_exists(api_client: client.ApiClient, name: str) -> bool:
    """Whether the named custom resource definition is installed."""
    try:
        client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=name)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("CRD %s is not installed", name)
        return False
    LOGGER.info("CRD %s is installed", name)
    return True


def deployment_exists(apps_client: client.AppsV1Api, name: str, namespace: str) -> bool:
    """Whether the named Deployment exists, without failing the run when it does not."""
    try:
        apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("deployment %s/%s is not present", namespace, name)
        return False
    return True


def module_api_version(api_client: client.ApiClient) -> str:
    """The storage version of the TrustyAI module CRD, so the client never guesses."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=MODULE_CRD_NAME)
    versions = crd.spec.versions or []
    storage = [version.name for version in versions if version.storage]
    if storage:
        return storage[0]
    if not versions:
        pytest.fail(f"CRD {MODULE_CRD_NAME} declares no versions", pytrace=False)
    return versions[0].name


def _module_resource(dynamic_client: DynamicClient, api_version: str) -> Any:
    try:
        return dynamic_client.resources.get(api_version=f"{MODULE_API_GROUP}/{api_version}", kind=MODULE_KIND)
    except ResourceNotFoundError:
        pytest.fail(
            f"{MODULE_API_GROUP}/{api_version} {MODULE_KIND} is not served on this cluster; "
            "run 'make up' or 'make deploy' first",
            pytrace=False,
        )


def read_module(dynamic_client: DynamicClient, api_version: str) -> dict[str, Any]:
    """Return the singleton TrustyAI module resource as a dict."""
    resource = _module_resource(dynamic_client, api_version)
    LOGGER.info("reading %s/%s", MODULE_API_GROUP, MODULE_CR_NAME)
    try:
        module = resource.get(name=MODULE_CR_NAME).to_dict()
    except NotFoundError:
        pytest.fail(
            f"{MODULE_KIND}/{MODULE_CR_NAME} was not found; the module operator has not reconciled "
            "its custom resource on this cluster",
            pytrace=False,
        )
    return module


def module_conditions(module: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map the module resource's status conditions by type."""
    status = module.get("status") or {}
    return {condition.get("type"): condition for condition in status.get("conditions") or []}


def module_condition(module: dict[str, Any], condition_type: str) -> str | None:
    """Return the status string of a module condition, if the condition is reported."""
    condition = module_conditions(module).get(condition_type)
    return condition.get("status") if condition else None


def describe_module_conditions(module: dict[str, Any]) -> str:
    """Readable one-line summary of every module condition."""
    conditions = module_conditions(module)
    if not conditions:
        return "none"
    return ", ".join(
        f"{condition_type}={condition.get('status')} ({condition.get('reason')})"
        for condition_type, condition in sorted(conditions.items())
    )


def wait_for_module_condition(
    dynamic_client: DynamicClient,
    api_version: str,
    condition_type: str,
    status: str = "True",
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Poll the module resource until `condition_type` reports `status`."""
    deadline = time.monotonic() + timeout
    observed: dict[str, Any] = {}
    while True:
        observed = read_module(dynamic_client=dynamic_client, api_version=api_version)
        if module_condition(observed, condition_type) == status:
            LOGGER.info("%s/%s %s=%s", MODULE_KIND, MODULE_CR_NAME, condition_type, status)
            return observed
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "waiting for %s=%s on %s/%s; conditions: %s",
            condition_type,
            status,
            MODULE_KIND,
            MODULE_CR_NAME,
            describe_module_conditions(observed),
        )
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"{MODULE_KIND}/{MODULE_CR_NAME} did not report {condition_type}={status} within {timeout}s; "
        f"conditions: {describe_module_conditions(observed)}",
        pytrace=False,
    )


def assert_module_ready(
    dynamic_client: DynamicClient,
    api_version: str,
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Wait until the module resource reports Ready=True and return it."""
    return wait_for_module_condition(
        dynamic_client=dynamic_client,
        api_version=api_version,
        condition_type=CONDITION_READY,
        status="True",
        timeout=timeout,
    )


def assert_module_provisioning_succeeded(module: dict[str, Any]) -> None:
    """Assert the module operator finished applying the workload manifests."""
    status = module_condition(module, CONDITION_PROVISIONING_SUCCEEDED)
    assert status == "True", (
        f"expected {CONDITION_PROVISIONING_SUCCEEDED}=True on {MODULE_KIND}/{MODULE_CR_NAME}, got {status!r}; "
        f"conditions: {describe_module_conditions(module)}"
    )


def assert_module_observed_generation(module: dict[str, Any]) -> None:
    """Assert status.observedGeneration tracks the current spec generation."""
    status = module.get("status") or {}
    observed_generation = status.get("observedGeneration")
    generation = (module.get("metadata") or {}).get("generation")
    LOGGER.info("%s/%s generation: %s observed %s", MODULE_KIND, MODULE_CR_NAME, generation, observed_generation)
    assert observed_generation == generation, (
        f"expected observedGeneration={generation} on {MODULE_KIND}/{MODULE_CR_NAME}, got {observed_generation}"
    )


def read_config_map(
    core_client: client.CoreV1Api,
    name: str,
    namespace: str,
) -> client.V1ConfigMap | None:
    """Return the named ConfigMap, or None when the cluster does not have it."""
    try:
        config_map = core_client.read_namespaced_config_map(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        LOGGER.info("ConfigMap %s/%s is not present", namespace, name)
        return None
    LOGGER.info("ConfigMap %s/%s is present with %d key(s)", namespace, name, len(config_map.data or {}))
    return config_map


def deployment_env(apps_client: client.AppsV1Api, name: str, namespace: str, env_name: str) -> str | None:
    """Value of an env var on the Deployment's first container, or None when unset."""
    deployment = apps_client.read_namespaced_deployment(name=name, namespace=namespace)
    containers = deployment.spec.template.spec.containers or []
    if not containers:
        return None
    for env in containers[0].env or []:
        if env.name == env_name:
            LOGGER.info("%s/%s container %s: %s=%s", namespace, name, containers[0].name, env_name, env.value)
            return env.value
    LOGGER.info("%s/%s container %s does not set %s", namespace, name, containers[0].name, env_name)
    return None


def expected_image_from_env(var_name: str) -> str | None:
    """Image the Makefile built and loaded, when the run came through `make test`."""
    image = os.environ.get(var_name) or ""
    return image or None
