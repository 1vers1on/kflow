# kflow Configuration Reference

This document is the complete field-level reference for both file types kflow reads:

- [Root config](#root-config) (`kflow.kind: Config`) - one per project
- [Resource definition](#resource-definition) (`kflow.kind: ResourceDefinition`) - one or more, listed in the root config
- [Step types](#step-types) - the building blocks inside a resource definition

For the conceptual overview see the [README](../README.md). For runner authoring see [writing-runners.md](writing-runners.md).

---

## File identity

Both file types carry a top-level `kflow:` block that marks them as kflow files and identifies their kind. A YAML file without this block is treated as a plain Kubernetes manifest and passed to `kubectl` verbatim.

```yaml
kflow:
  version: v1
  kind: Config              # or: ResourceDefinition
```

kflow loads multi-document YAML files (separated by `---`). Every document in a resource file must be a `ResourceDefinition`.

---

## Root config

The root config is the single entry point. By default kflow looks for `kflow.yaml` in the current directory; override with `-c`.

### Full example

```yaml
kflow:
  version: v1
  kind: Config

state:
  dir: ~/.kflow

context: my-cluster

runners:
  - runners/db_runner.py
  - runners/cache_runner.py

phases:
  - name: storage
    description: Persistent storage layer
  - name: ingress-controller
    description: Traefik ingress controller
  - name: ingress
    description: Ingress objects that need the controller to exist
  - name: apps
    description: Application workloads

resources:
  - resources/longhorn-storage.yaml
  - resources/traefik.yaml
  - resources/longhorn-ingress.yaml
  - resources/               # a directory: loads all *.yaml files in sorted order
```

### Fields

#### `state.dir`

```yaml
state:
  dir: ~/.kflow
```

Directory where kflow writes its local state file (`state.json`). State is keyed by kube-context, so a single state dir can track multiple clusters. Supports `~` expansion. Default: `~/.kflow`.

#### `context`

```yaml
context: my-cluster
```

The kubeconfig context to use for all kubectl and helm calls. Omit to use the active context (`kubectl config current-context`). Can be overridden at runtime with `--context` or `KFLOW_CONTEXT`.

#### `runners`

```yaml
runners:
  - runners/db_runner.py
  - path/to/another_runner.py
```

Paths to Python files that define `BaseRunner` subclasses, relative to the root config file. Every class found in these files is registered globally - any resource definition can reference it by name without specifying `file:` in its runner step.

Files are loaded in declaration order. Duplicate registry names across files raise a `RunnerLoadError`.

#### `phases`

```yaml
phases:
  - storage                  # shorthand: just a name
  - name: ingress-controller
    description: Traefik ingress controller
  - name: apps
```

An ordered list of phase names. Phases are a **strict outer ordering**: every step in phase *N* completes before any step in phase *N+1* begins.

Resources that declare no `phase:` are placed in an implicit `__default__` phase that runs after all declared phases.

Each phase entry is either a bare string (name only) or a mapping with:

| Field | Required | Description |
| --- | --- | --- |
| `name` | yes | Phase identifier. Referenced by `phase:` in resource definitions. |
| `description` | no | Human-readable label shown in `kflow graph` and `kflow list`. |

#### `resources`

```yaml
resources:
  - resources/app.yaml
  - resources/traefik.yaml
  - resources/               # directory
```

Paths to resource definition files or directories, relative to the root config file. Directories are expanded to all `*.yaml` / `*.yml` files in sorted order. Declaration order does not affect execution order - that is computed from phases and `dependsOn`. Duplicate resource names across files raise a `ConfigError`.

---

## Resource definition

A resource is a named, ordered collection of steps that kflow applies, destroys, and reloads together.

### Full example

```yaml
kflow:
  version: v1
  kind: ResourceDefinition

name: app
namespace: demo
phase: apps
description: Demo web application.

keepNamespace: false

dependsOn:
  - longhorn-ingress          # wait for this whole resource
  - traefik.install           # or a specific step in another resource

selector: app=web
workloads:
  - deployment/web
  - deployment/api

steps:
  - name: config
    manifests:
      - manifests/app-configmap.yaml
  - name: deploy
    dependsOn: [config, longhorn-ingress]
    manifests:
      - manifests/app-deployment.yaml
  - name: wait-ready
    dependsOn: [deploy]
    wait:
      for: deployment/web
      condition: available
      timeout: 120
  - name: migrate
    dependsOn: [wait-ready]
    runner:
      class: DatabaseRunner
      config:
        database: appdb
        seed: true
```

### Top-level fields

| Field | Required | Description |
| --- | --- | --- |
| `name` | yes | Unique resource identifier. Used in `dependsOn` references and CLI targeting. |
| `namespace` | no | Kubernetes namespace. Default: `default`. Applied as `-n <ns>` to every manifest/helm step that doesn't override it. |
| `phase` | no | Which phase this resource belongs to. Must match a name declared in `phases:`. Omit to use the implicit default phase. |
| `description` | no | Human-readable description shown in `kflow list`. |
| `keepNamespace` | no | When `true`, `kflow destroy --delete-namespaces` skips deleting this resource's namespace. Default: `false`. |
| `dependsOn` | no | Resource-level dependencies. The first step of this resource will not start until these are complete. Each entry is a resource name or a `resource.step` reference. |
| `selector` | no | Label selector (e.g. `app=web`) used by `restart`, `reload`, and `logs` to find live workloads. |
| `workloads` | no | Explicit list of `kind/name` entries (e.g. `deployment/web`) for restart/reload. When set, takes precedence over `selector` for rollout operations. |
| `steps` | no | Ordered list of step definitions (see [Step types](#step-types)). |

### Dependency references

Any `dependsOn` entry (at the resource level or within a step) can be:

- `resourceName` - waits for the **last step** of that resource.
- `resourceName.stepName` - waits for a specific step in another resource.
- `stepName` - within a step's `dependsOn`, a bare name first checks for a step with that name in the **same resource**, then falls back to treating it as a resource name.

Cross-phase dependencies work in both directions: a backward dependency (depending on a resource in an earlier phase) is automatically satisfied by phase ordering. A forward dependency (depending on a resource in a *later* phase) is reported as a warning and ignored - the phase ordering takes effect instead.

---

## Step types

Steps are declared in `steps:` as a list. Each step has a `name:` and a `dependsOn:` list (both optional for the latter), plus exactly one type-specific block.

```yaml
steps:
  - name: my-step           # required; unique within the resource
    dependsOn:              # optional; list of step/resource references
      - other-step
    <type-block>: ...
```

Steps within a resource run in **declaration order** by default. `dependsOn` adds additional ordering constraints. The engine enforces strict topological ordering across all steps and all resources.

---

### `manifests`

Apply one or more Kubernetes manifest files (or URLs) with `kubectl apply`.

```yaml
- name: deploy
  manifests:
    - manifests/app-deployment.yaml
    - manifests/app-service.yaml
    - https://example.com/some-manifest.yaml   # remote URL
```

| Field | Required | Description |
| --- | --- | --- |
| `manifests` | yes | List of paths (relative to the resource file) or `http://`/`https://` URLs. |

- On **apply** and **reload**: `kubectl apply -n <namespace> -f <path>` for each entry.
- On **destroy**: `kubectl delete -n <namespace> -f <path> --ignore-not-found` for each entry, in reverse order.
- Manifest hashes are recorded in state; `kflow status` reports files whose content has changed since the last apply as **drift**.
- Remote URLs are applied as-is but excluded from drift detection.

---

### `helm`

Install or upgrade a Helm chart with `helm upgrade --install`.

```yaml
- name: install
  helm:
    release: traefik
    chart: traefik/traefik
    version: 28.0.0
    namespace: traefik          # optional; defaults to resource namespace
    repo:
      name: traefik
      url: https://traefik.github.io/charts
    valuesFiles:
      - ../values/traefik.yaml
    values:
      service:
        type: LoadBalancer
      replicaCount: 2
```

| Field | Required | Description |
| --- | --- | --- |
| `release` | no | Helm release name. Defaults to the resource name. |
| `chart` | yes | Chart reference (e.g. `repo/chart`, `./path/to/chart`, OCI ref). |
| `namespace` | no | Namespace for the release. Defaults to the resource namespace. |
| `version` | no | Chart version (`--version`). Omit to use the latest. |
| `repo.name` | no | Helm repo name to register (`helm repo add <name> <url>`). Required when `repo.url` is set. |
| `repo.url` | no | Helm repo URL. If set, kflow runs `helm repo add` + `helm repo update` before upgrade. |
| `valuesFiles` | no | List of values files, relative to the resource file (`-f`). |
| `values` | no | Inline values dict rendered to `--set` flags. Supports nested keys and lists. Boolean values are serialized as `true`/`false`. |

- On **apply** and **reload**: `helm upgrade --install --create-namespace`.
- On **destroy**: `helm uninstall --ignore-not-found`.
- Under `--dry-run`: `helm upgrade --dry-run` is executed (a read-only render) so validation still runs.
- Helm releases are auto-targeted for `restart`/`reload` via the `app.kubernetes.io/instance=<release>` label unless the resource declares an explicit `selector:` or `workloads:`.

---

### `kustomize`

Apply a Kustomize overlay with `kubectl apply -k`.

```yaml
- name: install
  kustomize:
    path: overlays/production
```

| Field | Required | Description |
| --- | --- | --- |
| `path` | yes | Path to the kustomize directory, relative to the resource file. |

- On **apply** and **reload**: `kubectl apply -k <path>`.
- On **destroy**: `kubectl delete -k <path> --ignore-not-found`.

---

### `wait`

Wait for a Kubernetes resource to reach a condition or a JSONPath expression.

```yaml
- name: wait-ready
  dependsOn: [install]
  wait:
    for: deployment/longhorn-driver-deployer
    condition: available
    namespace: longhorn-system   # optional; defaults to resource namespace
    timeout: 180
```

```yaml
- name: wait-custom
  wait:
    for: pod/myapp-0
    jsonpath: '{.status.phase}'   # kubectl >= 1.23
    timeout: 60
```

| Field | Required | Description |
| --- | --- | --- |
| `for` | yes | Resource reference, e.g. `deployment/name`, `pod/name`. |
| `condition` | one of | Condition name for `--for=condition=<X>` (e.g. `available`, `ready`, `complete`). |
| `jsonpath` | one of | JSONPath expression for `--for=jsonpath='{...}'` (kubectl ≥ 1.23). |
| `namespace` | no | Namespace. Defaults to the resource namespace. |
| `timeout` | no | Timeout in seconds. Default: `120`. |

Exactly one of `condition` or `jsonpath` is required. On **destroy** and **restart**, wait steps are skipped (there is nothing to undo).

---

### `rolloutWait`

Wait for every rollout of the given workload kinds in a namespace to complete. Runs `kubectl rollout status` on each matching resource, in the same style as the classic `wait_for_rollouts` bash helper.

```yaml
- name: wait-all-rollouts
  rolloutWait: {}              # use all defaults
```

```yaml
- name: wait-app-rollouts
  dependsOn: [deploy]
  rolloutWait:
    kinds: [deployment, statefulset]
    selector: app=myapp          # optional; filter by label selector
    namespace: my-ns             # optional; defaults to resource namespace
    timeout: 120
```

| Field | Required | Description |
| --- | --- | --- |
| `kinds` | no | List of workload kinds to enumerate. Default: `[deployment, statefulset, daemonset]`. |
| `selector` | no | Label selector to filter which workloads are waited on (passed as `-l`). Omit to wait on all workloads of the given kinds in the namespace. |
| `namespace` | no | Namespace to list workloads from. Defaults to the resource namespace. |
| `timeout` | no | Per-rollout timeout in seconds. Default: `300`. |

Unlike `wait`, which targets a single named resource, `rolloutWait` **discovers** all workloads of the requested kinds in the namespace and waits on each in turn. If no resources of a given kind are found it is silently skipped. On **destroy**, rollout-wait steps are no-ops.

`rolloutWait: {}` (an empty mapping) is valid and uses all defaults.

---

### `script`

Run a shell command at apply time.

```yaml
- name: build
  script:
    run: make build
    onDestroy: make clean
    onReload: make build
    workdir: ../app             # optional; defaults to resource file directory
```

| Field | Required | Description |
| --- | --- | --- |
| `run` | yes | Shell command executed by `/bin/sh -c` on apply. |
| `onDestroy` | no | Command to run on destroy. Omit to skip destroy for this step. |
| `onReload` | no | Command to run on reload. Omit to re-run `run`. |
| `workdir` | no | Working directory for the command. Defaults to the resource definition file's directory. |

Under `--dry-run`, the command is printed but not executed.

---

### `runner`

Invoke a custom Python `BaseRunner` subclass.

```yaml
- name: migrate
  dependsOn: [deploy]
  runner:
    class: DatabaseRunner
    file: runners/db_runner.py    # optional if registered globally
    config:
      database: appdb
      seed: true
```

| Field | Required | Description |
| --- | --- | --- |
| `class` | yes | Registry name of the runner class. This is the `name` class attribute, or the class name if `name` is not set. |
| `file` | no | Path to the Python file defining the class, relative to the resource file. Optional when the file is registered globally under `runners:` in the root config. |
| `config` | no | Arbitrary dict passed to the runner's `__init__` and available as `self.config` / `ctx.config`. |

See [writing-runners.md](writing-runners.md) for the full runner authoring guide.

---

### `secret`

Declaratively create or update a Kubernetes `Secret` of type `Opaque`.

```yaml
- name: db-credentials
  secret:
    name: db-secret              # optional; defaults to step name
    namespace: demo              # optional; defaults to resource namespace
    literals:
      DB_HOST: postgres.default.svc.cluster.local
    fromEnv:
      DB_PASSWORD: DATABASE_PASSWORD   # key: ENV_VAR_NAME
    fromFiles:
      - certs/tls.crt
      - tls.key=certs/tls.key          # key=path form
    fromEnvFile: secrets.env
    fromCommand:
      TOKEN: "openssl rand -base64 128"   # key: shell command; stdout becomes the value
    ifNotExists: false           # skip if the secret already exists
```

| Field | Required | Description |
| --- | --- | --- |
| `name` | no | Secret name. Defaults to the step name. |
| `namespace` | no | Namespace. Defaults to the resource namespace. |
| `literals` | no | Dict of `key: value` pairs (`--from-literal`). |
| `fromEnv` | no | Dict of `key: ENV_VAR_NAME` - reads values from the environment at apply time. Missing variables raise an error. |
| `fromFiles` | no | List of file paths (`--from-file`). Use `key=path` to control the key name. Paths are relative to the resource file. |
| `fromEnvFile` | no | Path to a `.env`-style file (`--from-env-file`). Relative to the resource file. |
| `fromCommand` | no | Dict of `key: shell command`. Each command is run via `sh -c`; its trimmed stdout becomes the value. Non-zero exit raises an error. |
| `ifNotExists` | no | When `true`, skip creating/updating the secret if it already exists in the cluster. Default: `false`. |

Implemented as `kubectl create secret generic --dry-run=client -o yaml | kubectl apply -f -` so it is idempotent and update-safe. On **destroy**, the secret is deleted unless `ifNotExists: true`.

`fromCommand` is evaluated at apply time on the machine running kflow. Combine it with `ifNotExists: true` when you want a secret generated once and never rotated automatically.

---

### `configmap`

Declaratively create or update a Kubernetes `ConfigMap`.

```yaml
- name: app-config
  configmap:
    name: app-config             # optional; defaults to step name
    namespace: demo              # optional; defaults to resource namespace
    literals:
      LOG_LEVEL: debug
      REPLICA_COUNT: "3"
    fromFiles:
      - config/app.properties
      - app.json=config/app.json   # key=path form
    fromDir: config/             # load an entire directory as --from-file
    fromCommand:
      GIT_SHA: "git rev-parse --short HEAD"   # key: shell command; stdout becomes the value
    ifNotExists: false
```

| Field | Required | Description |
| --- | --- | --- |
| `name` | no | ConfigMap name. Defaults to the step name. |
| `namespace` | no | Namespace. Defaults to the resource namespace. |
| `literals` | no | Dict of `key: value` pairs (`--from-literal`). |
| `fromFiles` | no | List of file paths (`--from-file`). Use `key=path` to control the key name. Paths are relative to the resource file. |
| `fromDir` | no | Path to a directory; all files inside are added as keys (`--from-file=<dir>`). Relative to the resource file. |
| `fromCommand` | no | Dict of `key: shell command`. Each command is run via `sh -c`; its trimmed stdout becomes the value. Non-zero exit raises an error. |
| `ifNotExists` | no | When `true`, skip if the ConfigMap already exists. Default: `false`. |

---

### `exec`

Run a command inside a running pod.

```yaml
- name: init-schema
  dependsOn: [wait-ready]
  exec:
    command: [psql, -f, /sql/schema.sql]
    selector: app=postgres       # pick the first Running pod matching this selector
    container: postgres          # optional; required when the pod has multiple containers
    onDestroy: [psql, -c, "DROP SCHEMA public CASCADE"]
    onReload: [psql, -f, /sql/migrate.sql]
```

```yaml
- name: inspect
  exec:
    command: [sh, -c, "echo hello"]
    pod: myapp-0                 # target a specific pod by name instead of selector
```

| Field | Required | Description |
| --- | --- | --- |
| `command` | yes | Command and arguments as a list, or a shell string (wrapped as `sh -c <string>`). |
| `pod` | one of | Exact pod name. |
| `selector` | one of | Label selector; the first Running pod is chosen. |
| `container` | no | Container name. Required when the pod has multiple containers. |
| `onDestroy` | no | Command to run on destroy. Omit to skip destroy for this step. |
| `onReload` | no | Command to run on reload. Omit to re-run `command`. |

Exactly one of `pod` or `selector` is required. A non-zero exit code from the command raises an error and halts the operation.

---

### `dockerBuild`

Build (and optionally push) a Docker image.

```yaml
- name: build-api
  dockerBuild:
    context: ../api
    tag: registry.example.com/myapp/api:latest
    file: ../api/Dockerfile.prod   # optional; defaults to <context>/Dockerfile
    buildArgs:
      VERSION: "1.2.3"
      ENV: production
    push: true
    platform: linux/amd64,linux/arm64
    target: production             # multi-stage build target
```

| Field | Required | Description |
| --- | --- | --- |
| `context` | yes | Docker build context path, relative to the resource file. |
| `tag` | yes | Image tag (`-t`). |
| `file` | no | Path to the Dockerfile (`-f`). Defaults to `<context>/Dockerfile`. Relative to the resource file. |
| `buildArgs` | no | Dict of `--build-arg KEY=VALUE` pairs. |
| `push` | no | When `true`, run `docker push <tag>` after a successful build. Default: `false`. |
| `platform` | no | Target platform(s), e.g. `linux/amd64` or `linux/amd64,linux/arm64`. |
| `target` | no | Multi-stage build target (`--target`). |

On **destroy**, docker images are not removed. Under `--dry-run`, the `docker build` command is printed but not executed.

---

## Cross-cutting step fields

Every step, regardless of type, supports:

| Field | Required | Description |
| --- | --- | --- |
| `name` | yes | Unique name within the resource. Used in `dependsOn` references. |
| `dependsOn` | no | List of step/resource names this step must wait for. See [Dependency references](#dependency-references). |

---

## Path resolution

All file paths in resource definitions (manifests, valuesFiles, runner files, script workdir, etc.) are resolved **relative to the resource definition file that declares them**, not relative to the root config or the working directory. This means resource files can live in subdirectories and still use relative paths naturally.

Absolute paths and `~`-prefixed paths are used as-is.
