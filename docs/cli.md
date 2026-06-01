# kflow CLI Reference

Complete reference for the `kflow` command-line interface.

- [Global flags](#global-flags)
- [Targeting](#targeting)
- [Lifecycle commands](#lifecycle-commands)
  - [apply](#apply)
  - [destroy](#destroy)
  - [restart](#restart)
  - [reload](#reload)
  - [helm](#helm)
- [Inspection commands](#inspection-commands)
  - [status](#status)
  - [health](#health)
  - [logs](#logs)
  - [graph](#graph)
  - [plan](#plan)
  - [list](#list)
  - [validate](#validate)
  - [runners](#runners)
- [State commands](#state-commands)
  - [state show](#state-show)
  - [state path](#state-path)
  - [state clear](#state-clear)

---

## Global flags

These flags apply to every subcommand and must be placed **before** the subcommand name.

```
kflow [GLOBAL FLAGS] <command> [COMMAND FLAGS] [ARGS]
```

| Flag | Env var | Default | Description |
| --- | --- | --- | --- |
| `-c, --config PATH` | `KFLOW_CONFIG` | `kflow.yaml` | Path to the root config file. Resolved relative to the working directory. |
| `--dry-run` | - | off | Print mutating commands (kubectl apply, helm upgrade, etc.) without executing them. Read commands (get, status, wait) still run so validation output is meaningful. |
| `--context NAME` | `KFLOW_CONTEXT` | (active context) | kubeconfig context to use. Overrides `context:` in the root config. |
| `-v, --verbose` | - | off | Show the full stdout/stderr output of kubectl and helm calls. |
| `-y, --yes` | - | off | Skip interactive confirmation prompts (same effect as `--dry-run` for prompts). |
| `--version` | - | - | Print the kflow version and exit. |
| `-h, --help` | - | - | Show help for any command. |

---

## Targeting

Most commands accept an optional list of resource names:

```
kflow apply                     # operate on all resources
kflow apply traefik app         # operate on specific resources
kflow apply "longhorn-*"        # glob patterns are supported
```

**Dependency inclusion** differs by command:

| Command | Default behaviour | Override |
| --- | --- | --- |
| `apply` | Includes dependencies (what the targets need) | `--no-deps` |
| `destroy` | Includes dependents (what needs the targets) | `--no-deps` |
| `reload` | Includes dependencies | `--no-deps` |
| `helm` | Includes dependencies | `--no-deps` |
| `restart` | Targets only | `--with-deps` |
| `status` | Targets only | - |
| `health` | Targets only | - |
| `plan` | Includes dependencies | `--no-deps` |

An unknown name (no matching resource, no glob match) is an error.

---

## Lifecycle commands

### `apply`

Apply manifests and Helm charts in dependency order. Creates namespaces as needed.

```
kflow apply [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--no-deps` | off | Do not pull in dependencies of the named resources. |
| `--no-wait` | off | Do not wait for rollouts to become ready after applying. |
| `--timeout SECS` | `300` | Rollout wait timeout per workload, in seconds. |

**What it does, in order:**

1. Resolves the target set (adds dependencies unless `--no-deps`).
2. For each step in topological order:
   - If `autoCreateNamespace` is enabled (globally or per-resource), creates any missing namespace before the step runs.
   - Executes the step (manifest apply, helm upgrade, runner `pre_apply`→`apply`→`post_apply`, etc.).
3. After the last step of each resource, waits for its workloads to roll out (unless `--no-wait`).
4. Records apply state (phase, namespace, manifest hashes, timestamps) for each target.

Namespace auto-creation is **disabled by default**. Enable it with `autoCreateNamespace: true` in the root config (global) or in a resource definition (per-resource). See [configuration.md](configuration.md#autocreatenamespace).

**Examples**

```bash
kflow apply                          # apply everything
kflow apply traefik                  # apply traefik and its dependencies
kflow apply --dry-run app            # preview what would run
kflow apply --no-wait longhorn-storage
kflow apply --timeout 600 app        # give slow rollouts more time
```

---

### `destroy`

Tear down resources in reverse dependency order.

```
kflow destroy [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--no-deps` | off | Do not include dependents of the named resources. |
| `--delete-namespaces` | off | Also delete the resource namespaces after teardown. Skips `default` and resources with `keepNamespace: true`. |
| `--timeout SECS` | `300` | (reserved for future rollout waiting) |

Prompts for confirmation unless `--yes` or `--dry-run` is set.

**What it does:**

1. Resolves the target set (adds dependents - resources that depend on the targets - unless `--no-deps`).
2. Prompts: "Destroy N resource(s): …?"
3. Iterates steps in **reverse** topological order:
   - Manifest steps: `kubectl delete -f <path> --ignore-not-found`.
   - Helm steps: `helm uninstall --ignore-not-found`.
   - Script steps: runs `onDestroy:` if defined, skips otherwise.
   - Runner steps: `pre_destroy`→`destroy`→`post_destroy`.
   - Wait steps: skipped.
   - Secret/ConfigMap steps: deletes the resource unless `ifNotExists: true`.
   - Exec steps: runs `onDestroy:` if defined.
   - DockerBuild steps: skipped (images are not removed).
4. If `--delete-namespaces`, deletes each namespace (reversed order, skipping `default` and `keepNamespace: true`).
5. Records destroy state.

**Examples**

```bash
kflow destroy                               # tear down everything (confirms first)
kflow destroy app --yes                     # skip confirmation
kflow destroy --delete-namespaces           # also remove namespaces
kflow destroy --no-deps traefik            # only traefik, leave dependents alone
kflow --dry-run destroy app                 # preview what would be deleted
```

---

### `restart`

Trigger a `kubectl rollout restart` of a resource's workloads without applying any configuration changes.

```
kflow restart [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--with-deps` | off | Also restart resources that the targets depend on. |
| `--no-wait` | off | Do not wait for rollout to complete. |
| `--timeout SECS` | `300` | Rollout status wait timeout. |

**Workload targeting:**

1. If `workloads:` is declared on the resource, those exact `kind/name` entries are restarted.
2. Otherwise, if `selector:` is declared, kflow queries live workloads matching that selector.
3. Otherwise, if the resource has a Helm step, kflow uses `app.kubernetes.io/instance=<release>`.
4. If none of the above apply, a warning is printed and no restart occurs.

Runner steps' `restart` hook is also called for each runner step in the resource.

**Examples**

```bash
kflow restart app
kflow restart app --no-wait
kflow restart                       # restart all resources
```

---

### `reload`

Re-apply configuration non-destructively, then rollout-restart workloads so they pick up the new config (new ConfigMaps, Secrets, etc.).

```
kflow reload [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--no-deps` | off | Do not include dependencies. |
| `--no-wait` | off | Do not wait for rollouts after restarting. |
| `--timeout SECS` | `300` | Rollout wait timeout. |

**What it does:**

1. Re-applies every step using reload semantics (no deletes/recreates):
   - If `autoCreateNamespace` is enabled, creates any missing namespace before each step.
   - Manifest/kustomize steps: `kubectl apply` (same as apply).
   - Helm steps: `helm upgrade --install` (same as apply).
   - Script steps: runs `onReload:` if defined, otherwise re-runs `run:`.
   - Runner steps: calls the `reload` hook (which defaults to `apply` if not overridden).
   - Secret/ConfigMap steps: re-creates via `--dry-run=client | apply` (idempotent).
   - Exec steps: runs `onReload:` if defined, otherwise re-runs `command:`.
   - DockerBuild steps: re-runs the build.
2. After re-applying, restarts workloads (same targeting logic as `restart`).
3. Records apply + reload state.

**Examples**

```bash
kflow reload app                    # re-apply config and restart app pods
kflow reload --no-wait app
kflow reload                        # reload everything
```

---

### `helm`

Run `helm upgrade --install` for every Helm-backed step in the target set. Useful when you want to push only Helm changes without touching manifests or runners.

```
kflow helm [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--no-deps` | off | Do not include dependencies. |

Prints a warning if no Helm-backed resources are found in the selection.

**Examples**

```bash
kflow helm                          # upgrade all helm releases
kflow helm traefik longhorn-storage
```

---

## Inspection commands

### `status`

Show kflow's recorded state and live workload readiness for the target resources.

```
kflow status [NAMES...]
```

Output columns:

| Column | Description |
| --- | --- |
| `resource` | Resource name. |
| `phase` | Phase the resource belongs to. |
| `namespace` | Kubernetes namespace. |
| `state` | kflow's recorded status: `applied`, `destroyed`, or `unknown`. |
| `helm` | Helm release status (`deployed`, `failed`, etc.) queried live. Empty if no Helm step. |
| `ready` | `ready/total` workload replicas, queried live. `-` if no selector can be inferred. |
| `drift` | Number of manifest files whose on-disk content has changed since the last apply. |
| `last applied` | ISO timestamp of the last successful apply. |

Drift is detected by comparing SHA-256 hashes of manifest files to the hashes recorded at apply time. Remote URLs are excluded from drift detection. Non-zero drift is highlighted in yellow.

**Examples**

```bash
kflow status
kflow status app traefik
```

---

### `health`

Check workload health and runner health hooks; exits non-zero if anything is unhealthy.

```
kflow health [NAMES...]
```

Output columns:

| Column | Description |
| --- | --- |
| `resource` | Resource name. |
| `namespace` | Kubernetes namespace. |
| `health` | `healthy` (green), `unhealthy` (red), or `unknown` (dim). |
| `detail` | Summary of workload replica counts, e.g. `deployment/web 2/2, deployment/api 1/1`. |

A resource is `healthy` if all of its workloads have `ready >= desired` and all runner `health` hooks return `True`. A resource with no inferable selector and no runner health hook shows `unknown`. Exit code is 1 if any resource is unhealthy.

**Examples**

```bash
kflow health
kflow health app
```

---

### `logs`

Fetch or stream logs for a resource's pods.

```
kflow logs [OPTIONS] NAME
```

**Arguments**

| Arg | Description |
| --- | --- |
| `NAME` | Resource name (exactly one). |

**Options**

| Flag | Description |
| --- | --- |
| `-f, --follow` | Stream logs continuously (passes `-f` to kubectl). |
| `--tail N` | Show only the last N lines. |
| `--since DURATION` | Show logs newer than a duration, e.g. `10m`, `1h`. |
| `-c, --container NAME` | Container name. When omitted, shows all containers with `--all-containers=true`. |
| `--selector SELECTOR` | Override the label selector. Useful when the resource has no `selector:` declared. |
| `--previous` | Show logs from a previous (terminated) container instance. |

Pod targeting uses `selector:`, then `workloads:` (first entry), then the Helm release label. Raises an error if no selector can be determined - pass `--selector` to override.

**Examples**

```bash
kflow logs app
kflow logs app -f
kflow logs app --tail 100 --since 30m
kflow logs app -c api
kflow logs app --selector app=worker
```

---

### `graph`

Render the full dependency graph.

```
kflow graph [OPTIONS]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--format FORMAT` | `tree` | Output format: `tree`, `order`, or `dot`. |

- `tree` - Rich tree view grouped by phase, showing resources and their steps with dependency annotations.
- `order` - Table of every step in the computed execution order, with phase, resource, step name, kind, and `dependsOn`.
- `dot` - Graphviz DOT language output. Pipe to `dot -Tsvg` or paste into an online Graphviz renderer.

**Examples**

```bash
kflow graph
kflow graph --format order
kflow graph --format dot | dot -Tpng -o graph.png
```

---

### `plan`

Show the resolved execution order for a selection, without running anything.

```
kflow plan [OPTIONS] [NAMES...]
```

**Options**

| Flag | Default | Description |
| --- | --- | --- |
| `--no-deps` | off | Do not include dependencies of the named resources. |

Displays a numbered table: `#`, `phase`, `resource`, `step`, `kind`. Equivalent to `graph --format order` filtered to the target set.

**Examples**

```bash
kflow plan app                      # show what `kflow apply app` would touch
kflow plan --no-deps app
kflow plan                          # full plan for everything
```

---

### `list`

List all resources with their phase, namespace, steps, and dependencies.

```
kflow list
```

No arguments or options. Prints a table with columns: `resource`, `phase`, `namespace`, `steps`, `depends on`.

**Example**

```bash
kflow list
```

---

### `validate`

Validate the configuration and report warnings without connecting to a cluster.

```
kflow validate
```

Checks:

- Configuration parses without errors (schema, missing fields, duplicate names, unknown phase refs, bad dependency refs).
- All manifest file paths referenced in the config exist on disk (remote URLs are skipped).
- All kustomize paths exist.
- All dockerBuild contexts exist.
- Any dependency graph warnings (forward cross-phase deps, same-phase cycles).

Exits 0 if validation passes (warnings are printed but do not affect the exit code). Exits non-zero on configuration errors.

**Example**

```bash
kflow validate
kflow -c path/to/kflow.yaml validate
```

---

### `runners`

List all custom runners discovered from the configuration.

```
kflow runners
```

Prints a table with columns: `name` (registry name), `description`, `source` (file path). Useful for verifying that runner files were loaded correctly and that class names are what you expect.

**Example**

```bash
kflow runners
```

---

## State commands

kflow maintains a local JSON state file keyed by kube-context. It records what was applied, in which phase, per-manifest hashes (for drift detection), and operation timestamps. It does **not** record live cluster state - that is always queried fresh.

### `state show`

Print the full local state for the active cluster context as formatted JSON.

```
kflow state show
```

### `state path`

Print the path to the state file.

```
kflow state path
```

Useful for scripting or manual inspection:

```bash
cat "$(kflow state path)"
```

### `state clear`

Delete all recorded state for the active cluster context.

```
kflow state clear
```

Prompts for confirmation unless `--yes` is set. After clearing, all resources appear as `unknown` in `kflow status` until they are applied again. Does not affect the cluster itself.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Error (config error, command failure, unhealthy resources). |
| `130` | Aborted by the user (Ctrl-C). |
