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
