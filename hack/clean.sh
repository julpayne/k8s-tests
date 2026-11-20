#!/usr/bin/env bash
# Remove the images this harness built and the generated HTML report. The Kind
# cluster is left alone: that is `make down`.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

engine=$(container_engine)

for image in "$WORKLOAD_IMG" "$MODULE_IMG"; do
	if ! "$engine" image inspect "$image" >/dev/null 2>&1; then
		info "image $image is not present"
		continue
	fi
	info "removing image $image"
	"$engine" rmi "$image" >/dev/null || warn "could not remove $image"
done

report_dir=$(dirname "${REPORT_FILE:-reports/report.html}")
if [[ -d "$report_dir" ]]; then
	info "removing $report_dir"
	rm -rf "$report_dir"
fi

# The registry `make images-all` starts holds the images the cluster pulled from
# it; removing the container removes them with it. The local copies remain in
# the engine's store, so remove the ones tagged for the registry as well.
registry=$(registry_name)
if "$engine" container inspect "$registry" >/dev/null 2>&1; then
	info "removing local registry $registry"
	"$engine" container rm -f "$registry" >/dev/null || warn "could not remove $registry"
fi
registry_prefix="localhost:${REGISTRY_PORT:-5005}/"
while IFS= read -r image; do
	[[ -n "$image" ]] || continue
	info "removing registry image $image"
	"$engine" rmi "$image" >/dev/null || warn "could not remove $image"
done < <("$engine" images --format '{{.Repository}}:{{.Tag}}' | grep -F "$registry_prefix" || true)
