"""An LMEvalJob: the execution Pod the operator creates, and the policy that guards it.

The workload operator turns an `LMEvalJob` into two things: a Pod that runs
lm-evaluation-harness for one benchmark, and a NetworkPolicy whose Pod selector is
the Pod's identity labels. The policy is not an implementation detail the checks can
look past - it is what ties a running evaluation to the resource that asked for it -
so the operator re-derives the policy and the Pod's labels on every reconcile:
deleting them, or editing them by hand, must not detach a workload from its owner.

The Pod is deliberately never meant to run here. The job below asks for more CPU
than any Kind node has, so the Pod waits in the scheduler and the evaluation image
is never pulled: the identity and policy convergence the checks are about happens
before a container starts, and running a real benchmark would need a model, a
dataset and minutes of wall clock to prove the same contract.

What the operator reports is what the checks read: `NetworkPolicyReady` on the
resource, and a `podName` in its status.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

LOGGER = logging.getLogger(__name__)

API_GROUP = "trustyai.opendatahub.io"
API_VERSION = "v1alpha1"
API_REFERENCE = f"{API_GROUP}/{API_VERSION}"
PLURAL = "lmevaljobs"
KIND = "LMEvalJob"
CRD_NAME = f"{PLURAL}.{API_GROUP}"

FINALIZER = "trustyai.opendatahub.io/lmes-finalizer"
NETWORK_POLICY_READY_CONDITION = "NetworkPolicyReady"
SCHEDULED_STATE = "Scheduled"
COMPLETE_STATE = "Complete"
SUCCEED_REASON = "Succeeded"

# The identity the operator derives from the resource: which component owns the
# workload, which role it plays, and the UID of the resource that asked for it. The
# fourth label is what the first, ingress-only, policy selected on; it is still
# stamped on the Pod and the policy, but nothing selects on it any more.
COMPONENT_LABEL = "trustyai.opendatahub.io/component"
ROLE_LABEL = "trustyai.opendatahub.io/role"
OWNER_UID_LABEL = "trustyai.opendatahub.io/owner-uid"
JOB_UID_LABEL = "trustyai.opendatahub.io/lmevaljob-uid"
COMPONENT = "lmes"
ROLE = "evaluation"
POD_IDENTITY_LABELS = (JOB_UID_LABEL, OWNER_UID_LABEL, COMPONENT_LABEL, ROLE_LABEL)

# No Kind node has this much CPU, so the execution Pod stays Pending and nothing is
# pulled or run: what the checks assert is the policy and the identity, which the
# operator reconciles before the Pod's first container.
UNSCHEDULABLE_CPU = "64"
# The operator creates the policy before the Pod and reports on both through the
# resource's status, which is not written in the same instant as the resource.
JOB_TIMEOUT = 300
POLICY_TIMEOUT = 300
# A live job is re-checked on the operator's own polling interval and a completed one
# is repaired when its Pod changes, so the wait has to clear that interval with room
# for a loaded runner.
REPAIR_TIMEOUT = 120
CLEANUP_TIMEOUT = 180
POLL_INTERVAL = 2


def job_manifest(namespace: str, name: str, cpu_request: str = UNSCHEDULABLE_CPU) -> dict[str, Any]:
    """The LMEvalJob a user would create for one benchmark, held at Pod creation."""
    return {
        "apiVersion": API_REFERENCE,
        "kind": KIND,
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "model": "hf",
            "modelArgs": [{"name": "pretrained", "value": "google/flan-t5-small"}],
            "taskList": {"taskNames": ["arc_easy"]},
            "pod": {"container": {"resources": {"requests": {"cpu": cpu_request}}}},
        },
    }


def create_job(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> dict[str, Any]:
    """Create the job, returning what the API server stored."""
    job = custom_api.create_namespaced_custom_object(
        group=API_GROUP,
        version=API_VERSION,
        namespace=namespace,
        plural=PLURAL,
        body=job_manifest(namespace, name),
    )
    LOGGER.info("created %s/%s (%s)", namespace, name, job["metadata"]["uid"])
    return job


def read_job(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> dict[str, Any]:
    """The job as the operator last reported it."""
    return custom_api.get_namespaced_custom_object(
        group=API_GROUP, version=API_VERSION, namespace=namespace, plural=PLURAL, name=name
    )


def delete_job(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> None:
    """Delete the job; its finalizer removes the Pod and the policy it owns."""
    try:
        custom_api.delete_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION, namespace=namespace, plural=PLURAL, name=name
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        return
    LOGGER.info("deleted %s/%s; waiting for its finalizer", namespace, name)
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    while True:
        try:
            read_job(custom_api, namespace, name)
        except ApiException as exc:
            if exc.status == 404:
                LOGGER.info("%s/%s is gone, with the Pod and policy it owned", namespace, name)
                return
            raise
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{namespace}/{name} still exists {CLEANUP_TIMEOUT}s after deletion: "
                f"its {FINALIZER} finalizer did not finish",
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL)


def condition_is_true(job: dict[str, Any], condition: str) -> bool:
    """Whether the resource reports `condition` as true."""
    for entry in job.get("status", {}).get("conditions") or []:
        if entry.get("type") == condition:
            return entry.get("status") == "True"
    return False


def wait_for_scheduled_job(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    timeout: int = JOB_TIMEOUT,
) -> dict[str, Any]:
    """Wait until the operator has created the execution Pod and guarded it.

    The two are not the same wait: the policy is reconciled before the Pod is
    created, and `NetworkPolicyReady` is reported from the same step, so a job that
    already carries the condition only has its Pod left to appear.
    """
    deadline = time.monotonic() + timeout
    while True:
        job = read_job(custom_api, namespace, name)
        status = job.get("status") or {}
        if (
            status.get("state") == SCHEDULED_STATE
            and status.get("podName")
            and condition_is_true(job, NETWORK_POLICY_READY_CONDITION)
        ):
            LOGGER.info(
                "%s/%s is %s with pod %s and %s=True",
                namespace,
                name,
                SCHEDULED_STATE,
                status["podName"],
                NETWORK_POLICY_READY_CONDITION,
            )
            return job
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{namespace}/{name} did not reach {SCHEDULED_STATE} with "
                f"{NETWORK_POLICY_READY_CONDITION}=True within {timeout}s; status is {status}",
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL)


def read_pod(core_client: client.CoreV1Api, namespace: str, name: str) -> client.V1Pod:
    """The execution Pod the operator reports on the resource."""
    return core_client.read_namespaced_pod(name=name, namespace=namespace)


def policies_owned_by(
    networking_client: client.NetworkingV1Api, namespace: str, uid: str
) -> list[client.V1NetworkPolicy]:
    """The NetworkPolicies in `namespace` whose controller is the resource with `uid`."""
    policies = networking_client.list_namespaced_network_policy(namespace=namespace).items
    owned = []
    for policy in policies:
        for reference in policy.metadata.owner_references or []:
            if reference.controller and reference.uid == uid and reference.kind == KIND:
                owned.append(policy)
                break
    return owned


def wait_for_policy(
    networking_client: client.NetworkingV1Api,
    namespace: str,
    uid: str,
    timeout: int = POLICY_TIMEOUT,
    not_uid: str | None = None,
) -> client.V1NetworkPolicy:
    """Wait for the policy the operator creates for this resource.

    `not_uid` waits for a *replacement*: after a policy is deleted the operator
    creates a new object rather than reviving the old one, so the checks compare
    UIDs to tell one from the other.
    """
    deadline = time.monotonic() + timeout
    while True:
        owned = policies_owned_by(networking_client, namespace, uid)
        for policy in owned:
            if not_uid is None or policy.metadata.uid != not_uid:
                LOGGER.info("found policy %s/%s (%s)", namespace, policy.metadata.name, policy.metadata.uid)
                return policy
        if time.monotonic() >= deadline:
            pytest.fail(
                f"no NetworkPolicy owned by {uid} in {namespace} within {timeout}s"
                + (f" other than {not_uid}" if not_uid else ""),
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL)


def wait_for_policy_rules(
    networking_client: client.NetworkingV1Api,
    namespace: str,
    name: str,
    timeout: int = REPAIR_TIMEOUT,
) -> client.V1NetworkPolicy:
    """Wait until the policy admits no ingress again and keeps the operator's egress rule."""
    deadline = time.monotonic() + timeout
    while True:
        policy = networking_client.read_namespaced_network_policy(name=name, namespace=namespace)
        if not policy.spec.ingress and len(policy.spec.egress or []) == 1:
            LOGGER.info("%s/%s is back to no ingress and one egress rule", namespace, name)
            return policy
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{namespace}/{name} still has ingress={policy.spec.ingress} and egress={policy.spec.egress} "
                f"after {timeout}s",
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL)


