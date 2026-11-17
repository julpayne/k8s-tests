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
- a `trustyai-service-operator` checkout (defaults to
  `~/Sync/code/reference/trustyai-service-operator`)

[cryptography]: https://cryptography.io/

### Quick start

```sh
make up OPERATOR_REPO=/path/to/trustyai-service-operator
make status
```

`make up` is the four steps below, run in order; each is also usable on its own:

```sh
make cluster    # create the Kind cluster (idempotent)
make images     # build the workload and module operator images from OPERATOR_REPO
make load       # load both images into the cluster
make deploy     # deploy the module operator, pointing it at the built images
```

Tear everything down with `make down`.

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
| `OPERATOR_REPO` | `~/Sync/code/reference/trustyai-service-operator` | Checkout the images and manifests come from |
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
