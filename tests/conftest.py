"""Fixtures shared by the TrustyAI operator test modules."""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.dynamic import DynamicClient

from utilities.emulator import (
    FORWARDED_PORT,
    deploy_emulator,
    emulator_name,
    emulator_port,
    read_credential,
    remove_emulator,
)
from utilities.emulator import emulator_namespace as emulator_namespace_from_env
from utilities.evalhub import (
    CONDITION_READY,
    EVALHUB_NAME,
    EVALHUB_SERVICE_PORT,
    EVALHUB_TENANT_LABEL_KEY,
    EVALHUB_TENANT_LABEL_VALUE,
    TENANT_IDENTITY_NAME,
    create_evalhub_instance,
    create_namespace,
    create_tenant_identity,
    evalhub_api_version,
    grant_tenant_job_access,
    instance_tls_cert,
    mint_token,
    release_namespace,
    seed_instance_tls,
    seed_tenant_ca,
    wait_for_evalhub_condition,
    wait_for_tenant_rbac,
)
from utilities.kube import keep_resources, wait_for_deployment_available
from utilities.portforward import port_forward
from utilities.trustyai import MODULE_CRD_NAME, MODULE_OPERATOR_DEPLOYMENT, crd_exists, deployment_exists

LOGGER = logging.getLogger(__name__)


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
        release_namespace(core_client, name)


@pytest.fixture(scope="session")
def evalhub_instances_in_cluster(dynamic_client: DynamicClient) -> int:
    """Warn when the cluster holds more than one EvalHub instance.

    Every instance is named `evalhub` and reconciles every tenant-labelled
    namespace, so a leftover instance, or one a `KEEP_RESOURCES` run left behind,
    keeps writing to namespaces it did not create. The run grants its own job
    access, so it does not depend on which instance reconciled last, but the
    leftovers still consume the cluster and are worth naming here.
    """
    plural = dynamic_client.resources.get(api_version="trustyai.opendatahub.io/v1", kind="EvalHub")
    instances = plural.get().items
    if len(instances) > 1:
        namespaces = sorted(instance.metadata.namespace for instance in instances)
        LOGGER.warning(
            "the cluster holds %d EvalHub instances (%s); a leftover instance reconciles every tenant-labelled "
            "namespace, including ones it did not create. Delete the namespaces you do not need.",
            len(instances),
            ", ".join(namespaces),
        )
    return len(instances)


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
    wait_for_deployment_available(apps_client, EVALHUB_NAME, evalhub_instance_namespace, core_client=core_client)
    return instance


@pytest.fixture(scope="session")
def evalhub_tenant_namespace(core_client: client.CoreV1Api, evalhub_instance: dict[str, Any]) -> Iterator[str]:
    """A namespace labelled as an EvalHub tenant, which the operator seeds."""
    name = f"trustyai-evalhub-tenant-{uuid.uuid4().hex[:6]}"
    create_namespace(core_client, name, labels={EVALHUB_TENANT_LABEL_KEY: EVALHUB_TENANT_LABEL_VALUE})
    try:
        yield name
    finally:
        release_namespace(core_client, name)


@pytest.fixture(scope="session")
def emulator_namespace() -> str:
    """Namespace the harness deployed the LLM emulator into."""
    return emulator_namespace_from_env()


@pytest.fixture(scope="session")
def emulator(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    emulator_namespace: str,
) -> Iterator[client.V1Deployment]:
    """Deploy the LLM emulator and remove it when the session ends.

    The checks own it, the way they own the EvalHub instance: the namespace, image,
    port and token are parameters, and nothing is left behind in the cluster.
    """
    deployment = deploy_emulator(core_client, apps_client, emulator_namespace)
    try:
        yield deployment
    finally:
        if keep_resources():
            LOGGER.warning(
                "keeping the LLM emulator in namespace %s: kubectl -n %s get all,cm,secret",
                emulator_namespace,
                emulator_namespace,
            )
        else:
            remove_emulator(core_client, apps_client, emulator_namespace)


