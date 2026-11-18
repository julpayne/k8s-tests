# k8s-tests

[![Python lint](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml/badge.svg?branch=main)](https://github.com/trustyai-explainability/k8s-tests/actions/workflows/python-lint.yaml)

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

- `kind` and `kubectl` 1.24 or newer, which the checks need to mint bearer
  tokens with `kubectl create token`
- `podman` or `docker` to build the images
- `uv`, which resolves the pinned Python 3.11+ dependencies from `uv.lock` for
  `make test`, `make test-evalhub-adapters`, `make lint` and `make fmt`
- `python3` with the [`cryptography`][cryptography] package, which `make deploy`
  runs `hack/gen-certs.py` with to create the TLS secrets Kind does not issue
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
the harness built and the HTML reports (`reports/`), without touching the
cluster.

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
| `KUBECTL` | `kubectl` | kubectl the harness and the checks shell out to, including the token the API checks mint |
| `MODEL_URL` | *(empty)* | OpenAI-compatible base URL the job checks call instead of the emulator; setting it stops the fixtures deploying the emulator |
| `MODEL_KEY` | *(empty)* | Credential for `MODEL_URL`; the fixtures write it to the `model-credentials` Secret under `api-key`, which the job names in `model.auth.secret_ref` |
| `MODEL_NAME` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Model name a job submits |
| `WORKLOAD_IMG` | `localhost/trustyai-service-operator:dev` | Workload operator image reference |
| `MODULE_IMG` | `localhost/trustyai-operator-module:dev` | Module operator image reference |
| `EVALHUB_IMG` | *(empty)* | EvalHub operand image; empty keeps the operator's own pin (`quay.io/evalhub/evalhub:latest`) |
| `TRUSTYAI_SERVICE_IMG` | `quay.io/trustyai/trustyai-service:latest` | TrustyAI service operand image, passed through `RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`; set it to a build under test |
| `TAG` | `dev` | Tag used by both image references |
| `IMAGE_PREFIX` | `localhost` | Registry/repository prefix used by both image references |
| `ENGINE` | auto | `podman` or `docker`; detected when unset |
| `TIMEOUT` | `300s` | Timeout for each readiness wait |
| `PYTEST_ARGS` | *(empty)* | Extra pytest arguments, e.g. `-m module`, `-m adapters` or `-k evalhub` |
| `REPORT_FILE` | `reports/report.html` | Report written by `make test` and `make test-all` |
| `ADAPTERS_REPORT_FILE` | `reports/adapters.html` | Report written by `make test-evalhub-adapters` |
| `KEEP_RESOURCES` | *(empty)* | Leave everything the run created on the cluster: see [Keeping the resources](#keeping-the-resources) |

The LLM emulator's `EMULATOR_IMG`, `EMULATOR_NAMESPACE` and `EMULATOR_TOKEN` are
documented under [LLM emulator](#llm-emulator).

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
- Every instance is named `evalhub` and reconciles every tenant-labelled
  namespace, including namespaces it did not create, so a `KEEP_RESOURCES` run or
  an interrupted one leaves an instance that keeps writing to later runs'
  namespaces. The run grants its own job access under a name the operator does not
  manage, so it does not depend on who reconciled last, and it logs a warning when
  it sees more than one instance. Delete what you do not need
  (`kubectl get evalhubs -A`).
- The LLM emulator (`vllm-emulator`) is deployed by the fixtures into the
  namespace under test and removed when the run ends; a job reaches it in-cluster
  as `vllm-emulator:8000`, which is what the benchmark and adapter checks
  submit. While a run is in flight it can be poked at from the host with
  `kubectl -n system port-forward svc/vllm-emulator 8000`.
- The EvalHub tests create their own instance with the sqlite database backend,
  since Kind has no PostgreSQL, and mint its `evalhub-tls` certificate and
  `evalhub-service-ca` bundle, which OpenShift's service CA would provide. The
  EvalHub image is pulled from the registry rather than loaded into the node, so
  the first `make test` needs network access; the adapter checks also fetch their
  benchmark's dataset from the Hub. `make up EVALHUB_IMG=<ref>` points
  the instances those tests create at another image, for example one built from
  `eval-hub`.

## LLM emulator

The checks that call a model use an emulator by default: the `vllm_emulator`
image, which answers OpenAI-shaped `/v1/completions` and `/v1/chat/completions`
without a GPU.

Set `MODEL_URL` to call a real, OpenAI-compatible endpoint instead. The fixtures
then deploy no emulator, so the checks run against a cluster that already serves
a model, and the emulator checks skip. `MODEL_KEY`, when set, is the credential
that endpoint requires: the fixtures create a `model-credentials` Secret in the
tenant namespace with the value under `api-key`, the key a job's
`model.auth.secret_ref` resolves, so the job payload never carries it.
`MODEL_NAME` overrides the model name a job submits.

```sh
make test MODEL_URL=http://model.example.svc:8000/v1 MODEL_KEY=sk-...
make test MODEL_URL=http://model.example.svc:8000/v1 MODEL_NAME=granite-3b
```

Without `MODEL_URL`, `MODEL_KEY` is also the token the emulator requires, so the
credential plumbing can be exercised with a key of your own.

The tests own the emulator. `tests/conftest.py` deploys it into the namespace
under test (and removes it afterwards), so `make up` leaves no emulator behind
and a run cannot depend on one someone left in the cluster. The image, namespace,
port and token are parameters:

```sh
make test                                  # default emulator, namespace and token
make test EMULATOR_NAMESPACE=my-ns         # somewhere else
make test EMULATOR_IMG=quay.io/.../vllm_emulator:<tag>
make test EMULATOR_TOKEN=another-token
```

The emulator image has no notion of credentials, so the fixture mounts a wrapper
over it from a ConfigMap (`hack/emulator/auth_emulator.py`) and starts that
instead. Every `/v1` request must then carry `Authorization: Bearer <token>`,
where the token is the `api-key` entry of the `vllm-emulator-auth` Secret - the
key an EvalHub job's `model.auth.secret_ref` resolves. `/health` and
`/__auth/state` stay open: the first is the probe, the second reports how many
requests were authenticated or rejected and the fingerprints of the tokens that
arrived (never the tokens), so a check can tell a forwarded credential from a
dropped one.

## Benchmark job

`tests/test_evalhub_benchmark.py` submits one real evaluation job through the
EvalHub API and reads its results:

- a ServiceAccount in a tenant namespace, granted the API by the RBAC the proxy
  authorizes against, minted as a bearer token by `kubectl create token`;
- a `model.auth.secret_ref` Secret in that namespace carrying `api-key`: the
  emulator's token, or `MODEL_KEY` when set. With `MODEL_KEY` the Secret has the
  known name `model-credentials`; otherwise it is `emulator-credentials`;
- the instance certificate copied into the tenant's `evalhub-service-ca`
  ConfigMap, which OpenShift's service CA would fill in and Kind cannot, so the
  evaluation sidecar can verify the instance it reports back to;
- the `arc_easy` benchmark of the `lm_evaluation_harness` provider with
  `num_examples: 1`, against the emulator's in-cluster URL or `MODEL_URL` when
  that is set.

It is the harness's own end-to-end check of the API, the sidecar and the
credential, so it stays in `make test`. Benchmarks that are the point of the run
rather than a check of the plumbing live in [EvalHub adapters](#evalhub-adapters),
one per entry in its table.

The job runs in the cluster: the adapter container calls the model through the
EvalHub sidecar, which injects the credential. The checks assert the job reaches
`completed`, that `results.benchmarks[0]` carries the benchmark id, provider and
numeric metrics, and, when the model is the emulator, that it saw the tenant
Secret's token on a `/v1` path. A real endpoint does not report what it
authenticated, so that last check skips rather than pretending the job's own
verdict proves the credential arrived. The first run of the day is the slow one -
the adapter image has to be pulled and the job scheduled (about six minutes on a
cold Kind node); afterwards the run is about a minute. It is marked `benchmark`,
so it can be filtered out:

```sh
make test PYTEST_ARGS="-m 'not benchmark'"
```

## EvalHub adapters

`tests/test_evalhub_adapters.py` is the section that runs real benchmarks. Every
entry in its `BENCHMARKS` table is one adapter run: submitted through the API,
waited for until the adapter finishes in the cluster, and read back as a result.
It covers one today, `ethics_cm` (commonsense moral judgment) of the
`lm_evaluation_harness` provider with `num_examples: 1`; adding a benchmark is one
more entry in that table, and every check then runs once for it, under a
`[<benchmark>]` id. Its jobs call the same endpoint the benchmark job does: the
emulator the fixtures deploy, or `MODEL_URL` when that is set, with `MODEL_KEY` as
the credential.

For each benchmark the section asserts that the job is accepted with exactly that
benchmark, its provider and its `num_examples`; that the job and the benchmark
alone reach `completed`; that `results.benchmarks[]` carries the benchmark, its
provider and numeric metrics; that the metric the provider config scores it on
(`acc` for `ethics_cm`) is among those metrics and is a rate; that the provider's
pass criteria turn the score into a `test` block; and, when the model is the
emulator, that it authenticated the job's model requests, so the model was called
rather than the job merely reporting success.

The section carries the `adapters` marker and nothing else, in both directions:
the EvalHub instance checks are `evalhub` and never select it, and it never
selects them. An adapter run costs minutes and fetches its benchmark's dataset
from the Hub, so `make test` leaves the section out and it runs on its own, with a
report of its own:

```sh
make test-evalhub-adapters                    # only this section, into reports/adapters.html
make test-evalhub-adapters KEEP_RESOURCES=1   # leave the job, its pod and the adapter logs behind
make test-all                                 # every section, the adapters included
make test PYTEST_ARGS="-m adapters"            # the same selection, into the usual report
```

## TrustyAI service

`tests/test_trustyai_service.py` creates a `TrustyAIService` itself, waits for
the operand the workload operator builds from it, and then talks to the API the
way a user would: through the TLS Service, with a bearer token.

The service image matters, because the operator's ODH manifest pins
`quay.io/opendatahub/trustyai-service:latest`, the retired Java build. That image
mounts a serving key the current deployment template no longer provides and
crash-loops on `/etc/tls/internal/tls.crt`. The harness therefore passes
`TRUSTYAI_SERVICE_IMG` (the Python service by default) through
`RELATED_IMAGE_ODH_TRUSTYAI_SERVICE_PY_IMAGE`, and `make test` asserts the
operand runs exactly that image:

```sh
make up OPERATOR_REPO=... TRUSTYAI_SERVICE_IMG=quay.io/you/trustyai-service@sha256:...
```

Three pieces are seeded by the fixtures because the platform supplies them on
OpenShift and Kind does not: the two serving certificates (`<name>-tls` and
`<name>-internal`, which the service CA issues from the serving-cert
annotations), and a ServiceAccount with `get` on the Service, which the proxy's
SubjectAccessReview checks. The API is reached over a port-forward, so the
certificate covers `localhost` and the checks verify the chain rather than
disabling it.

Two conditions the resource reports are platform-only here and asserted as such
rather than faked: `RouteAvailable` (`RouteNotFound`, since nothing admits the
Route) and `InferenceServicesPresent` (no model server registers), which is why
`Ready` stays `False` while the claim and the operand are available.

## Pytest suite

`tests/` holds the pytest checks that run against a live cluster; they are kept
separate from the harness so they can point at any cluster that runs the same
operators.

| Module | What it checks |
| --- | --- |
| `tests/test_trustyai_operators.py` | the harness smoke checks: both operator Deployments exist, ask for a single replica, and are healthy (`Available`, the desired replicas ready, their pods `Running` with every container ready) |
| `tests/test_trustyai_module.py` | the modular stack: the module CRD is installed, both operator Deployments are available, `TrustyAI/default-trustyai` reports `Ready=True` with `ProvisioningSucceeded=True` and an up-to-date `observedGeneration`, and the module operator runs the built image |
| `tests/test_trustyai_workload.py` | the workload operator image: it matches the `RELATED_IMAGE_*` value the module operator renders it from, the image the harness built, and the operator's own ConfigMap |
| `tests/test_emulator.py` | the LLM emulator (skipped when `MODEL_URL` is set): its Deployment runs the pinned image and is healthy, `/health` needs no token, `/v1/completions` and `/v1/chat/completions` answer `401` without the token or with another one and `200` with the token from the `vllm-emulator-auth` Secret, that Secret carries the `api-key` key `model.auth.secret_ref` resolves, and the token check reports which token fingerprint it saw |
| `tests/test_evalhub_benchmark.py` | a benchmark job: the API accepts a job for `arc_easy` with `num_examples: 1` against the emulator or `MODEL_URL`, it reaches `completed`, its results carry the benchmark id, provider and numeric metrics, and - against the emulator - the emulator saw the tenant Secret's token (or `MODEL_KEY`) on a `/v1` request |
| `tests/test_evalhub_adapters.py` | the EvalHub adapters section (opt-in, `make test-evalhub-adapters`): each benchmark in its table - currently `ethics_cm` of `lm_evaluation_harness`, with `num_examples: 1` against the emulator or `MODEL_URL` - is accepted as the job's only benchmark, reaches `completed`, reports the benchmark, provider, numeric metrics and the `acc` its provider scores it on, turns its pass criteria into a test outcome, and - against the emulator - the emulator authenticated the model requests it made |
| `tests/test_trustyai_service.py` | the TrustyAI service: the CRD serves `v1` and `v1alpha1` through a conversion webhook and a resource created as one reads back as the other; the operand rolls out one replica with the `trustyai-service`, `kube-rbac-proxy` and `internal-tls-proxy` containers, runs the requested image, carries the resource's schedule and storage settings, and mounts a bound claim at the requested size; `/q/health` answers `healthy` unauthenticated; `/info` needs a token and refuses one without the grant; both halves of a drift comparison upload and are counted; CompareMeans detects the shifted distribution; and a scheduled metric is listed once and deleted |
| `tests/test_evalhub.py` | EvalHub: the CRD serves `v1` and `v1alpha1` through a conversion webhook; the instance the tests create reads back through both versions, reports `Ready`, runs one pod with the `evalhub` and `kube-rbac-proxy` containers and the labels the Service selects on; a namespace labelled as a tenant receives the discovery and CA ConfigMaps; and `/api/v1/health` answers `200` (and `405` to non-GET methods) |

Select one area with the markers:

```sh
make test PYTEST_ARGS="-m module"                          # the modular stack
make test PYTEST_ARGS="-m workload"                        # the workload operator
make test PYTEST_ARGS="-m evalhub"                         # the EvalHub instance
make test PYTEST_ARGS="-m service"                         # the TrustyAI service
make test PYTEST_ARGS="-m emulator"                        # the LLM emulator
make test PYTEST_ARGS="-m benchmark"                       # the harness smoke job
make test-evalhub-adapters                                 # the EvalHub adapters (opt-in, own report)
```

`make test` runs every section except the EvalHub adapters, which are opt-in: see
[EvalHub adapters](#evalhub-adapters).

Checks that need an OLM-managed or platform-managed install are deliberately
absent, because the harness cannot provide one: the `odh-trustyai-config` and
`trustyai-dsc-config` ConfigMaps written by a platform operator, the check that
the image is listed in an operator CSV's `relatedImages` and released from
`registry.redhat.io` by digest, and the EvalHub provider, collection, MCP,
metrics and multi-tenancy suites, which need Kueue, MCP or the platform's tenant
RBAC. Of the providers, one job is covered: [EvalHub adapters](#evalhub-adapters)
runs `ethics_cm` of `lm_evaluation_harness` end to end, not the suite of them.

The EvalHub API checks call `/api/v1/health` through a `kubectl port-forward` to
the Service. That path is exempt from the kube-rbac-proxy authorization the
operator configures, so the health checks need no token; they do not verify the
certificate, because a forwarded port is reached as 127.0.0.1 and the harness
certificate only covers the in-cluster Service names.

```sh
make test                                                  # every section except the adapters
make test-evalhub-adapters                                 # only the EvalHub adapters
make test-all                                              # every section
make test PYTEST_ARGS="-v"                                 # extra pytest flags
KUBE_CONTEXT=my-cluster NAMESPACE=opendatahub make test     # another cluster
```

### Keeping the resources

By default the fixtures remove what they created. Set `KEEP_RESOURCES=1` - or run
`make test-keep`, or pass `--keep-resources` through `PYTEST_ARGS` - to leave it
all in place and look at it afterwards:

```sh
make test KEEP_RESOURCES=1
kubectl -n "$(kubectl get ns -o name | grep trustyai-evalhub | head -1 | cut -d/ -f2)" get all,cm,evalhubs,jobs
kubectl -n system get deploy,svc,cm,secret -l app=vllm-emulator
```

That keeps the EvalHub instance and its tenant namespace, the LLM emulator and its
Secret and wrapper ConfigMap, and the evaluation job with its spec ConfigMap, its
pod and the adapter logs. The run logs each namespace it leaves behind and the HTML
report says so too. Nothing is kept silently: with the default setting the same
resources are removed at the end of the session.

> **Warning: kept resources affect later runs.** Every EvalHub instance is named
> `evalhub` and reconciles *every* tenant-labelled namespace, including namespaces
> it did not create, so a kept instance keeps writing into a later run's tenant
> namespace. The run grants its own job access, so a re-run still passes, but the
> namespaces pile up, the kept instance competes for the same tenant resources, and
> the emulator keeps its port in the namespace under test. Delete the leftovers
> before a run you want to be clean:
>
> ```sh
> kubectl get ns -o name | grep trustyai-evalhub | xargs -r kubectl delete ns
> kubectl -n system delete deploy,svc -l app=vllm-emulator
> kubectl -n system delete cm/vllm-emulator-auth-script secret/vllm-emulator-auth
> ```
>
> The run repeats this warning at start-up whenever `KEEP_RESOURCES` is set.

Runs are verbose by design: at INFO level the suite logs the target context and
server version, the image each operator runs, replica and condition details, and
the readiness of every pod, so a passing run shows what was checked and a
failing one says what was not.

Every run also writes a self-contained HTML report (git-ignored) that keeps the
captured log and failure detail per test: `reports/report.html` for `make test`
and `make test-all`, `reports/adapters.html` for `make test-evalhub-adapters`.
The run is ordered by scope and the report shows it: one block each for EvalHub,
the EvalHub adapters, the LLM emulator, the module operator, the TrustyAI service
and the workload operator, a `Scope` column naming the block its first row
starts, and a per-scope count above the table.

```sh
make test REPORT_FILE=/tmp/trustyai.html                     # choose the report path
make test-evalhub-adapters ADAPTERS_REPORT_FILE=/tmp/a.html  # the same for the adapters
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
  clients and turns Deployment and pod status into readable failures,
  `utilities/trustyai.py` holds the module, workload and image helpers, and
  `utilities/model.py` chooses the model endpoint (`MODEL_URL`/`MODEL_KEY`) and
  writes the credential Secret it resolves
- `conftest.py` — session fixtures for the context, namespace and API clients
- `pyproject.toml` — the dependencies (`kubernetes`, `pytest`, `requests`,
  `cryptography`) with the `pytest-html` and `ruff` dev group, the marker
  registry, and the `ruff` configuration; `uv.lock` pins them and the make
  targets run them through `uv`

`make lint` runs `ruff check` and `ruff format --check`; `make fmt` fixes both.
`.github/workflows/python-lint.yaml` runs `make lint` in CI on pushes to
`main` and on pull requests.
