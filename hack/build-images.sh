#!/usr/bin/env bash
# Build the TrustyAI workload and module operator images from OPERATOR_REPO.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_operator_repo
engine=$(container_engine)

module_dir="$OPERATOR_REPO/trustyai-operator-module"
[[ -f "$module_dir/Dockerfile" ]] || die "missing $module_dir/Dockerfile"

# The module image bakes the workload operator manifests at build time, so a
# stale template silently deploys the previous workload manifests.
if ! diff -rq "$OPERATOR_REPO/config" "$module_dir/config/manifests-template" >/dev/null 2>&1; then
	warn "config/ and trustyai-operator-module/config/manifests-template/ differ;"
	warn "run 'make -C $OPERATOR_REPO sync-trustyai-module-manifests' to refresh the template"
fi

info "building workload operator image $WORKLOAD_IMG with $engine"
"$engine" build \
	--build-arg "CI_CONTAINER_VERSION=$TAG" \
	-t "$WORKLOAD_IMG" \
	-f "$OPERATOR_REPO/Dockerfile" \
	"$OPERATOR_REPO"

info "building module operator image $MODULE_IMG with $engine"
"$engine" build \
	--build-arg "VERSION=$TAG" \
	-t "$MODULE_IMG" \
	-f "$module_dir/Dockerfile" \
	"$module_dir"

info "built $WORKLOAD_IMG and $MODULE_IMG"