@pytest.fixture(scope="session")
def emulator_credentials(
    core_client: client.CoreV1Api,
    emulator_namespace: str,
    emulator: client.V1Deployment,
) -> str:
    """The token the emulator requires, from the Secret the emulator fixture wrote."""
    return read_credential(core_client, emulator_namespace)


@pytest.fixture(scope="session")
def emulator_url(kube_context: str, emulator_namespace: str, emulator: client.V1Deployment) -> Iterator[str]:
    """HTTP base URL of the emulator, reached through a port-forward to its Service."""
    with port_forward(
        kube_context,
        emulator_namespace,
        f"service/{emulator_name()}",
        FORWARDED_PORT,
        emulator_port(),
    ) as url:
        yield url


@pytest.fixture(scope="session")
def evalhub_api_base(
    kube_context: str,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
) -> Iterator[str]:
    """HTTPS base URL of the instance's API, through a port-forward to its Service."""
    with port_forward(
        context=kube_context,
        namespace=evalhub_instance_namespace,
        target=f"service/{EVALHUB_NAME}",
        local_port=FORWARDED_PORT + 1,
        remote_port=EVALHUB_SERVICE_PORT,
        scheme="https",
    ) as base_url:
        yield base_url


@pytest.fixture(scope="session")
def evalhub_tenant_identity(
    core_client: client.CoreV1Api,
    api_client: client.ApiClient,
    evalhub_instance: dict[str, Any],
    evalhub_tenant_namespace: str,
) -> str:
    """A ServiceAccount in the tenant namespace, granted the EvalHub API."""
    return create_tenant_identity(
        core_client,
        client.RbacAuthorizationV1Api(api_client),
        evalhub_tenant_namespace,
    )


@pytest.fixture(scope="session")
def evalhub_tenant_token(kube_context: str, evalhub_tenant_identity: str, evalhub_tenant_namespace: str) -> str:
    """A bearer token for the tenant identity, which the proxy authorizes by SAR."""
    return mint_token(kube_context, evalhub_tenant_namespace, TENANT_IDENTITY_NAME)


@pytest.fixture(scope="session")
def evalhub_tenant_ca(
    core_client: client.CoreV1Api,
    evalhub_instance: dict[str, Any],
    evalhub_instance_namespace: str,
    evalhub_tenant_namespace: str,
) -> None:
    """Wait for the tenant to be provisioned, then give it the instance certificate.

    The evaluation sidecar reads `service-ca.crt` from the tenant's ConfigMap to
    verify the instance it reports results back to; without this every job runs to
    completion and then loses its results on a TLS failure.
    """
    rbac_client = client.RbacAuthorizationV1Api(core_client.api_client)
    wait_for_tenant_rbac(rbac_client, evalhub_tenant_namespace, evalhub_instance_namespace)
    # Belt and braces: the wait above can only observe the operator's binding, and
    # another instance in the cluster may rewrite it a moment later.
    grant_tenant_job_access(rbac_client, evalhub_tenant_namespace, evalhub_instance_namespace)
    cert_pem = instance_tls_cert(core_client, evalhub_instance_namespace)
    seed_tenant_ca(core_client, evalhub_tenant_namespace, cert_pem)


@pytest.fixture(scope="session")
def evalhub_model_credentials(
    core_client: client.CoreV1Api,
    evalhub_tenant_namespace: str,
    evalhub_tenant_ca: None,
    emulator_credentials: str,
) -> str:
    """The model credential Secret a job's `model.auth.secret_ref` points at.

    `api-key` is the key EvalHub resolves, and its value is the token the emulator
    requires, so a job can only reach the model if the credential is forwarded.
    """
    name = "emulator-credentials"
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=evalhub_tenant_namespace),
        data={"api-key": base64.b64encode(emulator_credentials.encode()).decode()},
    )
    try:
        core_client.create_namespaced_secret(namespace=evalhub_tenant_namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_secret(name=name, namespace=evalhub_tenant_namespace, body=body)
    return name
