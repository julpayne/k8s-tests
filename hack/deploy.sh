#!/usr/bin/env bash
# Deploy the TrustyAI module operator on Kind, overriding the module operator
# image, the workload operator image it renders, and the target namespace.
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
      - op: add
        path: /spec/template/spec/containers/0/env/-
        value:
          name: RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE
          value: $WORKLOAD_IMG
EOF

info "deploying the module operator ($MODULE_IMG) with workload image $WORKLOAD_IMG"
kube apply -k "$workdir" --server-side=true

info "waiting for the module operator deployment"
kube -n "$NAMESPACE" wait --for=condition=Available \
	deployment/trustyai-operator-module-controller-manager --timeout "$TIMEOUT"

info "seeding the Prometheus instance the module operator expects"
cat <<EOF | kube apply -f -
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: e2e-prometheus
  namespace: $NAMESPACE
EOF

# On OpenShift the service CA creates these from the serving-cert annotations;
# on Kind they have to exist before the workload operator pods can start. They are
# only created once so repeat deploys keep the same certificate.
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
kube -n "$NAMESPACE" wait --for=condition=Available \
	deployment/trustyai-service-operator-controller-manager --timeout "$TIMEOUT"

for crd in trustyaiservices.trustyai.opendatahub.io evalhubs.trustyai.opendatahub.io; do
	if [[ $(kube get crd "$crd" -o jsonpath='{.spec.conversion.strategy}' 2>/dev/null) == Webhook ]]; then
		kube patch crd "$crd" --type=json \
			-p="[{\"op\":\"add\",\"path\":\"/spec/conversion/webhook/clientConfig/caBundle\",\"value\":\"$ca_bundle\"}]" \
			>/dev/null
		info "patched the $crd conversion webhook CA bundle"
	fi
done

info "TrustyAI is deployed in namespace $NAMESPACE"
