"""Checks for an evaluation job that creates a benchmark against the emulator.

The job is the smallest real one the harness can run: one benchmark, one dataset
example, against the LLM emulator the fixtures deploy. It exercises the whole
path an adapter takes - provider, model URL, model credential - and the emulator
is strict about the credential, so the checks can prove the token in the tenant
Secret reached the model rather than the run merely finishing.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest

from utilities.emulator import auth_state, model_url
from utilities.evalhub import api_headers, benchmark_result, submit_job, wait_for_job

LOGGER = logging.getLogger(__name__)

pytestmark = [pytest.mark.evalhub, pytest.mark.benchmark]

BENCHMARK_ID = "arc_easy"
PROVIDER_ID = "lm_evaluation_harness"
# One example: enough to score, small enough to run in a Kind cluster.
NUM_EXAMPLES = 1
JOB_TIMEOUT = 900
MODEL_NAME = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"


def job_payload(emulator_namespace: str, credentials_secret: str) -> dict[str, Any]:
    """The job body a user would submit for one benchmark against the emulator."""
    return {
        "name": f"emulator-{BENCHMARK_ID}",
        "model": {
            "url": model_url(emulator_namespace),
            "name": MODEL_NAME,
            "auth": {"secret_ref": credentials_secret},
        },
        "benchmarks": [
            {
                "id": BENCHMARK_ID,
                "provider_id": PROVIDER_ID,
                "parameters": {"num_examples": NUM_EXAMPLES},
            }
        ],
    }


@pytest.fixture(scope="module")
def submitted(
    evalhub_api_base: str,
    evalhub_tenant_token: str,
    evalhub_tenant_namespace: str,
    evalhub_model_credentials: str,
    emulator_namespace: str,
    emulator_url: str,
) -> dict[str, Any]:
    """Submit the job, recording what the emulator had seen before it."""
    payload = job_payload(emulator_namespace, evalhub_model_credentials)
    headers = api_headers(evalhub_tenant_token, evalhub_tenant_namespace)
    before = auth_state(emulator_url)
    job = submit_job(evalhub_api_base, headers, payload)
    return {"job": job, "headers": headers, "payload": payload, "auth_before": before}


@pytest.fixture(scope="module")
def finished(submitted: dict[str, Any], evalhub_api_base: str) -> dict[str, Any]:
    """Wait for the job to reach a terminal state."""
    return wait_for_job(
        evalhub_api_base,
        submitted["headers"],
        submitted["job"]["resource"]["id"],
        timeout=JOB_TIMEOUT,
    )


def test_the_job_is_accepted_with_the_requested_benchmark(submitted: dict[str, Any]) -> None:
    job = submitted["job"]
    assert job["resource"]["id"], f"the server returned no job id: {job}"
    assert job["status"]["state"] in ("pending", "running"), f"unexpected initial state: {job['status']['state']}"
    benchmark = job["benchmarks"][0]
    assert (benchmark["id"], benchmark["provider_id"]) == (BENCHMARK_ID, PROVIDER_ID)
    assert benchmark["parameters"]["num_examples"] == NUM_EXAMPLES


def test_the_job_completes(finished: dict[str, Any]) -> None:
    status = finished["status"]
    assert status["state"] == "completed", (
        f"job {finished['resource']['id']} ended {status['state']}: {(status.get('message') or {}).get('message')}"
    )
    benchmark_status = status["benchmarks"][0]
    assert benchmark_status["status"] == "completed", (
        f"{BENCHMARK_ID} ended {benchmark_status['status']}: {benchmark_status.get('error_message')}"
    )


def test_the_result_has_the_expected_shape(finished: dict[str, Any]) -> None:
    """`results.benchmarks[]` carries the benchmark, its provider and its metrics."""
    result = benchmark_result(finished, BENCHMARK_ID)
    LOGGER.info("benchmark result: %s", result)
    assert result["provider_id"] == PROVIDER_ID
    assert result["benchmark_index"] == 0
    assert isinstance(result.get("metrics"), dict) and result["metrics"], (
        f"{BENCHMARK_ID} completed without metrics: {result}"
    )
    for name, value in result["metrics"].items():
        assert isinstance(value, (int, float)), f"metric {name!r} is {value!r}, not a number"


def test_the_benchmark_reports_a_test_outcome(finished: dict[str, Any]) -> None:
    """The pass criteria in the provider config turn the score into a test result."""
    result = benchmark_result(finished, BENCHMARK_ID)
    test = result.get("test")
    if test is None:
        pytest.skip(f"{BENCHMARK_ID} reported no pass criteria in this deployment: {sorted(result)}")
    assert isinstance(test["primary_score"], (int, float))
    assert isinstance(test["pass"], bool)
    metric = test.get("primary_score_metric")
    if metric:
        assert metric in result["metrics"], (
            f"the test scored {metric!r}, which is not among the reported metrics {sorted(result['metrics'])}"
        )


def test_the_token_reached_the_model(
    finished: dict[str, Any],
    submitted: dict[str, Any],
    emulator_url: str,
    emulator_credentials: str,
) -> None:
    """The emulator saw the tenant Secret's token on the job's model requests.

    This is the point of the credential plumbing: the job's `model.auth.secret_ref`
    resolves to `api-key`, the sidecar injects it, and the emulator rejects
    anything else - so an authenticated request can only come from that token.
    """
    state = auth_state(emulator_url)
    before = submitted["auth_before"]
    assert state["authenticated_requests"] > before["authenticated_requests"], (
        f"the job completed but the emulator authenticated no model request; before={before}, after={state}"
    )
    assert state["last_authenticated_path"].startswith("/v1/"), (
        f"the last authenticated request was {state['last_authenticated_path']!r}, not a model endpoint"
    )
    fingerprint = hashlib.sha256(emulator_credentials.encode()).hexdigest()[:16]
    assert fingerprint in state["presented_fingerprints"], (
        "the emulator did not see the tenant Secret's token; it saw "
        f"{state['presented_fingerprints']} instead of {fingerprint}"
    )
