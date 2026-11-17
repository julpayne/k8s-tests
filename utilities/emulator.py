"""The LLM emulator the adapter checks call as their model.

The checks own the emulator: a fixture applies it, a fixture removes it, and the
namespace, image, port and token are all parameters. Nothing is baked into the
cluster ahead of the run.

The emulator image has no notion of credentials, so the deployment mounts a
wrapper over it from a ConfigMap (`hack/emulator/auth_emulator.py`) and starts
that instead: every `/v1` request must carry `Authorization: Bearer <token>`,
where the token is the `api-key` entry of the `<name>-auth` Secret. That is the
key an EvalHub job's `model.auth.secret_ref` resolves, so a check can prove the
credential reached the model rather than assuming it did.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import pytest
import requests
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from utilities.kube import namespace_from_env, wait_for_deployment_available

LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "vllm-emulator"
DEFAULT_PORT = 8000
DEFAULT_IMAGE = (
    "quay.io/trustyai_testing/vllm_emulator@sha256:32b5f26b5ec1c5c8052afa26c6d9769dbc864df27a058716c8df62d827cf1d07"
)
# A development value, not a secret: it only has to be the same on both sides.
DEFAULT_TOKEN = "trustyai-emulator-token"

TOKEN_VAR = "EMULATOR_TOKEN"
IMAGE_VAR = "EMULATOR_IMG"
CREDENTIAL_KEY = "api-key"
HEALTH_PATH = "/health"
AUTH_STATE_PATH = "/__auth/state"
COMPLETIONS_PATH = "/v1/completions"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
FORWARDED_PORT = 18000
REQUEST_TIMEOUT = 20
DEPLOY_TIMEOUT = 300

WRAPPER_PATH = Path(__file__).resolve().parent.parent / "hack" / "emulator" / "auth_emulator.py"


def emulator_name() -> str:
    return os.environ.get("EMULATOR_NAME") or DEFAULT_NAME


def emulator_port() -> int:
    return int(os.environ.get("EMULATOR_PORT") or DEFAULT_PORT)


def emulator_namespace() -> str:
    """Namespace the emulator runs in; the operators' namespace by default."""
    return os.environ.get("EMULATOR_NAMESPACE") or namespace_from_env()


def emulator_image() -> str:
    return os.environ.get(IMAGE_VAR) or DEFAULT_IMAGE


def emulator_token() -> str:
    return os.environ.get(TOKEN_VAR) or DEFAULT_TOKEN


def credential_secret_name() -> str:
    return f"{emulator_name()}-auth"


def wrapper_config_map_name() -> str:
    return f"{emulator_name()}-auth-script"


def model_url(namespace: str) -> str:
    """In-cluster base URL an EvalHub job uses as its model endpoint."""
    return f"http://{emulator_name()}.{namespace}.svc.cluster.local:{emulator_port()}/v1"


def _labels() -> dict[str, str]:
    return {"app": emulator_name()}


def _wrapper_source() -> str:
    if not WRAPPER_PATH.exists():
        pytest.fail(f"the emulator auth wrapper is missing: {WRAPPER_PATH}", pytrace=False)
    return WRAPPER_PATH.read_text()


def _create_or_replace_config_map(core_client: client.CoreV1Api, namespace: str, body: client.V1ConfigMap) -> None:
    try:
        core_client.create_namespaced_config_map(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_config_map(name=body.metadata.name, namespace=namespace, body=body)


def _create_or_replace_secret(core_client: client.CoreV1Api, namespace: str, body: client.V1Secret) -> None:
    try:
        core_client.create_namespaced_secret(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_secret(name=body.metadata.name, namespace=namespace, body=body)


def _emulator_deployment(namespace: str, image: str) -> client.V1Deployment:
    name = emulator_name()
    port = emulator_port()
    labels = _labels()
    wrapper_mount = client.V1VolumeMount(name="auth-script", mount_path="/opt/emulator-auth", read_only=True)
    container = client.V1Container(
        name=name,
        image=image,
        # The emulator's own entry point, with the token wrapper in front of it.
        command=["uvicorn", "auth_emulator:app", "--host", "0.0.0.0", "--port", str(port)],
        env=[
            client.V1EnvVar(
                name="EMULATOR_API_KEY",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name=credential_secret_name(), key=CREDENTIAL_KEY)
                ),
            ),
            client.V1EnvVar(name="PYTHONPATH", value="/opt/emulator-auth:/app"),
        ],
        ports=[client.V1ContainerPort(name="http", container_port=port, protocol="TCP")],
        readiness_probe=client.V1Probe(
            tcp_socket=client.V1TCPSocketAction(port=port),
            initial_delay_seconds=2,
            period_seconds=5,
            timeout_seconds=3,
            failure_threshold=6,
        ),
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        volume_mounts=[wrapper_mount],
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    # The emulator has no state to flush, so a replacement pod does
                    # not need the default grace period before it stops serving.
                    termination_grace_period_seconds=5,
                    containers=[container],
                    volumes=[
                        client.V1Volume(
                            name="auth-script",
                            config_map=client.V1ConfigMapVolumeSource(name=wrapper_config_map_name()),
                        )
                    ],
                ),
            ),
        ),
    )


