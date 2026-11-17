"""Helpers for the EvalHub checks.

The tests create the EvalHub instance, its tenant namespace and, because Kind has
no service CA, its TLS material; the operator owns everything below that. The API
checks call EvalHub through a `kubectl port-forward` to its Service, since the
harness has neither an Ingress nor an OpenShift Route.

The kube-rbac-proxy sidecar serves the TLS endpoint with the certificate the
tests generate, whose subject alternative names are the in-cluster Service names.
A forwarded port is reached as 127.0.0.1, so the API checks do not verify the
certificate; they are not a TLS test.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError

from utilities.certs import generate_serving_cert, service_dns_names

LOGGER = logging.getLogger(__name__)

EVALHUB_API_GROUP = "trustyai.opendatahub.io"
EVALHUB_KIND = "EvalHub"
EVALHUB_PLURAL = "evalhubs"
EVALHUB_CRD_NAME = f"{EVALHUB_PLURAL}.{EVALHUB_API_GROUP}"
EVALHUB_NAME = "evalhub"
EVALHUB_SERVICE_PORT = 8443
EVALHUB_HEALTH_PATH = "/api/v1/health"
EVALHUB_HEALTH_STATUS_HEALTHY = "healthy"
EVALHUB_CONTAINER_NAME = "evalhub"
EVALHUB_PROXY_CONTAINER_NAME = "kube-rbac-proxy"
EVALHUB_APP_LABEL = "eval-hub"
EVALHUB_COMPONENT_LABEL = "api"
EVALHUB_DATABASE_BACKEND = "sqlite"
EVALHUB_IMAGE_VAR = "EVALHUB_IMG"
EVALHUB_IMAGE_RELATED_ENV = "RELATED_IMAGE_ODH_EVAL_HUB_IMAGE"
# A namespace carrying this label is a tenant of an EvalHub instance: the
# operator seeds its discovery, service CA and MLflow CA ConfigMaps there.
EVALHUB_TENANT_LABEL_KEY = "evalhub.trustyai.opendatahub.io/tenant"
EVALHUB_TENANT_LABEL_VALUE = "true"
# ConfigMaps the operator writes into a tenant namespace.
EVALHUB_TENANT_CONFIG_MAPS = (
    f"{EVALHUB_NAME}-service-ca",
    f"{EVALHUB_NAME}-mlflow-ca-bundle",
    "evalhub-discovery",
)
CONDITION_READY = "Ready"
CONDITION_MONITORING_DEGRADED = "MonitoringDegraded"

DEFAULT_WAIT_TIMEOUT = 300
WAIT_INTERVAL = 5
PORT_FORWARD_TIMEOUT = 30.0


def evalhub_api_version(api_client: client.ApiClient) -> str:
    """Storage version of the EvalHub CRD, so the client never guesses."""
    crd = client.ApiextensionsV1Api(api_client).read_custom_resource_definition(name=EVALHUB_CRD_NAME)
    versions = crd.spec.versions or []
    storage = [version.name for version in versions if version.storage]
    if storage:
        return storage[0]
    if not versions:
        pytest.fail(f"CRD {EVALHUB_CRD_NAME} declares no versions", pytrace=False)
    return versions[0].name


def _evalhub_resource(dynamic_client: DynamicClient, api_version: str) -> Any:
    return dynamic_client.resources.get(
        api_version=f"{EVALHUB_API_GROUP}/{api_version}",
        kind=EVALHUB_KIND,
    )


def read_evalhub(dynamic_client: DynamicClient, api_version: str, name: str, namespace: str) -> dict[str, Any]:
    """Return the named EvalHub resource as a dict."""
    LOGGER.info("reading %s/%s in %s as %s", EVALHUB_KIND, name, namespace, api_version)
    try:
        return _evalhub_resource(dynamic_client, api_version).get(name=name, namespace=namespace).to_dict()
    except NotFoundError:
        pytest.fail(
            f"{EVALHUB_KIND}/{name} was not found in {namespace}; run 'make up' or 'make deploy' first",
            pytrace=False,
        )


def create_evalhub_instance(
    dynamic_client: DynamicClient,
    api_version: str,
    name: str,
    namespace: str,
    replicas: int = 1,
    database: str = EVALHUB_DATABASE_BACKEND,
) -> dict[str, Any]:
    """Create an EvalHub resource from Python, the way a user would."""
    body = {
        "apiVersion": f"{EVALHUB_API_GROUP}/{api_version}",
        "kind": EVALHUB_KIND,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"replicas": replicas, "database": {"type": database}},
    }
    LOGGER.info("creating %s/%s in %s with %s database", EVALHUB_KIND, name, namespace, database)
    return _evalhub_resource(dynamic_client, api_version).create(body=body, namespace=namespace).to_dict()


def evalhub_conditions(evalhub: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map the EvalHub resource's status conditions by type."""
    status = evalhub.get("status") or {}
    return {condition.get("type"): condition for condition in status.get("conditions") or []}


def evalhub_condition(evalhub: dict[str, Any], condition_type: str) -> str | None:
    """Return the status string of an EvalHub condition, if it is reported."""
    condition = evalhub_conditions(evalhub).get(condition_type)
    return condition.get("status") if condition else None


