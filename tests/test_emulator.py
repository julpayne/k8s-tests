"""Checks for the LLM emulator and the token it requires.

The harness deploys a digest-pinned vLLM emulator behind a `/v1` bearer-token
check, with the token in the `<name>-auth` Secret under the key EvalHub resolves
`model.auth.secret_ref` to. These checks pin that contract: a request carrying
the secret's token succeeds, and a request without it, or with another token, is
rejected, which is what lets an adapter check tell a forwarded credential from a
dropped one. With `MODEL_URL` set the checks call a real endpoint instead and
this module skips, because there is no emulator to pin.
"""

from __future__ import annotations

import hashlib

import pytest
from kubernetes import client

from utilities.emulator import (
    COMPLETIONS_PATH,
    CREDENTIAL_KEY,
    auth_state,
    chat_completion,
    completion,
    credential_secret_name,
    emulator_image,
    emulator_name,
    emulator_token,
    health,
    model_url,
)
from utilities.kube import assert_deployment_healthy, container_images

pytestmark = pytest.mark.emulator


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def test_emulator_deployment_is_healthy(
    apps_client: client.AppsV1Api,
    core_client: client.CoreV1Api,
    emulator: client.V1Deployment,
    emulator_namespace: str,
) -> None:
    assert_deployment_healthy(apps_client, core_client, emulator_name(), emulator_namespace)


def test_emulator_runs_the_image_the_harness_chose(emulator: client.V1Deployment) -> None:
    images = container_images(emulator)
    assert images == [emulator_image()], (
        f"the emulator Deployment runs {images}; the harness deploys {emulator_image()} (override with EMULATOR_IMG)"
    )


def test_health_endpoint_needs_no_token(emulator_url: str) -> None:
    response = health(emulator_url)
    assert response.status_code == 200, f"the health endpoint answered {response.status_code}: {response.text}"
    assert response.json() == {"status": "ok"}


def test_completions_are_rejected_without_a_token(emulator_url: str) -> None:
    response = completion(emulator_url, token=None, max_tokens=4)
    assert response.status_code == 401, (
        f"the emulator answered {response.status_code} to an unauthenticated {COMPLETIONS_PATH}; "
        "it is supposed to require a bearer token"
    )
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_completions_are_rejected_with_another_token(emulator_url: str) -> None:
    response = completion(emulator_url, token="not-the-harness-token", max_tokens=4)
    assert response.status_code == 401, f"the emulator accepted a token that is not the configured one: {response.text}"
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_completions_succeed_with_the_token_from_the_secret(emulator_url: str, emulator_credentials: str) -> None:
    response = completion(emulator_url, token=emulator_credentials)
    assert response.status_code == 200, f"the emulator rejected the configured token: {response.text}"
    body = response.json()
    assert body["choices"], f"the emulator returned no choices: {body}"
    assert body["choices"][0]["text"].strip(), f"the emulator returned an empty completion: {body}"


def test_chat_completions_require_the_same_token(emulator_url: str, emulator_credentials: str) -> None:
    rejected = chat_completion(emulator_url, token=None, max_tokens=4)
    assert rejected.status_code == 401, f"chat completions answered {rejected.status_code} without a token"
    accepted = chat_completion(emulator_url, token=emulator_credentials)
    assert accepted.status_code == 200, f"chat completions rejected the configured token: {accepted.text}"
    assert accepted.json()["choices"][0]["message"]["content"].strip()


def test_the_token_check_reports_what_it_saw(emulator_url: str, emulator_credentials: str) -> None:
    """The emulator can name the token that arrived, so a check can attribute a request."""
    before = auth_state(emulator_url)
    completion(emulator_url, token=emulator_credentials, max_tokens=4)
    completion(emulator_url, token="another-token-entirely", max_tokens=4)
    after = auth_state(emulator_url)

    assert after["authenticated_requests"] == before["authenticated_requests"] + 1
    assert after["rejected_requests"] == before["rejected_requests"] + 1
    assert after["last_authenticated_path"] == COMPLETIONS_PATH
    assert after["last_rejected_reason"] == "invalid"
    assert fingerprint(emulator_credentials) in after["presented_fingerprints"], (
        "the emulator did not record the configured token, so a check could not tell it apart from another"
    )


def test_the_credential_secret_has_the_key_evalhub_resolves(
    core_client: client.CoreV1Api,
    emulator_namespace: str,
    emulator_credentials: str,
) -> None:
    """`model.auth.secret_ref` resolves `api-key` from a Secret in the tenant namespace."""
    secret = core_client.read_namespaced_secret(name=credential_secret_name(), namespace=emulator_namespace)
    assert CREDENTIAL_KEY in (secret.data or {}), (
        f"secret {emulator_namespace}/{credential_secret_name()} has keys {sorted(secret.data or {})}; "
        f"EvalHub resolves model credentials from {CREDENTIAL_KEY!r}"
    )
    assert emulator_credentials, "the emulator token is empty"
    assert emulator_credentials == emulator_token(), "the Secret does not carry the configured token"


def test_the_model_url_is_the_one_a_job_would_use(emulator: client.V1Deployment, emulator_namespace: str) -> None:
    """The in-cluster URL a job submits, so a check never has to guess it."""
    url = model_url(emulator_namespace)
    assert url == f"http://{emulator_name()}.{emulator_namespace}.svc.cluster.local:8000/v1"
