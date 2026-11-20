#!/usr/bin/env bash
# Remove the images this harness built and the generated HTML report. The Kind
# cluster is left alone: that is `make down`.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$here/.." && pwd)

# Only the images need a container engine; the reports do not. With the podman
# machine stopped the images and the registry are left for a later run instead of
# failing a clean that is mostly about reports/.
if engine=$(reachable_container_engine); then
	for image in "${WORKLOAD_IMG:-}" "${MODULE_IMG:-}"; do
		[[ -n "$image" ]] || continue
		if ! "$engine" image inspect "$image" >/dev/null 2>&1; then
			info "image $image is not present"
			continue
		fi
		info "removing image $image"
		"$engine" rmi "$image" >/dev/null || warn "could not remove $image"
	done

	# The registry `make images-all` starts holds the images the cluster pulled
	# from it; removing the container removes them with it. The local copies stay
	# in the engine's store, so remove the ones `make images-all` recorded - not
	# every image under the registry prefix, which would also delete an image a
	# user pushed there by hand (the kube-rbac-proxy build in the README, for one).
	env_file=${IMAGES_ENV_FILE:-$repo_root/reports/images.env}
	[[ "$env_file" == /* ]] || env_file="$repo_root/$env_file"
	registry=$(registry_name)
	if "$engine" container inspect "$registry" >/dev/null 2>&1; then
		info "removing local registry $registry"
		"$engine" container rm -f "$registry" >/dev/null || warn "could not remove $registry"
	fi
	if [[ -f "$env_file" ]]; then
		while IFS= read -r line; do
			[[ -n "$line" && "$line" != "#"* && "$line" == *=* ]] || continue
			image=${line#*=}
			[[ -n "$image" ]] || continue
			info "removing registry image $image"
			"$engine" rmi "$image" >/dev/null || warn "could not remove $image"
		done <"$env_file"
	fi
else
	warn "podman/docker is not reachable; leaving the built images and the local registry alone"
fi

report_dir=$(dirname "${REPORT_FILE:-reports/report.html}")
if [[ -d "$report_dir" ]]; then
	info "removing $report_dir"
	rm -rf "$report_dir"
fi
