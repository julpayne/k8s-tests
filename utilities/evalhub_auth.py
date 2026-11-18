"""Helpers for the authorization policy the EvalHub operator configures.

kube-rbac-proxy fronts the EvalHub API and authorizes every request with a
namespace-scoped SubjectAccessReview, in the namespace the `X-Tenant` header
names, against a pseudo-resource such as `evaluations` or `collections`. Which
pseudo-resource and verb a path maps to is not in the pod spec: it is the
`auth.yaml` key of the instance's configuration ConfigMap, which the proxy
container mounts and whose digest the operator records on the pod template. The
operator also writes the Role a tenant namespace's own ServiceAccounts act
through.

The checks read both, so they assert the policy the cluster actually runs rather
than a copy of it in the test suite.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import pytest
import yaml
from kubernetes import client

LOGGER = logging.getLogger(__name__)

# The ConfigMap key the proxy reads its policy from, and the volume that carries it.
AUTH_CONFIG_KEY = "auth.yaml"
AUTH_VOLUME_NAME = "evalhub-config"
# Pod-template annotation carrying the SHA-256 of the auth.yaml the pod mounts.
AUTH_CONFIG_HASH_ANNOTATION = "trustyai.opendatahub.io/auth-config-hash"

# The paths trustyai-service-operator#1131 added, and the generic entry they have
# to precede: kube-rbac-proxy authorizes a request against the first endpoint
# whose path matches.
EVALUATIONS_PATH = "/api/v1/evaluations"
COLLECTIONS_PATH = f"{EVALUATIONS_PATH}/collections"
COLLECTIONS_WILDCARD_PATH = f"{COLLECTIONS_PATH}/*"
GENERATION_PATH = f"{COLLECTIONS_PATH}/generation"
GENERATION_WILDCARD_PATH = f"{GENERATION_PATH}/*"
GENERATION_MODELS_PATH = f"{GENERATION_PATH}/models"

# The header the policy maps to a namespace, and the two rewrites it uses.
TENANT_HEADER = "X-Tenant"
TENANT_NAMESPACE_REWRITE = "{{.FromHeader}}"
METHOD_VERB_REWRITE = "{{.FromMethod}}"

# The label the operator stamps on the resources it writes into a tenant namespace,
# and the suffix its tenant-user Role carries.
TENANT_RESOURCE_SELECTOR = "app.kubernetes.io/managed-by=trustyai-service-operator"
TENANT_USER_ROLE_SUFFIX = "-user"
TENANT_ROLE_TIMEOUT = 120
WAIT_INTERVAL = 5


def auth_config_map_name(deployment: client.V1Deployment) -> str:
    """Name of the ConfigMap whose `auth.yaml` the proxy container mounts.

    The ConfigMap is named after the instance *and* a digest of its policy, so a
    new policy gets a new name; the only way to find the one the running pod uses
    is through the volume the proxy mounts `auth.yaml` from.
    """
    pod_spec = deployment.spec.template.spec
    volumes = {volume.name: volume for volume in pod_spec.volumes or []}
    for container in pod_spec.containers or []:
        for mount in container.volume_mounts or []:
            if mount.sub_path != AUTH_CONFIG_KEY:
                continue
            volume = volumes.get(mount.name)
            if volume is not None and volume.config_map is not None:
                LOGGER.info(
                    "container %s mounts %s from ConfigMap %s", container.name, AUTH_CONFIG_KEY, volume.config_map.name
                )
                return volume.config_map.name
    pytest.fail(
        f"no container in deployment {deployment.metadata.name} mounts {AUTH_CONFIG_KEY} from a ConfigMap; "
        f"volumes: {sorted(volumes)}",
        pytrace=False,
    )


def auth_config_map(
    core_client: client.CoreV1Api, deployment: client.V1Deployment, namespace: str
) -> client.V1ConfigMap:
    """The ConfigMap holding the policy pod the proxy container runs with."""
    name = auth_config_map_name(deployment)
    config_map = core_client.read_namespaced_config_map(name=name, namespace=namespace)
    LOGGER.info("read policy ConfigMap %s/%s with keys %s", namespace, name, sorted(config_map.data or {}))
    return config_map


def auth_policy(config_map: client.V1ConfigMap) -> dict[str, Any]:
    """The parsed `auth.yaml` of a policy ConfigMap."""
    data = config_map.data or {}
    if AUTH_CONFIG_KEY not in data:
        pytest.fail(
            f"ConfigMap {config_map.metadata.namespace}/{config_map.metadata.name} has no {AUTH_CONFIG_KEY} key; "
            f"present: {sorted(data)}",
            pytrace=False,
        )
    text = data[AUTH_CONFIG_KEY]
    try:
        policy = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        pytest.fail(
            f"{AUTH_CONFIG_KEY} in ConfigMap {config_map.metadata.namespace}/{config_map.metadata.name} is not "
            f"valid YAML: {exc}",
            pytrace=False,
        )
    if not isinstance(policy, dict) or not isinstance(policy.get("authorization"), dict):
        pytest.fail(f"{AUTH_CONFIG_KEY} declares no authorization block: {policy!r}", pytrace=False)
    return policy


def auth_config_digest(text: str) -> str:
    """The SHA-256 the pod template records for an `auth.yaml`, lower-case hex."""
    return hashlib.sha256(text.encode()).hexdigest()


def endpoints(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """The endpoint rules in declaration order, which is the order they are matched in."""
    return (policy.get("authorization") or {}).get("endpoints") or []


def endpoint_paths(policy: dict[str, Any]) -> list[str]:
    """Declared paths, in the order the proxy matches them."""
    return [endpoint.get("path") for endpoint in endpoints(policy)]


def endpoint_index(policy: dict[str, Any]) -> dict[str, int]:
    """Map each declared path to its position in the policy."""
    return {endpoint.get("path"): index for index, endpoint in enumerate(endpoints(policy))}


def endpoint_for(policy: dict[str, Any], path: str) -> dict[str, Any]:
    """The endpoint rule declared for a path."""
    index = endpoint_index(policy)
    if path not in index:
        pytest.fail(
            f"the policy declares no {path} endpoint; declared paths: {endpoint_paths(policy)}",
            pytrace=False,
        )
    return endpoints(policy)[index[path]]


def single_mapping(policy: dict[str, Any], path: str) -> dict[str, Any]:
    """The one mapping an endpoint declares, failing with what it declares instead."""
    mappings = endpoint_for(policy, path).get("mappings") or []
    if len(mappings) != 1:
        pytest.fail(f"{path} declares {len(mappings)} mappings, expected one: {mappings}", pytrace=False)
    return mappings[0]


def mapping_resources(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """The resources one mapping authorizes against, each with its rewrites and attributes."""
    return mapping.get("resources") or []


def resource_attributes(resource: dict[str, Any]) -> dict[str, Any]:
    """The SubjectAccessReview attributes one mapping resource performs."""
    return resource.get("resourceAttributes") or {}


def tenant_user_role(
    rbac_client: client.RbacAuthorizationV1Api,
    namespace: str,
    timeout: int = TENANT_ROLE_TIMEOUT,
) -> client.V1Role:
    """The Role the operator writes in a tenant namespace for the tenant's own users.

    It is not owner-referenced, because it lives in another namespace, so it is
    recognised by the label the operator stamps on everything it writes there and
    by the `evaluations` rule only a tenant Role carries - which also keeps the
    Roles the checks create themselves out of the result. A namespace labelled as a
    tenant is reconciled by the operator's namespace watch, so the Role arrives a
    moment after the namespace does.
    """
    deadline = time.monotonic() + timeout
    names: list[str] = []
    while True:
        roles = rbac_client.list_namespaced_role(namespace=namespace, label_selector=TENANT_RESOURCE_SELECTOR).items
        names = [role.metadata.name for role in roles]
        candidates = [
            role for role in roles if any("evaluations" in (rule.resources or []) for rule in role.rules or [])
        ]
        if candidates:
            role = next(
                (role for role in candidates if role.metadata.name.endswith(TENANT_USER_ROLE_SUFFIX)),
                candidates[0],
            )
            LOGGER.info("%s: tenant Role %s: %s", namespace, role.metadata.name, role.rules)
            return role
        if time.monotonic() >= deadline:
            break
        LOGGER.info("waiting for the tenant Role in %s; operator-managed Roles: %s", namespace, sorted(names) or "none")
        time.sleep(WAIT_INTERVAL)
    pytest.fail(
        f"the operator wrote no tenant Role carrying an evaluations rule into {namespace} within {timeout}s; "
        f"operator-managed Roles present: {sorted(names) or 'none'}",
        pytrace=False,
    )


def rule_verbs(role: client.V1Role, resource: str) -> set[str]:
    """Every verb the Role grants on one pseudo-resource, across its matching rules."""
    verbs: set[str] = set()
    for rule in role.rules or []:
        if "trustyai.opendatahub.io" in (rule.api_groups or []) and resource in (rule.resources or []):
            verbs.update(rule.verbs or [])
    return verbs