def mark_complete(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    name: str,
    results: str = "retained-result",
) -> dict[str, Any]:
    """Record a finished evaluation on the resource, the way its driver would.

    Kind has a kubelet, but what it runs here is a Pod held out of the scheduler: a
    completed evaluation would need a model and a dataset to reach this state. The
    status is the operator's own API to report outcomes, so writing it is how a
    finished run is reproduced without running one - and it is the state in which a
    retained job must still repair its Pod's identity, because nothing else re-checks
    it on an interval once it is complete.
    """
    completed = custom_api.patch_namespaced_custom_object_status(
        group=API_GROUP,
        version=API_VERSION,
        namespace=namespace,
        plural=PLURAL,
        name=name,
        body={
            "status": {
                "state": COMPLETE_STATE,
                "reason": SUCCEED_REASON,
                "results": results,
                "completeTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        },
    )
    LOGGER.info("%s/%s reports %s with results %r", namespace, name, COMPLETE_STATE, results)
    return completed


def remove_label(core_client: client.CoreV1Api, namespace: str, pod_name: str, label: str) -> None:
    """Take one identity label off the Pod, as editing it by hand would."""
    core_client.patch_namespaced_pod(name=pod_name, namespace=namespace, body={"metadata": {"labels": {label: None}}})
    LOGGER.info("removed %s from %s/%s", label, namespace, pod_name)


def wait_for_label(
    core_client: client.CoreV1Api,
    namespace: str,
    pod_name: str,
    label: str,
    expected: str,
    timeout: int = REPAIR_TIMEOUT,
) -> None:
    """Wait for the operator to put an identity label back on the Pod."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            pod = read_pod(core_client, namespace, pod_name)
        except ApiException as exc:
            if exc.status != 404:
                raise
            pytest.fail(f"the Pod {namespace}/{pod_name} was deleted, so no identity could be repaired", pytrace=False)
        observed = (pod.metadata.labels or {}).get(label)
        if observed == expected:
            LOGGER.info("restored %s=%s on %s/%s", label, expected, namespace, pod_name)
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{namespace}/{pod_name} still has {label}={observed!r} after {timeout}s, expected {expected!r}",
                pytrace=False,
            )
        time.sleep(POLL_INTERVAL)
