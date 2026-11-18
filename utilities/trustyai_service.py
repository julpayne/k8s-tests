"""The TrustyAI service: the resource, its operand and the API behind it.

A `TrustyAIService` is what a data scientist talks to. The workload operator turns
it into a Deployment whose API sits behind a kube-rbac-proxy, and the checks here
create that resource themselves instead of relying on the harness, the way a user
would. Two things the platform provides are supplied by the fixtures: the serving
certificates, which OpenShift's service CA would issue from the serving-cert
annotations, and a caller identity, because the proxy authorizes every request
with a SubjectAccessReview.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.certs import generate_serving_cert, service_dns_names

LOGGER = logging.getLogger(__name__)

API_GROUP = "trustyai.opendatahub.io"
CRD_NAME = "trustyaiservices.trustyai.opendatahub.io"
PLURAL = "trustyaiservices"
# v1 is the storage version and v1alpha1 the one earlier consumers created; the
# CRD serves both and converts between them with a webhook.
API_VERSIONS = ("v1", "v1alpha1")
STORAGE_VERSION = "v1"

SERVICE_NAME = "trustyai-service"
# The operand cannot start before the operator has created it and pulled its
# image, which on a cold cluster is the slowest part of the run.
OPERAND_TIMEOUT = 600
APP_CONTAINER = "trustyai-service"
PROXY_CONTAINER = "kube-rbac-proxy"
INTERNAL_PROXY_CONTAINER = "internal-tls-proxy"
SERVICE_CONTAINERS = (APP_CONTAINER, PROXY_CONTAINER, INTERNAL_PROXY_CONTAINER)
TLS_SECRET_SUFFIX = "-tls"
INTERNAL_SECRET_SUFFIX = "-internal"

# The Service the app's health endpoint is on, and the TLS one the proxy is on.
HEALTH_SERVICE = SERVICE_NAME
HEALTH_SERVICE_PORT = 80
TLS_SERVICE_SUFFIX = "-tls"
TLS_SERVICE_PORT = 443

STORAGE_FOLDER = "/inputs"
STORAGE_SIZE = "1Gi"
DATA_FILENAME = "data.csv"
DATA_FORMAT = "CSV"
METRICS_SCHEDULE = "5s"

# Local ports for the port-forwards. They are read from the environment so two
# runs can share a machine, and they differ from the EvalHub ones for the same
# reason.
API_FORWARD_VAR = "TRUSTYAI_SERVICE_API_PORT"
HEALTH_FORWARD_VAR = "TRUSTYAI_SERVICE_HEALTH_PORT"
DEFAULT_API_FORWARD_PORT = 18443
DEFAULT_HEALTH_FORWARD_PORT = 18080

HEALTH_PATH = "/q/health"
INFO_PATH = "/info"
UPLOAD_PATH = "/data/upload"
DRIFT_PATH = "/metrics/drift/comparemeans"
DRIFT_SCHEDULE_PATH = "/metrics/drift/comparemeans/request"
DRIFT_REQUESTS_PATH = "/metrics/drift/comparemeans/requests"

# The identity the checks call the API as. The proxy authorizes `get` on the
# Service the request arrives at, which is what the Role below grants.
IDENTITY_NAME = "trustyai-api"

MODEL_NAME = "k8s-tests-model"
INPUT_TENSOR = "credit_inputs"
# Enough rows for CompareMeans to separate the two distributions, and the shift
# the production upload applies so the drift check has something to detect.
TRAINING_ROWS = 1000
PRODUCTION_ROWS = 200
PRODUCTION_SHIFT = 15.0
DATA_SEED = 7


@dataclass(frozen=True)
class ServiceAddress:
    """Where a running service answers, and what it takes to call it."""

    namespace: str
    name: str
    token: str
    ca_file: Path
    api_url: str
    health_url: str

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> requests.Response:
        """Send one request to the API through the TLS Service."""
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        LOGGER.info("%s %s%s", method, self.api_url, path)
        return requests.request(
            method,
            f"{self.api_url}{path}",
            headers=headers,
            data=json.dumps(body) if body is not None else None,
            verify=str(self.ca_file),
            timeout=120,
        )

    def health(self) -> requests.Response:
        """Read the app's own health endpoint, which needs no credentials."""
        LOGGER.info("GET %s%s", self.health_url, HEALTH_PATH)
        return requests.get(f"{self.health_url}{HEALTH_PATH}", timeout=30)