def describe_evalhub_conditions(evalhub: dict[str, Any]) -> str:
    """Readable one-line summary of every EvalHub condition."""
    conditions = evalhub_conditions(evalhub)
    if not conditions:
        return "none"
    return ", ".join(
        f"{condition_type}={condition.get('status')} ({condition.get('reason')})"
        for condition_type, condition in sorted(conditions.items())
    )


def wait_for_evalhub_condition(
    dynamic_client: DynamicClient,
    api_version: str,
    name: str,
    namespace: str,
    condition_type: str,
    status: str = "True",
    timeout: int = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    """Poll the EvalHub resource until `condition_type` reports `status`."""
    deadline = time.monotonic() + timeout
    observed: dict[str, Any] = {}
    while True:
        observed = read_evalhub(dynamic_client, api_version, name, namespace)
        if evalhub_condition(observed, condition_type) == status:
            LOGGER.info("%s/%s %s=%s", EVALHUB_KIND, name, condition_type, status)
            return observed
        if time.monotonic() >= deadline:
            break
        LOGGER.info(
            "waiting for %s=%s on %s/%s; conditions: %s",
            condition_type,
            status,
            EVALHUB_KIND,
            name,
            describe_evalhub_conditions(observed),
        )
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"{EVALHUB_KIND}/{name} in {namespace} did not report {condition_type}={status} within {timeout}s; "
        f"conditions: {describe_evalhub_conditions(observed)}",
        pytrace=False,
    )


def create_namespace(core_client: client.CoreV1Api, name: str, labels: dict[str, str] | None = None) -> str:
    """Create a namespace, optionally labelled, and return its name."""
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels or {}))
    core_client.create_namespace(body=body)
    LOGGER.info("created namespace %s with labels %s", name, labels or {})
    return name


def delete_namespace(core_client: client.CoreV1Api, name: str) -> None:
    """Delete a namespace, tolerating one that is already gone."""
    try:
        core_client.delete_namespace(name=name)
    except ApiException as exc:
        if exc.status != 404:
            raise
    LOGGER.info("deleted namespace %s", name)


def seed_instance_tls(core_client: client.CoreV1Api, name: str, namespace: str) -> None:
    """Create the TLS material a self-managed EvalHub instance needs on Kind.

    The operator mounts `<name>-tls` into its kube-rbac-proxy sidecar and reads
    the upstream CA from the `<name>-service-ca` ConfigMap it creates with no
    data, expecting the OpenShift service CA to fill it in. Kind has no service
    CA, so the tests mint the certificate and fill both in.
    """
    cert_pem, key_pem = generate_serving_cert(dns_names=service_dns_names(service=name, namespace=namespace))
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=f"{name}-tls", namespace=namespace),
        type="kubernetes.io/tls",
        data={
            "tls.crt": base64.b64encode(cert_pem).decode(),
            "tls.key": base64.b64encode(key_pem).decode(),
        },
    )
    core_client.create_namespaced_secret(namespace=namespace, body=secret)

    config_map = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=f"{name}-service-ca", namespace=namespace),
        data={"service-ca.crt": cert_pem.decode()},
    )
    core_client.create_namespaced_config_map(namespace=namespace, body=config_map)
    LOGGER.info("seeded %s-tls and %s-service-ca in %s", name, name, namespace)


def tenant_config_maps(core_client: client.CoreV1Api, namespace: str) -> set[str]:
    """Names of the EvalHub ConfigMaps present in a namespace."""
    names = {item.metadata.name for item in core_client.list_namespaced_config_map(namespace=namespace).items}
    LOGGER.info("%s ConfigMaps: %s", namespace, sorted(names))
    return names


def container_image(deployment: client.V1Deployment, container_name: str) -> str | None:
    """Image of the named container in a Deployment's pod template."""
    for container in deployment.spec.template.spec.containers or []:
        if container.name == container_name:
            return container.image
    return None


def _kubectl() -> str:
    kubectl = os.environ.get("KUBECTL") or "kubectl"
    if shutil.which(kubectl) is None:
        pytest.skip(f"{kubectl} is not installed, so the EvalHub API cannot be reached")
    return kubectl


def _wait_for_local_port(port: int, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"port-forward exited before serving port {port}:\n{output}", pytrace=False)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"port-forward did not serve port {port} within {timeout}s", pytrace=False)


@contextlib.contextmanager
def port_forward(context: str, namespace: str, target: str, local_port: int, remote_port: int) -> Iterator[str]:
    """Forward `local_port` to `target` and yield the local base URL."""
    command = [
        _kubectl(),
        "--context",
        context,
        "--namespace",
        namespace,
        "port-forward",
        target,
        f"{local_port}:{remote_port}",
    ]
    LOGGER.info("forwarding 127.0.0.1:%d to %s/%s:%d", local_port, namespace, target, remote_port)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_local_port(local_port, process, PORT_FORWARD_TIMEOUT)
        yield f"https://127.0.0.1:{local_port}"
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()


def request(
    method: str,
    url: str,
    timeout: int = 10,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Send a request to EvalHub without verifying the certificate the tests minted."""
    LOGGER.info("%s %s", method.upper(), url)
    return requests.request(method, url, headers=headers, verify=False, timeout=timeout)
