# kflow

![ci](https://github.com/1vers1on/kflow/actions/workflows/ci.yml/badge.svg)

Declarative Kubernetes workflow orchestration. kflow replaces the pile of
one-off `apply.sh` / `destroy.sh` / `restart.sh` / `reload.sh` scripts with a
single installable tool that manages the full lifecycle of your cluster
resources - with dependency awareness, phase ordering, state tracking, and
extensibility through custom Python runners.

It shells out to your existing `kubectl` and `helm` and honours your active
kubeconfig context, so there's no new cluster access model to learn.

```bash
pip install kflow

kflow -c examples/kflow.yaml graph        # see the plan
kflow -c examples/kflow.yaml --dry-run apply
kflow -c examples/kflow.yaml apply        # bring everything up, in order
```

---

## Concepts

### Two kinds of YAML

kflow works with two file types and tells them apart by a top-level `kflow:`
block. Anything without that block is treated as a plain Kubernetes manifest and
handed to `kubectl` unchanged.

```yaml
kflow:
  version: v1
  kind: Config            # the root config

# ...or...

kflow:
  version: v1
  kind: ResourceDefinition   # a resource definition
```

### Root config

kflow never auto-discovers files. It starts from one root config (default
`./kflow.yaml`, override with `-c`) and follows the paths declared there.

```yaml
kflow:
  version: v1
  kind: Config

state:
  dir: ~/.kflow            # where local state lives (per kube-context)

# context: my-cluster      # optional; defaults to the active kubeconfig context

runners:                   # custom runner files, registered globally
  - runners/db_runner.py

phases:                    # strict, ordered sequence (see below)
  - name: storage
  - name: ingress-controller
  - name: ingress
  - name: apps

resources:                 # files or directories of ResourceDefinitions
  - resources/longhorn-storage.yaml
  - resources/traefik.yaml
  - resources/longhorn-ingress.yaml
  - resources/app.yaml
```

### Resource definitions

```yaml
kflow:
  version: v1
  kind: ResourceDefinition

name: app
namespace: demo            # declared here, NOT baked into the manifests
phase: apps                # which phase this resource belongs to
description: Demo web app.

dependsOn:                 # resource-level deps (names or resource.step refs)
  - longhorn-ingress

selector: app=web          # label selector for restart / reload / logs
workloads:                 # explicit rollout targets (kind/name)
  - deployment/web

# Shorthand: any of these top-level fields becomes a step automatically.
manifests:
  - manifests/app-configmap.yaml
helm:
  release: myrelease
  chart: repo/chart
  version: 1.2.3
  repo: { name: repo, url: https://charts.example.com }
  valuesFiles: [../values/app.yaml]
  values: { replicaCount: 2 }     # rendered to `--set` flags
runners:
  - class: DatabaseRunner
    config: { database: appdb }

# ...or take full control with explicit, ordered steps that can declare
# fine-grained dependencies within and across resources/phases:
steps:
  - name: config
    manifests: [manifests/app-configmap.yaml]
  - name: deploy
    manifests: [manifests/app-deployment.yaml]
    dependsOn: [config, longhorn-ingress]   # step- and cross-resource deps
  - name: migrate
    dependsOn: [deploy]
    runner:
      class: DatabaseRunner
      config: { database: appdb, seed: true }
```

A resource is a sequence of **steps**. Each step is one of `manifests`, `helm`,
or `runner`. Steps run in declared order by default, and any step may add
`dependsOn` references:

* `stepName` - another step in the same resource
* `resourceName` - wait for that whole resource
* `resourceName.stepName` - wait for one specific step

### Phases solve the longhorn ↔ traefik problem

Phases are a **strict ordered sequence**: every step in phase *N* runs before
any step in phase *N+1*. Within a phase, ordering is computed from
`dependsOn`.

Longhorn wants storage up early, but its **ingress** needs Traefik, while
Traefik may want Longhorn storage - a cycle in naive tooling. kflow splits it
across phases:

```
phase storage             →  longhorn-storage (helm)
phase ingress-controller  →  traefik (helm)            depends on longhorn-storage
phase ingress             →  longhorn-ingress (manifest) depends on traefik
phase apps                →  app                          depends on longhorn-ingress
```

kflow does **not** error on circular `dependsOn`. Backward cross-phase
dependencies are satisfied by phase order; forward ones are reported and
ignored; genuine same-phase cycles are broken deterministically with a warning.

---

## Commands

| Command | What it does |
| --- | --- |
| `apply [names...]` | Apply manifests + helm in dependency order, creating namespaces as needed. |
| `destroy [names...]` | Tear down in reverse order. `--delete-namespaces` to remove namespaces too. |
| `restart [names...]` | `kubectl rollout restart` of a resource's workloads - no config changes. |
| `reload [names...]` | Re-apply manifests/helm/config non-destructively, then restart affected pods so they pick it up. |
| `helm [names...]` | Run `helm upgrade --install` for helm-backed resources. |
| `status [names...]` | kflow state + live workload readiness + helm status + manifest drift. |
| `health [names...]` | Health check; exits non-zero if anything is unhealthy. |
| `logs <name>` | Tail/fetch logs (`-f`, `--tail`, `--since`, `-c`, `--selector`). |
| `graph` | Render the dependency tree (`--format tree\|order\|dot`). |
| `plan [names...]` | Show the resolved execution order for a selection. |
| `list` | List phases and resources. |
| `validate` | Validate config and report warnings. |
| `runners` | List discovered custom runners. |
| `state show\|path\|clear` | Inspect or manage local state. |

### Global flags

* `-c, --config` - root config path (env `KFLOW_CONFIG`)
* `--dry-run` - print mutating commands without running them (reads still run)
* `--context` - kubeconfig context (env `KFLOW_CONTEXT`)
* `-v, --verbose` - show command output
* `-y, --yes` - skip confirmation prompts

### Targeting

Every operation works globally or on a subset. Names support glob patterns
(`longhorn-*`). By default a subset pulls in what it needs: `apply`/`reload`
include dependencies, `destroy` includes dependents. Use `--no-deps` to restrict
to exactly what you named (or `--with-deps` for `restart`).

---

## Operation semantics

* **apply** - ensure namespace → run each step (manifests via `kubectl apply -n
  <ns>`, helm via `upgrade --install`, runner `pre_apply`/`apply`/`post_apply`)
  → wait for rollouts (`--no-wait` to skip).
* **destroy** - reverse order; runner `*_destroy` hooks, `helm uninstall`,
  `kubectl delete`. Namespaces are kept unless `--delete-namespaces` (and never
  for `keepNamespace: true` resources or `default`).
* **restart** - `kubectl rollout restart` for `workloads`/`selector` (or the
  helm release's `app.kubernetes.io/instance` label), plus runner `restart`.
* **reload** - re-apply everything non-destructively (no delete/recreate), then
  rollout-restart so new ConfigMaps/Secrets are picked up; runner `reload`.
* **helm** - `helm upgrade --install` (kflow does not track values changes; it
  just runs the right command).

---

## Custom runners

Anything project-specific (create a database, seed data, run a migration) lives
in your own Python files, never in kflow. A runner subclasses `BaseRunner` from
the `kflow.runners` sub-library and overrides the hooks it needs:

```python
from kflow.runners import BaseRunner

class DatabaseRunner(BaseRunner):
    description = "Create and seed the app database."

    def apply(self, ctx):
        db = ctx.config.get("database", "appdb")
        ctx.log(f"ensuring database {db!r} exists")
        ctx.kubectl_exec(["sh", "-c", f"createdb {db} || true"],
                         selector="app=postgres")

    def reload(self, ctx):      # default reload == apply; override for migrations
        ctx.kubectl_exec(["sh", "-c", "run-migrations"], selector="app=postgres")

    def destroy(self, ctx):
        ctx.kubectl_exec(["sh", "-c", "dropdb appdb || true"],
                         selector="app=postgres")
```

Hooks: `pre_apply` / `apply` / `post_apply`, `pre_destroy` / `destroy` /
`post_destroy`, `restart`, `reload`, plus `status` and `health`. Each receives a
`RunnerContext` (`ctx`) exposing `ctx.kubectl(...)`, `ctx.helm(...)`,
`ctx.kubectl_exec(...)`, `ctx.apply_manifest(...)`, `ctx.namespace`,
`ctx.config`, `ctx.dry_run`, and helpers in `kflow.runners.helpers`
(`configmap_manifest`, `secret_manifest`, `b64`, `wait_for`). Mutating `ctx.*`
calls are automatically skipped under `--dry-run`.

Register runners globally (`runners:` in the root config) or per resource
(`file:` in a runner block / step). They're imported dynamically at runtime.

**See [docs/writing-runners.md](docs/writing-runners.md) for the full guide** -
every hook, the `RunnerContext` and `KubeClient` API, the dry-run rules, helpers,
and testing patterns.

---

## Documentation

| Doc | Contents |
| --- | --- |
| [docs/configuration.md](docs/configuration.md) | Complete YAML reference - every field in the root config, resource definitions, and all ten step types. |
| [docs/cli.md](docs/cli.md) | Full CLI reference - every command, every flag, targeting behaviour, exit codes. |
| [docs/writing-runners.md](docs/writing-runners.md) | Runner authoring guide - lifecycle hooks, `RunnerContext` API, dry-run rules, helpers, testing. |

---

## State

kflow keeps a small local JSON store under `state.dir`, keyed by kube-context:
what was applied, in which phase, when, and per-manifest hashes (used to flag
**drift** in `status`). Live truth - pod readiness, rollout status, helm release
status - is always queried fresh from the cluster, never trusted from state.

```bash
kflow state path     # where the file is
kflow state show     # dump it
kflow state clear    # reset for the active context
```

---

## Development

```bash
pip install -e '.[dev]'
pytest
```

The test suite fakes `subprocess.run`, so it runs without a cluster, `kubectl`,
or `helm` installed.
