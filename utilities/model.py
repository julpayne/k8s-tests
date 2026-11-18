"""The model endpoint the job checks call, and the credential it needs.

By default the checks call the LLM emulator the fixtures deploy, and the
emulator's token is the credential they prove reached the model. Setting
`MODEL_URL` points the jobs at a real, OpenAI-compatible endpoint instead; the
fixtures then deploy no emulator, so the checks work against a cluster that
already serves a model.

`MODEL_KEY`, when set, is the credential that endpoint requires. The suite's
setup creates a Secret in the tenant namespace with the known name
`model-credentials` and the `api-key` entry EvalHub resolves from
`model.auth.secret_ref`, so the job can name it without guessing what the
credential is.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.exceptions import ApiException

LOGGER = logging.getLogger(__name__)

MODEL_URL_VAR = "MODEL_URL"
MODEL_KEY_VAR = "MODEL_KEY"
MODEL_NAME_VAR = "MODEL_NAME"
# The name the setup gives the Secret it creates when MODEL_KEY is set, and the
# key EvalHub resolves model credentials from. A job's `model.auth.secret_ref`
# names the Secret; the value is never logged.
MODEL_CREDENTIALS_SECRET = "model-credentials"
MODEL_CREDENTIALS_KEY = "api-key"
DEFAULT_MODEL_NAME = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"


@dataclass(frozen=True)
class ModelEndpoint:
    """The model a job calls, its name, and the credential it may need.

    `auth_secret_name` is the Secret a job's `model.auth.secret_ref` names, or
    `None` when the endpoint needs no credential. `credential` is the value that
    Secret carries; it is never logged. `observes_requests` is True only for the
    emulator the fixtures deploy, whose `/__auth/state` endpoint can prove a job
    called the model rather than merely reporting success; `state_url` is where
    that endpoint is reachable from the host.
    """

    url: str
    name: str = DEFAULT_MODEL_NAME
    auth_secret_name: str | None = None
    credential: str | None = None
    observes_requests: bool = False
    state_url: str | None = None


def external_model_url() -> str | None:
    """The real model endpoint under test, or `None` when the emulator is used."""
    return (os.environ.get(MODEL_URL_VAR) or "").strip() or None


def external_model_key() -> str | None:
    """The credential for the real model endpoint, or `None` when it needs none."""
    return (os.environ.get(MODEL_KEY_VAR) or "").strip() or None


def model_name() -> str:
    """The model name a job submits; MODEL_NAME overrides the default."""
    return (os.environ.get(MODEL_NAME_VAR) or "").strip() or DEFAULT_MODEL_NAME


def ensure_model_credentials(
    core_client: client.CoreV1Api,
    namespace: str,
    name: str,
    key: str,
) -> str:
    """Create or replace the Secret a job's `model.auth.secret_ref` names.

    The Secret carries the `api-key` entry EvalHub resolves, so the value never
    has to appear in the job payload. Only the Secret name is logged.
    """
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data={MODEL_CREDENTIALS_KEY: base64.b64encode(key.encode()).decode()},
    )
    try:
        core_client.create_namespaced_secret(namespace=namespace, body=body)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_client.replace_namespaced_secret(name=name, namespace=namespace, body=body)
    LOGGER.info("wrote the model credential to secret %s/%s key %s", namespace, name, MODEL_CREDENTIALS_KEY)
    return name
