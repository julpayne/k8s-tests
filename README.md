# k8s-tests

Kubernetes-level tests and harnesses for the TrustyAI components.

## Local Kind harness

`Makefile` brings up a local [Kind](https://kind.sigs.k8s.io/) cluster and runs
the TrustyAI module and workload operators on it: it builds both operator images
from a `trustyai-service-operator` checkout, loads them into the cluster, and
deploys the module operator with its image values overridden to the images it
just built. The module operator then renders and deploys the workload operator
from the manifests baked into its image. The harness stops there: EvalHub
instances are created by the tests, so the instance lifecycle is under test.

### Prerequisites

- `kind`, `kubectl`, and `python3` with the [`cryptography`][cryptography]
  package
- `podman` or `docker` to build the images
- a `trustyai-service-operator` checkout, passed as `OPERATOR_REPO`

[cryptography]: https://cryptography.io/

### Quick start

```sh
make up OPERATOR_REPO=/path/to/trustyai-service-operator
make status
make test       # pytest checks against what was just deployed
```

`make up` is the four steps below, run in order; each is also usable on its own:

```sh
make cluster    # create the Kind cluster (idempotent)
make images     # build the workload and module operator images from OPERATOR_REPO
make load       # load both images into the cluster
make deploy     # deploy the module operator, pointing it at the built images
```

`up`, `images` and `deploy` require `OPERATOR_REPO` and stop with a pointer to
the flag when it is missing or does not look like an operator checkout.

Tear the cluster down with `make down`; `make clean` also deletes the images
the harness built and the HTML report, without touching the cluster.

### What `make deploy` does

1. Creates the target namespace (`system` by default).
2. Applies the external CRD stand-ins the operators expect, copied from
   `$OPERATOR_REPO/tests/crds`: OpenShift `Route`, Prometheus Operator
   `Prometheus`/`ServiceMonitor`, and KServe `InferenceService`.
3. Renders `$OPERATOR_REPO/trustyai-operator-module/config/default` from a
   temporary copy, so the module operator image, the workload operator image it
   renders (`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE`), the EvalHub
   image the workload operator resolves (`RELATED_IMAGE_ODH_EVAL_HUB_IMAGE`,
   when `EVALHUB_IMG` is set) and the namespace are all overridden without
   touching the checkout.
4. Waits for the module operator, seeds the `Prometheus` instance it expects,
   and creates the webhook and metrics TLS secrets that OpenShift's service CA
   would normally issue (Kind has no service CA).
5. Applies the TrustyAI module resource (`hack/manifests/trustyai-cr.yaml`),
   waits for the workload operator it deploys, and points the conversion
   webhooks of the `TrustyAIService` and `EvalHub` CRDs at the seeded CA.

It deliberately deploys no EvalHub instance: `tests/test_evalhub.py` creates the
instance, its namespace and its TLS material itself, the way a user or the
platform operator would.

### Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPERATOR_REPO` | *(required)* | Checkout the images and manifests come from |
| `KIND_CLUSTER` | `trustyai` | Kind cluster name (context `kind-<name>`) |
| `KIND_NODE_IMAGE` | Kind's default | Pin a node image, e.g. `kindest/node:v1.29.0` |
| `KIND_PROVIDER` | auto | Force `podman` or `docker` on hosts that have both |
| `NAMESPACE` | `system` | Namespace both operators are deployed into |
| `WORKLOAD_IMG` | `localhost/trustyai-service-operator:dev` | Workload operator image reference |
| `MODULE_IMG` | `localhost/trustyai-operator-module:dev` | Module operator image reference |
| `EVALHUB_IMG` | *(empty)* | EvalHub operand image; empty keeps the operator's own pin (`quay.io/evalhub/evalhub:latest`) |
| `TAG` | `dev` | Tag used by both image references |
| `IMAGE_PREFIX` | `localhost` | Registry/repository prefix used by both image references |
| `ENGINE` | auto | `podman` or `docker`; detected when unset |
| `TIMEOUT` | `300s` | Timeout for each readiness wait |

### Notes and troubleshooting

- Image tags other than `latest` are important: `latest` would make Kubernetes
  pull instead of using the image loaded into the node.
- On hosts without the `docker` CLI the harness exports each image with
  `podman save` and loads it with `kind load image-archive`, since
  `kind load docker-image` always shells out to `docker`.
- The module image bakes the workload operator manifests at build time. If
  `config/` and `trustyai-operator-module/config/manifests-template/` have
  drifted, `make images` warns; refresh the template with
  `make -C $OPERATOR_REPO sync-trustyai-module-manifests`.
- `make logs` tails the module operator; the workload operator has its own
  pods in the same namespace (`-l control-plane=trustyai-service-operator`),
  and EvalHub runs as `-l app=eval-hub`.
- The EvalHub tests create their own instance with the sqlite database backend,
  since Kind has no PostgreSQL, and mint its `evalhub-tls` certificate and
  `evalhub-service-ca` bundle, which OpenShift's service CA would provide. The
  EvalHub image is pulled from the registry rather than loaded into the node, so
  the first `make test` needs network access. `make up EVALHUB_IMG=<ref>` points
  the instances those tests create at another image, for example one built from
  `eval-hub`.

## Pytest suite

`tests/` holds the pytest checks that run against a live cluster; they are kept
separate from the harness so they can point at any cluster that runs the same
operators.

| Module | What it checks |
| --- | --- |
| `tests/test_trustyai_operators.py` | the harness smoke checks: both operator Deployments exist, ask for a single replica, and are healthy (`Available`, the desired replicas ready, their pods `Running` with every container ready) |
| `tests/test_trustyai_module.py` | the modular stack: the module CRD is installed, both operator Deployments are available, `TrustyAI/default-trustyai` reports `Ready=True` with `ProvisioningSucceeded=True` and an up-to-date `observedGeneration`, and the module operator runs the built image |
| `tests/test_trustyai_workload.py` | the workload operator image: it matches the `RELATED_IMAGE_*` value the module operator renders it from, the image the harness built, and the operator's own ConfigMap |
| `tests/test_evalhub.py` | EvalHub: the CRD serves `v1` and `v1alpha1` through a conversion webhook; the instance the tests create reads back through both versions, reports `Ready`, runs one pod with the `evalhub` and `kube-rbac-proxy` containers and the labels the Service selects on; a namespace labelled as a tenant receives the discovery and CA ConfigMaps; and `/api/v1/health` answers `200` (and `405` to non-GET methods) |

Select one area with the markers:

```sh
make test PYTEST_ARGS="-m module"                          # the modular stack
make test PYTEST_ARGS="-m workload"                        # the workload operator
make test PYTEST_ARGS="-m evalhub"                         # the EvalHub instance
```

Checks that need an OLM-managed or platform-managed install are deliberately
absent, because the harness cannot provide one: the `odh-trustyai-config` and
`trustyai-dsc-config` ConfigMaps written by a platform operator, the check that
the image is listed in an operator CSV's `relatedImages` and released from
`registry.redhat.io` by digest, and the EvalHub provider, collection, job, MCP,
metrics and multi-tenancy suites, which need provider images, Kueue, MCP or the
platform's tenant RBAC.

The EvalHub API checks call `/api/v1/health` through a `kubectl port-forward` to
the Service. That path is exempt from the kube-rbac-proxy authorization the
operator configures, so the health checks need no token; they do not verify the
certificate, because a forwarded port is reached as 127.0.0.1 and the harness
certificate only covers the in-cluster Service names.

```sh
make test                                                  # default target
make test PYTEST_ARGS="-v"                                 # extra pytest flags
KUBE_CONTEXT=my-cluster NAMESPACE=opendatahub make test     # another cluster
```

Runs are verbose by design: at INFO level the suite logs the target context and
server version, the image each operator runs, replica and condition details, and
the readiness of every pod, so a passing run shows what was checked and a
failing one says what was not.

Every run also writes a self-contained HTML report to `reports/report.html`
(git-ignored) that keeps the captured log and failure detail per test. The run is
ordered by scope and the report shows it: one block each for EvalHub, the module
operator and the workload operator, a `Scope` column naming the block its first
row starts, and a per-scope count above the table.

```sh
make test REPORT_FILE=/tmp/trustyai.html       # choose the report path
```

Checks run against `KUBE_CONTEXT` (default `kind-trustyai`) and `NAMESPACE`
(default `system`), the values the harness uses by default. A missing kubeconfig
or context skips the suite; a missing Deployment fails with a pointer to
`make up`.

Layout:

- `tests/` — test modules, one file per area; `tests/conftest.py` holds the
  fixture that skips the module checks when a cluster does not run the modular
  stack
- `utilities/` — helpers shared by the tests; `utilities/kube.py` loads the
  clients and turns Deployment and pod status into readable failures, and
  `utilities/trustyai.py` holds the module, workload and image helpers
- `conftest.py` — session fixtures for the context, namespace and API clients
- `pyproject.toml` — `kubernetes` and `pytest` dependencies plus the `ruff`
  configuration; the make targets prepare the environment and run them

`make lint` runs `ruff check` and `ruff format --check`; `make fmt` fixes both.
`.github/workflows/python-lint.yaml` runs `make lint` in CI on pushes to
`main` and on pull requests.
