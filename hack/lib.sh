#!/usr/bin/env bash
# Shared helpers for the Kind harness. Sourced by the hack/*.sh scripts.

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

# container_engine prints the container engine to build images with.
container_engine() {
	if [[ -n "${ENGINE:-}" ]]; then
		command -v "$ENGINE" >/dev/null 2>&1 || die "ENGINE=$ENGINE is not installed"
		printf '%s' "$ENGINE"
		return
	fi
	if command -v docker >/dev/null 2>&1; then
		printf docker
	elif command -v podman >/dev/null 2>&1; then
		printf podman
	else
		die "neither docker nor podman is installed"
	fi
}

# kube is the kubectl invocation every script uses, pinned to the Kind context.
kube() {
	"${KUBECTL:-kubectl}" --context "${KUBE_CONTEXT:-kind-kind}" "$@"
}

# split_image sets IMG_NAME and IMG_TAG for "registry:port/name:tag" style refs.
split_image() {
	local ref=$1
	if [[ ${ref##*/} == *:* ]]; then
		IMG_TAG=${ref##*:}
		IMG_NAME=${ref%:*}
	else
		IMG_NAME=$ref
		IMG_TAG=latest
	fi
}

require_operator_repo() {
	[[ -n "${OPERATOR_REPO:-}" ]] ||
		die "OPERATOR_REPO must point at a trustyai-service-operator checkout"
	[[ -d "$OPERATOR_REPO" ]] || die "OPERATOR_REPO=$OPERATOR_REPO does not exist"
}
