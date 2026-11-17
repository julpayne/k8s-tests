# Local Kind harness for the TrustyAI module and workload operators.
#
#   make up OPERATOR_REPO=/path/to/trustyai-service-operator
#
# creates a Kind cluster, builds the workload and module operator images from
# that checkout, loads them into the cluster, and deploys the module operator
# pointed at the freshly built workload operator image.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -o pipefail -ec
.DEFAULT_GOAL := help

KUBECTL ?= kubectl
KIND ?= kind

KIND_CLUSTER ?= trustyai
KIND_CONFIG ?= hack/kind/cluster.yaml
# Empty uses the node image Kind ships with.
KIND_NODE_IMAGE ?=
KUBE_CONTEXT ?= kind-$(KIND_CLUSTER)
# Leave empty to let Kind pick: docker when it is usable, otherwise podman.
# Set to `podman` or `docker` to force a provider on machines that have both.
KIND_PROVIDER ?=
KIND_ENV := $(if $(KIND_PROVIDER),KIND_EXPERIMENTAL_PROVIDER=$(KIND_PROVIDER))

NAMESPACE ?= system

# Checkout of trustyai-service-operator used for the images and the manifests.
OPERATOR_REPO ?= $(HOME)/Sync/code/reference/trustyai-service-operator
TAG ?= dev
IMAGE_PREFIX ?= localhost
WORKLOAD_IMG ?= $(IMAGE_PREFIX)/trustyai-service-operator:$(TAG)
MODULE_IMG ?= $(IMAGE_PREFIX)/trustyai-operator-module:$(TAG)
# Leave empty to use docker when available, otherwise podman.
ENGINE ?=

TRUSTYAI_CR_FILE ?= hack/manifests/trustyai-cr.yaml
TIMEOUT ?= 300s
PYTEST_ARGS ?=

export KIND KUBECTL KIND_CLUSTER KUBE_CONTEXT NAMESPACE OPERATOR_REPO TAG \
	WORKLOAD_IMG MODULE_IMG ENGINE TRUSTYAI_CR_FILE TIMEOUT

.PHONY: help
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z_0-9-]+:.*## / {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: cluster
cluster: ## Create the Kind cluster (idempotent).
	$(KIND_ENV) $(KIND) get clusters | grep -qx '$(KIND_CLUSTER)' || \
		$(KIND_ENV) $(KIND) create cluster --name $(KIND_CLUSTER) --config $(KIND_CONFIG) \
			$(if $(KIND_NODE_IMAGE),--image $(KIND_NODE_IMAGE)) --wait 5m
	$(KUBECTL) --context $(KUBE_CONTEXT) wait --for=condition=Ready nodes --all --timeout $(TIMEOUT)

.PHONY: cluster-delete
cluster-delete: ## Delete the Kind cluster.
	$(KIND_ENV) $(KIND) delete cluster --name $(KIND_CLUSTER)

.PHONY: images
images: ## Build the workload and module operator images from OPERATOR_REPO.
	hack/build-images.sh

.PHONY: load
load: ## Load the built images into the Kind cluster.
	hack/load-images.sh

.PHONY: deploy
deploy: ## Deploy the module operator, overriding the image values with the built ones.
	hack/deploy.sh

.PHONY: up
up: cluster images load deploy ## Create the cluster, build, load and deploy.

.PHONY: down
down: cluster-delete ## Tear everything down.

.PHONY: test
test: ## Run the pytest suite against the deployed operators.
	uv run pytest $(PYTEST_ARGS)

.PHONY: lint
lint: ## Lint and format-check the Python sources.
	uv run ruff check .
	uv run ruff format --check .

.PHONY: fmt
fmt: ## Format and autofix the Python sources.
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: status
status: ## Show the module CR, the two operators and their pods.
	$(KUBECTL) --context $(KUBE_CONTEXT) get trustyais.components.platform.opendatahub.io --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) get deployment,pods \
		-l app.kubernetes.io/name=trustyai-operator-module --ignore-not-found
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) get deployment,pods \
		-l control-plane=trustyai-service-operator --ignore-not-found

.PHONY: logs
logs: ## Tail the module and workload operator logs.
	$(KUBECTL) --context $(KUBE_CONTEXT) -n $(NAMESPACE) logs \
		-l control-plane=controller-manager --tail=100 -f