def forwarded_api_port() -> int:
    """Local port the API forward listens on."""
    return int(os.environ.get(API_FORWARD_VAR) or DEFAULT_API_FORWARD_PORT)


def forwarded_health_port() -> int:
    """Local port the health forward listens on."""
    return int(os.environ.get(HEALTH_FORWARD_VAR) or DEFAULT_HEALTH_FORWARD_PORT)


def service_manifest(name: str) -> dict[str, Any]:
    """The TrustyAIService to create, with PVC storage so the data survives a restart."""
    return {
        "apiVersion": f"{API_GROUP}/{STORAGE_VERSION}",
        "kind": "TrustyAIService",
        "metadata": {"name": name},
        "spec": {
            "storage": {"format": "PVC", "folder": STORAGE_FOLDER, "size": STORAGE_SIZE},
            "data": {"filename": DATA_FILENAME, "format": DATA_FORMAT},
            "metrics": {"schedule": METRICS_SCHEDULE},
            "replicas": 1,
        },
    }


def create_service(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> dict[str, Any]:
    """Create the TrustyAIService, replacing the spec of one left behind by an earlier run."""
    body = service_manifest(name)
    try:
        created = custom_api.create_namespaced_custom_object(
            group=API_GROUP, version=STORAGE_VERSION, namespace=namespace, plural=PLURAL, body=body
        )
        LOGGER.info("created TrustyAIService/%s in %s", name, namespace)
        return created
    except ApiException as exc:
        if exc.status != 409:
            raise
        LOGGER.info("TrustyAIService/%s already exists in %s; leaving its spec alone", name, namespace)
        return read_service(custom_api, namespace, name)


def wait_for_operand(
    apps_client: client.AppsV1Api,
    namespace: str,
    name: str = SERVICE_NAME,
    timeout: int = OPERAND_TIMEOUT,
    interval: int = 5,
) -> None:
    """Wait for the operator to create the operand Deployment.

    The operator creates it on its next reconcile, which is not the same instant
    as the resource being accepted, so the rollout wait cannot be the first read.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            apps_client.read_namespaced_deployment(name=name, namespace=namespace)
            LOGGER.info("%s/%s was created by the operator", namespace, name)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise
            if time.monotonic() >= deadline:
                pytest.fail(f"the operator did not create {namespace}/{name} within {timeout}s", pytrace=False)
            time.sleep(interval)


def read_service(
    custom_api: client.CustomObjectsApi, namespace: str, name: str, version: str = STORAGE_VERSION
) -> dict[str, Any]:
    """Read the TrustyAIService through one of the versions the CRD serves."""
    LOGGER.info("reading TrustyAIService/%s in %s as %s", name, namespace, version)
    return custom_api.get_namespaced_custom_object(
        group=API_GROUP, version=version, namespace=namespace, plural=PLURAL, name=name
    )


def condition(service: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    """The named status condition, or None when the operand has not reported it."""
    for entry in service.get("status", {}).get("conditions") or []:
        if entry.get("type") == condition_type:
            return entry
    return None


def describe_conditions(service: dict[str, Any]) -> str:
    """Render the conditions on one line, for a log or an assertion message."""
    conditions = service.get("status", {}).get("conditions") or []
    return ", ".join(f"{entry.get('type')}={entry.get('status')} ({entry.get('reason')})" for entry in conditions)


def ensure_serving_secret(core_client: client.CoreV1Api, namespace: str, secret_name: str, service: str) -> bytes:
    """Create the serving certificate the platform would issue, and return it as a CA bundle.

    The API is reached over a port-forward, so `localhost` is one of the names the
    certificate has to cover for the checks to verify the chain.
    """
    certificate, key = generate_serving_cert([*service_dns_names(service, namespace), "localhost"])
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=secret_name, namespace=namespace),
        type="kubernetes.io/tls",
        data={
            "tls.crt": base64.b64encode(certificate).decode(),
            "tls.key": base64.b64encode(key).decode(),
        },
    )
    try:
        core_client.create_namespaced_secret(namespace=namespace, body=body)
        LOGGER.info("created serving certificate %s/%s", namespace, secret_name)
    except ApiException as exc:
        if exc.status != 409:
            raise
        LOGGER.info("serving certificate %s/%s already exists", namespace, secret_name)
    return certificate


def ensure_identity(
    core_client: client.CoreV1Api,
    rbac_client: client.RbacAuthorizationV1Api,
    namespace: str,
    service_name: str,
    name: str = IDENTITY_NAME,
) -> str:
    """Create the ServiceAccount the checks call the API as, and grant it the Service.

    The proxy runs a SubjectAccessReview for `get` on the Service the request
    arrived at, so the grant names that Service and nothing else.
    """
    service_account = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=name, namespace=namespace))
    role = client.V1Role(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        rules=[
            client.V1PolicyRule(
                api_groups=[""],
                resources=["services"],
                resource_names=[service_name],
                verbs=["get"],
            )
        ],
    )
    binding = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=name),
        subjects=[client.RbacV1Subject(kind="ServiceAccount", name=name, namespace=namespace)],
    )
    for create, replace, body in (
        (
            core_client.create_namespaced_service_account,
            core_client.replace_namespaced_service_account,
            service_account,
        ),
        (rbac_client.create_namespaced_role, rbac_client.replace_namespaced_role, role),
        (rbac_client.create_namespaced_role_binding, rbac_client.replace_namespaced_role_binding, binding),
    ):
        try:
            create(namespace=namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            replace(name=body.metadata.name, namespace=namespace, body=body)
        LOGGER.info("ensured %s %s/%s", body.kind, namespace, body.metadata.name)
    return name


def rows(count: int, shift: float = 0.0) -> list[list[float]]:
    """`count` feature rows, shifted by `shift`, from a fixed seed so runs agree."""
    generator = random.Random(DATA_SEED)
    return [[round(generator.gauss(50.0 + shift, 10.0), 4) for _ in range(4)] for _ in range(count)]


def upload_payload(tag: str, data: list[list[float]], model_name: str = MODEL_NAME) -> dict[str, Any]:
    """One inference pair, which is the shape the upload endpoint takes.

    Only the request half feeds the drift metrics, but the endpoint expects the
    response half too, so it is filled in the way a serving runtime would.
    """
    generator = random.Random(DATA_SEED)
    predictions = [round(generator.gauss(0.3, 0.1), 4) for _ in data]
    return {
        "model_name": model_name,
        "data_tag": tag,
        "request": {"inputs": [{"name": INPUT_TENSOR, "shape": [len(data), 4], "datatype": "FP64", "data": data}]},
        "response": {
            "model_name": model_name,
            "model_version": "1",
            "outputs": [{"name": "predict", "shape": [len(data), 1], "datatype": "FP32", "data": predictions}],
        },
    }


def observations(metadata: dict[str, Any], model_name: str = MODEL_NAME) -> int:
    """The number of observations the service holds for a model."""
    return int(((metadata.get(model_name) or {}).get("data") or {}).get("observations") or 0)


def drift_request(model_name: str = MODEL_NAME) -> dict[str, Any]:
    """A CompareMeans request: TRAINING is the reference, the rest the organic tail."""
    return {"modelId": model_name, "referenceTag": "TRAINING"}
