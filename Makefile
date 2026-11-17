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
# Required by `up`, `images` and `deploy`; there is no default.
OPERATOR_REPO ?=
TAG ?= dev
IMAGE_PREFIX ?= localhost
WORKLOAD_IMG ?= $(IMAGE_PREFIX)/trustyai-service-operator:$(TAG)
MODULE_IMG ?= $(IMAGE_PREFIX)/trustyai-operator-module:$(TAG)
# Leave empty to use docker when available, otherwise podman.
ENGINE ?=

TRUSTYAI_CR_FILE ?= hack/manifests/trustyai-cr.yaml
TIMEOUT ?= 300s
PYTEST_ARGS ?=
REPORT_FILE ?= reports/report.html

export KIND KUBECTL KIND_CLUSTER KUBE_CONTEXT NAMESPACE OPERATOR_REPO TAG \
	WORKLOAD_IMG MODULE_IMG ENGINE TRUSTYAI_CR_FILE TIMEOUT REPORT_FILE

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

.PHONY: check-operator-repo
check-operator-repo:
	@test -n "$(OPERATOR_REPO)" || { \
		echo "error: OPERATOR_REPO is required, e.g. make $(MAKECMDGOALS) OPERATOR_REPO=/path/to/trustyai-service-operator" >&2; \
		exit 2; \
	}
	@test -d "$(OPERATOR_REPO)" || { echo "error: OPERATOR_REPO=$(OPERATOR_REPO) does not exist" >&2; exit 2; }
	@test -f "$(OPERATOR_REPO)/Dockerfile" -a -f "$(OPERATOR_REPO)/trustyai-operator-module/Dockerfile" || { \
		echo "error: $(OPERATOR_REPO) does not look like a trustyai-service-operator checkout" >&2; \
		exit 2; \
	}

.PHONY: images
images: check-operator-repo ## Build the workload and module operator images from OPERATOR_REPO.
	hack/build-images.sh

.PHONY: load
load: ## Load the built images into the Kind cluster.
	hack/load-images.sh

.PHONY: deploy
deploy: check-operator-repo ## Deploy the module operator, overriding the image values with the built ones.
	hack/deploy.sh

.PHONY: up
up: check-operator-repo cluster images load deploy ## Create the cluster, build, load and deploy.

.PHONY: down
down: cluster-delete ## Delete the Kind cluster.

.PHONY: clean
clean: ## Delete the built operator images and the HTML report (leaves the cluster).
	hack/clean.sh

.PHONY: test
test: ## Run the pytest suite and write an HTML report.
	@uv run pytest $(PYTEST_ARGS) --html=$(REPORT_FILE) --self-contained-html || status=$$?; \
		echo "HTML report: $(abspath $(REPORT_FILE))"; \
		exit $${status:-0}

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
