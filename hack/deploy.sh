#!/usr/bin/env bash
# Deploy the TrustyAI module operator on Kind, overriding the module operator
# image, the workload operator image it renders, the TrustyAI service and EvalHub
# images the workload operator resolves, and the target namespace.
#
# The operators only: EvalHub instances, their namespaces and their TLS material
# are created by the tests, the way a user or the platform operator would.
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
ca_bundle=$(kube -n "$NAMESPACE" get secret webhook-server-cert -o jsonpath='{.data.tls\.crt}')

info "applying the TrustyAI module resource from $(basename "$TRUSTYAI_CR_FILE")"
kube apply -f "$TRUSTYAI_CR_FILE"

info "waiting for the module operator to deploy the workload operator"
for _ in $(seq 1 90); do
	if kube -n "$NAMESPACE" get deployment/trustyai-service-operator-controller-manager >/dev/null 2>&1; then
		break
	fi
	sleep 2
done
kube -n "$NAMESPACE" get deployment/trustyai-service-operator-controller-manager >/dev/null ||
	die "the module operator did not create the workload operator deployment"

# The module operator renders the workload operator's pod spec (its image and
# the RELATED_IMAGE_* values it resolves operands from) from params.env, and it
# rewrites params.env from its own env only on its next reconcile. Wait for the
# rendered values here: creating an operand before they land would pin the old
# image, which is exactly what a re-deploy with a new image would otherwise do.
wait_for_workload_image() {
	local want=$1 image=""
	for _ in $(seq 1 90); do
		image=$(kube -n "$NAMESPACE" get deployment/trustyai-service-operator-controller-manager \
			-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
		if [[ "$image" == "$want" ]]; then
			info "workload operator image is $want"
			return 0
		fi
		sleep 2
	done
	die "the module operator never rendered the workload operator image $want (last: ${image:-unset})"
}

wait_for_operand_image_env() {
	local env_name=$1 want=$2 envs key value got=""
	for _ in $(seq 1 90); do
		envs=$(kube -n "$NAMESPACE" get deployment/trustyai-service-operator-controller-manager \
			-o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' 2>/dev/null || true)
		got=""
		while IFS='=' read -r key value; do
			if [[ "$key" == "$env_name" ]]; then
				got=$value
				break
			fi
		done <<<"$envs"
		if [[ "$got" == "$want" ]]; then
			info "workload operator resolves $env_name=$want"
			return 0
		fi
		sleep 2
	done
	die "the workload operator never rendered $env_name=$want (last: ${got:-unset})"
}

wait_for_workload_image "$WORKLOAD_IMG"

# The TrustyAI service image the workload operator should resolve. The harness
# always sets it, so an override and a return to the default are both gated.
if [[ -n "${TRUSTYAI_SERVICE_IMG:-}" ]]; then
	wait_for_operand_image_env RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE "$TRUSTYAI_SERVICE_IMG"
fi

# The EvalHub image the workload operator should resolve: the requested one, or
# the default the module image bakes into params.env. Gating on both directions
# keeps a re-deploy without EVALHUB_IMG from leaving an operand pinned to the
# previously requested image.
evalhub_want=$EVALHUB_IMG
if [[ -z "$evalhub_want" ]]; then
	params_env="$OPERATOR_REPO/trustyai-operator-module/config/manifests-template/base/params.env"
	if [[ -f "$params_env" ]]; then
		evalhub_want=$(grep -m1 '^evalHubImage=' "$params_env" | cut -d= -f2- || true)
	fi
fi
if [[ -n "$evalhub_want" ]]; then
	wait_for_operand_image_env RELATED_IMAGE_ODH_EVAL_HUB_IMAGE "$evalhub_want"
else
	warn "could not read the default evalHubImage from params.env; not waiting for the operand image"
fi
kube -n "$NAMESPACE" rollout status deployment/trustyai-service-operator-controller-manager \
	--timeout "$TIMEOUT"

for crd in trustyaiservices.trustyai.opendatahub.io evalhubs.trustyai.opendatahub.io; do
	if [[ $(kube get crd "$crd" -o jsonpath='{.spec.conversion.strategy}' 2>/dev/null) == Webhook ]]; then
		kube patch crd "$crd" --type=json \
			-p="[{\"op\":\"add\",\"path\":\"/spec/conversion/webhook/clientConfig/caBundle\",\"value\":\"$ca_bundle\"}]" \
			>/dev/null
		info "patched the $crd conversion webhook CA bundle"
	fi
done

info "TrustyAI is deployed in namespace $NAMESPACE"
