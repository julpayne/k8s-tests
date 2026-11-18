#!/usr/bin/env bash
# Deploy the TrustyAI module operator on Kind, overriding the module operator
# image, the workload operator image it renders, the TrustyAI service and EvalHub
# images the workload operator resolves, and the target namespace.
#
# The operators: the module operator, plus the TrustyAI module resource and the
# workload operator it deploys from it. Nothing here depends on a manifest checked
# into this repository - the resource is built in Python. The operands on top of
# that (EvalHub instances and their namespaces and TLS material, TrustyAIService,
# the jobs that call a model) are created by the tests, the way a user or the
# platform operator would.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_operator_repo
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

module_config="$OPERATOR_REPO/trustyai-operator-module/config"
[[ -d "$module_config/default" ]] || die "missing $module_config/default"
crd_dir="$OPERATOR_REPO/tests/crds"
[[ -d "$crd_dir" ]] || die "missing $crd_dir (external CRD stand-ins)"

# External CRDs the rendered workload manifests reference. Kind has neither
# OpenShift Routes, the Prometheus Operator, nor KServe, but the operators still
# need the API types to exist so their watchers can start.
external_crds=(
	route_crd.yaml
	monitoring.coreos.com_prometheuses.yaml
	monitoring.coreos.com_servicemonitors.yaml
	serving.kserve.io_inferenceservices.yaml
)

info "creating namespace $NAMESPACE"
kube create namespace "$NAMESPACE" --dry-run=client -o yaml | kube apply -f -

info "applying external CRDs from $crd_dir"
for crd in "${external_crds[@]}"; do
	[[ -f "$crd_dir/$crd" ]] || die "missing $crd_dir/$crd"
	kube apply -f "$crd_dir/$crd" >/dev/null
done
for crd in routes.route.openshift.io prometheuses.monitoring.coreos.com \
	servicemonitors.monitoring.coreos.com inferenceservices.serving.kserve.io; do
	kube wait --for=condition=Established "crd/$crd" --timeout "$TIMEOUT" >/dev/null
done

# Render the module operator from a copy of the checkout's config tree so the
# image, the workload image it renders and the namespace can all be overridden
# without editing the operator checkout.
split_image "$MODULE_IMG"
module_name=$IMG_NAME module_tag=$IMG_TAG
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
cp -a "$module_config" "$workdir/config"
# EVALHUB_IMG overrides the operand image the module operator passes to the
# workload operator through params.env.
module_env_patch=$(cat <<EOF
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE
          value: $WORKLOAD_IMG
EOF
)
if [[ -n "${TRUSTYAI_SERVICE_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE
          value: $TRUSTYAI_SERVICE_IMG
EOF
)
fi
if [[ -n "${EVALHUB_IMG:-}" ]]; then
	module_env_patch+=$(cat <<EOF

      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_EVAL_HUB_IMAGE
          value: $EVALHUB_IMG
EOF
)
fi

cat >"$workdir/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
  - ./config/default
images:
  - name: trustyai-operator-module-controller
    newName: $module_name
    newTag: $module_tag
patches:
  - target:
      group: apps
      version: v1
      kind: Deployment
      name: trustyai-operator-module-controller-manager
    patch: |-
$module_env_patch
EOF

info "deploying the module operator ($MODULE_IMG) with workload image $WORKLOAD_IMG"
if [[ -n "${EVALHUB_IMG:-}" ]]; then
	info "passing EvalHub image $EVALHUB_IMG to the workload operator"
fi
if [[ -n "${TRUSTYAI_SERVICE_IMG:-}" ]]; then
	info "passing TrustyAI service image $TRUSTYAI_SERVICE_IMG to the workload operator"
fi
kube apply -k "$workdir" --server-side=true

# rollout status, not just Available: an existing pod set satisfies Available
# while a new image is still rolling out, and the next step reads what the new
# module operator renders.
info "waiting for the module operator deployment"
kube -n "$NAMESPACE" rollout status deployment/trustyai-operator-module-controller-manager \
	--timeout "$TIMEOUT"

info "seeding the Prometheus instance the module operator expects"
cat <<EOF | kube apply -f -
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: e2e-prometheus
  namespace: $NAMESPACE
EOF

# On OpenShift the service CA creates these from the serving-cert annotations;
# on Kind they have to exist before the operator pods can start. They are only
# created once so repeat deploys keep the same certificate.
create_tls_secrets() {
	local dir="$workdir/certs"
	python3 "$here/gen-certs.py" --namespace "$NAMESPACE" --out-dir "$dir"
	for secret in webhook-server-cert trustyai-service-operator-metrics-tls; do
		kube -n "$NAMESPACE" create secret tls "$secret" \
			--cert="$dir/tls.crt" --key="$dir/tls.key"
	done
}

if ! kube -n "$NAMESPACE" get secret webhook-server-cert >/dev/null 2>&1; then
	info "creating the webhook and metrics TLS secrets"
	create_tls_secrets
fi

# What `make up` promises also includes the stack under the module operator: the
# module resource, and the workload operator the module operator deploys from it.
# Both come from Python (utilities/stack.py, which builds the resource with the
# helpers the tests use and then waits for the Deployment, the images it renders
# and the conversion webhooks), so there is no manifest to keep in step with the
# operator. The operands on top of that - EvalHub instances, TrustyAIService, the
# jobs that call a model - stay the tests' business.
command -v uv >/dev/null 2>&1 || die "uv is required to create the TrustyAI module resource; install uv"
info "applying the TrustyAI module resource and waiting for the workload operator"
repo_root=$(cd "$here/.." && pwd)
(
	cd "$repo_root"
	KUBE_CONTEXT="$KUBE_CONTEXT" NAMESPACE="$NAMESPACE" uv run --quiet python -m utilities.stack
)

info "TrustyAI is deployed in namespace $NAMESPACE"
info "  the module operator and the workload operator it deploys are running"
info "  its operands are created by the tests: make test"
