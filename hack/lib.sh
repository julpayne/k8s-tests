#!/usr/bin/env bash
# Shared helpers for the Kind harness. Sourced by the hack/*.sh scripts.

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

# container_engine prints the container engine to build images with. podman is
# preferred (it stores images locally and can push to the plain-HTTP registry
# without daemon configuration); docker is the fallback.
container_engine() {
	if [[ -n "${ENGINE:-}" ]]; then
		command -v "$ENGINE" >/dev/null 2>&1 || die "ENGINE=$ENGINE is not installed"
		printf '%s' "$ENGINE"
		return
	fi
	if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
		printf podman
	elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
		printf docker
	elif command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
		die "podman/docker is installed but not reachable; start the podman machine (podman machine start) or Docker Desktop"
	else
		die "neither podman nor docker is installed"
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

# host_platform prints the platform images should be built for: the engine's own
# OS/arch, which is linux/arm64 on Apple Silicon and linux/amd64 on Intel/Linux.
host_platform() {
	local engine platform
	engine=$(container_engine)
	if [[ "$engine" == docker ]]; then
		platform=$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null || true)
		if [[ -n "$platform" ]]; then
			printf '%s' "$platform"
			return
		fi
	fi
	case "$(uname -m)" in
	arm64 | aarch64) printf 'linux/arm64' ;;
	x86_64 | amd64) printf 'linux/amd64' ;;
	*) die "unsupported host architecture: $(uname -m)" ;;
	esac
}

# build_image builds one image for PLATFORM (host platform when unset) and loads
# it into the engine's image store. `docker build` is buildx, and with the
# docker-container driver the image stays in the builder cache unless --load is
# passed.
build_image() {
	local image=$1 context=$2 dockerfile=$3
	shift 3
	local engine platform
	engine=$(container_engine)
	platform=${PLATFORM:-$(host_platform)}
	local -a cmd
	if [[ "$engine" == docker ]] && docker buildx version >/dev/null 2>&1; then
		cmd=(docker buildx build --load)
	else
		cmd=("$engine" build)
	fi
	info "building $image for $platform"
	"${cmd[@]}" --platform "$platform" -t "$image" -f "$dockerfile" "$@" "$context"
}

# push_image pushes a locally built image. The registry listens on localhost over
# plain HTTP, which podman has to be told explicitly.
push_image() {
	local image=$1 engine
	engine=$(container_engine)
	info "pushing $image"
	if [[ "$engine" == podman ]]; then
		"$engine" push --tls-verify=false "$image"
	else
		"$engine" push "$image"
	fi
}

# registry_name is the registry container this Kind cluster's images go to.
registry_name() { printf 'k8s-tests-registry-%s' "${KIND_CLUSTER:-trustyai}"; }

# ensure_registry starts (or reuses) a registry:2 container on the kind network,
# published on 127.0.0.1:${REGISTRY_PORT}, and sets REGISTRY_ALIAS to the name
# the nodes resolve it by.
ensure_registry() {
	local engine name alias networks mapping status attempt
	engine=$(container_engine)
	name=$(registry_name)
	REGISTRY_PORT=${REGISTRY_PORT:-5005}
	alias="k8s-tests-reg-$(printf '%s' "${KIND_CLUSTER:-trustyai}" | cksum | awk '{print $1}')"
	REGISTRY_ALIAS=$alias
	"$engine" network inspect kind >/dev/null 2>&1 ||
		die "container network 'kind' does not exist; create the cluster with make cluster"

	if "$engine" container inspect "$name" >/dev/null 2>&1; then
		status=$("$engine" container inspect --format '{{.State.Status}}' "$name")
		[[ "$status" == running ]] || "$engine" container start "$name" >/dev/null
	else
		"$engine" image inspect docker.io/library/registry:2 >/dev/null 2>&1 ||
			"$engine" image pull docker.io/library/registry:2
		info "starting local registry $name on 127.0.0.1:${REGISTRY_PORT}"
		"$engine" run --detach --name "$name" \
			--label "io.k8s-tests.cluster=${KIND_CLUSTER:-trustyai}" \
			--publish "127.0.0.1:${REGISTRY_PORT}:5000" docker.io/library/registry:2 >/dev/null
	fi

	mapping=$("$engine" port "$name" 5000/tcp 2>/dev/null | head -n 1)
	[[ "$mapping" == "127.0.0.1:${REGISTRY_PORT}" ]] ||
		die "registry $name is published on ${mapping:-nothing}, not 127.0.0.1:${REGISTRY_PORT}; remove it or set REGISTRY_PORT"

	networks=$("$engine" container inspect --format '{{json .NetworkSettings.Networks}}' "$name")
	if [[ "$networks" != *'"kind":'* ]]; then
		"$engine" network connect --alias "$alias" kind "$name" >/dev/null
	elif [[ "$networks" != *"$alias"* ]]; then
		"$engine" network disconnect kind "$name" >/dev/null 2>&1 || true
		"$engine" network connect --alias "$alias" kind "$name" >/dev/null
	fi

	for attempt in {1..30}; do
		curl --silent --fail "http://127.0.0.1:${REGISTRY_PORT}/v2/" >/dev/null 2>&1 && return 0
		sleep 1
	done
	die "local registry $name did not become reachable on 127.0.0.1:${REGISTRY_PORT}"
}

# configure_registry_mirrors points each Kind node's containerd at the registry
# for localhost:${REGISTRY_PORT}. New clusters get the certs.d config path from
# hack/kind/cluster.yaml; a cluster created before that patch has it enabled in
# place (set PATCH_CONTAINERD=false to require a recreated cluster instead).
configure_registry_mirrors() {
	local engine node hosts
	engine=$(container_engine)
	hosts=$(mktemp)
	printf '[host."http://%s:5000"]\n  capabilities = ["pull", "resolve"]\n' "$REGISTRY_ALIAS" >"$hosts"
	chmod 644 "$hosts"
	while IFS= read -r node; do
		[[ -n "$node" ]] || continue
		if ! "$engine" exec "$node" grep -q 'certs.d' /etc/containerd/config.toml 2>/dev/null; then
			if [[ "${PATCH_CONTAINERD:-true}" != true ]]; then
				rm -f "$hosts"
				die "kind node $node does not load /etc/containerd/certs.d; recreate the cluster (make down && make cluster) so hack/kind/cluster.yaml's containerdConfigPatches takes effect"
			fi
			warn "kind node $node was created without the certs.d config path; enabling it in place"
			"$engine" exec "$node" sh -c 'cat >>/etc/containerd/config.toml <<EOF

[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
EOF'
			"$engine" exec "$node" systemctl restart containerd
		fi
		"$engine" exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REGISTRY_PORT}"
		"$engine" cp "$hosts" "$node:/etc/containerd/certs.d/localhost:${REGISTRY_PORT}/hosts.toml" >/dev/null
		info "configured localhost:${REGISTRY_PORT} mirror on $node"
	done < <("${KIND:-kind}" get nodes --name "${KIND_CLUSTER:-trustyai}")
	rm -f "$hosts"
}