def _emulator_service(namespace: str) -> client.V1Service:
    port = emulator_port()
    return client.V1Service(
        metadata=client.V1ObjectMeta(name=emulator_name(), namespace=namespace, labels=_labels()),
        spec=client.V1ServiceSpec(
            selector=_labels(),
            ports=[client.V1ServicePort(name="http", port=port, target_port=port, protocol="TCP")],
        ),
    )


def deploy_emulator(
    core_client: client.CoreV1Api,
    apps_client: client.AppsV1Api,
    namespace: str,
    image: str | None = None,
    token: str | None = None,
) -> client.V1Deployment:
    """Create the emulator's Secret, wrapper ConfigMap, Deployment and Service."""
    name = emulator_name()
    image = image or emulator_image()
    token = token or emulator_token()

    LOGGER.info("deploying the LLM emulator %s in namespace %s", image, namespace)
    _create_or_replace_secret(
        core_client,
        namespace,
        client.V1Secret(
            metadata=client.V1ObjectMeta(name=credential_secret_name(), namespace=namespace),
            data={CREDENTIAL_KEY: base64.b64encode(token.encode()).decode()},
        ),
    )
    _create_or_replace_config_map(
        core_client,
        namespace,
        client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=wrapper_config_map_name(), namespace=namespace),
            data={"auth_emulator.py": _wrapper_source()},
        ),
    )

    deployment = _emulator_deployment(namespace, image)
    try:
        apps_client.create_namespaced_deployment(namespace=namespace, body=deployment)
    except ApiException as exc:
        if exc.status != 409:
            raise
        apps_client.replace_namespaced_deployment(name=name, namespace=namespace, body=deployment)

    service = _emulator_service(namespace)
    try:
        core_client.create_namespaced_service(namespace=namespace, body=service)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_service(name=name, namespace=namespace, body=service)

    return wait_for_deployment_available(apps_client, name, namespace, timeout=DEPLOY_TIMEOUT, core_client=core_client)


def remove_emulator(core_client: client.CoreV1Api, apps_client: client.AppsV1Api, namespace: str) -> None:
    """Remove what `deploy_emulator` created, tolerating anything already gone."""
    name = emulator_name()
    LOGGER.info("removing the LLM emulator from namespace %s", namespace)
    for deleter, kwargs in (
        (apps_client.delete_namespaced_deployment, {"name": name, "namespace": namespace}),
        (core_client.delete_namespaced_service, {"name": name, "namespace": namespace}),
        (core_client.delete_namespaced_config_map, {"name": wrapper_config_map_name(), "namespace": namespace}),
        (core_client.delete_namespaced_secret, {"name": credential_secret_name(), "namespace": namespace}),
    ):
        try:
            deleter(**kwargs)
        except ApiException as exc:
            if exc.status != 404:
                raise


def read_credential(core_client: client.CoreV1Api, namespace: str) -> str:
    """The token the emulator requires, read from the Secret the fixture wrote."""
    secret_name = credential_secret_name()
    try:
        secret = core_client.read_namespaced_secret(name=secret_name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        pytest.fail(
            f"secret {namespace}/{secret_name} was not found; the emulator fixture did not run",
            pytrace=False,
        )
    data = secret.data or {}
    if CREDENTIAL_KEY not in data:
        pytest.fail(
            f"secret {namespace}/{secret_name} has no {CREDENTIAL_KEY!r} key; "
            "EvalHub resolves model credentials by that name",
            pytrace=False,
        )
    LOGGER.info("read the emulator token from secret %s/%s key %s", namespace, secret_name, CREDENTIAL_KEY)
    return base64.b64decode(data[CREDENTIAL_KEY]).decode()


def bearer(token: str | None) -> dict[str, str]:
    """Headers for a model request; `None` sends no Authorization at all."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def completion(
    base_url: str,
    token: str | None,
    prompt: str = "The opposite of down is",
    max_tokens: int = 8,
) -> requests.Response:
    """POST a completions request, the way an adapter would."""
    LOGGER.info("POST %s%s with%s a bearer token", base_url, COMPLETIONS_PATH, "" if token else "out")
    return requests.post(
        f"{base_url}{COMPLETIONS_PATH}",
        json={"model": "emulated-model", "prompt": prompt, "max_tokens": max_tokens, "logprobs": True},
        headers=bearer(token),
        timeout=REQUEST_TIMEOUT,
    )


def chat_completion(base_url: str, token: str | None, max_tokens: int = 8) -> requests.Response:
    """POST a chat completions request, the way a chat-style adapter would."""
    LOGGER.info("POST %s%s with%s a bearer token", base_url, CHAT_COMPLETIONS_PATH, "" if token else "out")
    body = {
        "model": "emulated-model",
        "messages": [{"role": "user", "content": "What is the opposite of down?"}],
        "max_tokens": max_tokens,
        "logprobs": True,
    }
    return requests.post(
        f"{base_url}{CHAT_COMPLETIONS_PATH}",
        json=body,
        headers=bearer(token),
        timeout=REQUEST_TIMEOUT,
    )


def health(base_url: str) -> requests.Response:
    return requests.get(f"{base_url}{HEALTH_PATH}", timeout=REQUEST_TIMEOUT)


def auth_state(base_url: str) -> dict:
    """What the emulator's token check has seen; it never returns a token."""
    response = requests.get(f"{base_url}{AUTH_STATE_PATH}", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()
