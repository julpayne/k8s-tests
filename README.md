# k8s-tests

Kubernetes-level tests and harnesses for the TrustyAI components.

## Local Kind harness

`Makefile` brings up a local [Kind](https://kind.sigs.k8s.io/) cluster and runs
the TrustyAI module and workload operators on it: it builds both operator images
from a `trustyai-service-operator` checkout, loads them into the cluster, and
deploys the module operator with its image values overridden to the images it
just built. The module operator then renders and deploys the workload operator
from the manifests baked into its image.

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
   renders (`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_OPERATOR_IMAGE`) and the
   namespace are all overridden without touching the checkout.
4. Waits for the module operator, seeds the `Prometheus` instance it expects,
   and creates the webhook and metrics TLS secrets that OpenShift's service CA
   would normally issue (Kind has no service CA).
5. Applies the TrustyAI module resource (`hack/manifests/trustyai-cr.yaml`),
   waits for the workload operator it deploys, and points the conversion
   webhooks of the `TrustyAIService` and `EvalHub` CRDs at the seeded CA.

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
  pods in the same namespace (`-l control-plane=trustyai-service-operator`).

## Pytest suite

`tests/` holds the pytest checks that run against a live cluster; they are kept
separate from the harness so they can point at any cluster that runs the same
operators. The first suite asserts that both operator Deployments exist, ask
for a single replica, and are healthy: `Available`, the desired replicas ready,
and their pods `Running` with every container ready.

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
(git-ignored) that keeps the captured log and failure detail per test:

```sh
make test REPORT_FILE=/tmp/trustyai.html       # choose the report path
```

Checks run against `KUBE_CONTEXT` (default `kind-trustyai`) and `NAMESPACE`
(default `system`), the values the harness uses by default. A missing kubeconfig
or context skips the suite; a missing Deployment fails with a pointer to
`make up`.

Layout:

- `tests/` — test modules, one file per area
- `utilities/` — helpers shared by the tests; `utilities/kube.py` loads the
  clients and turns Deployment and pod status into readable failures
- `conftest.py` — session fixtures for the context, namespace and API clients
- `pyproject.toml` — `kubernetes` and `pytest` dependencies plus the `ruff`
  configuration; the make targets prepare the environment and run them

`make lint` runs `ruff check` and `ruff format --check`; `make fmt` fixes both.
`.github/workflows/python-lint.yaml` runs `make lint` in CI on pushes to
`main` and on pull requests.
