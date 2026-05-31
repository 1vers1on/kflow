# Writing custom runners

Runners are how you put **project-specific behaviour** - create a database, seed
data, run a migration, render a Secret, wait for an external system - into a
kflow lifecycle, without that logic ever living in kflow itself.

A runner is a small Python class that subclasses `BaseRunner` and overrides the
lifecycle hooks it cares about. kflow imports your file at runtime, registers
the class, and calls its hooks at the right point in the dependency order.

> The `kflow.runners` package is the **stable, public API** for runner authors.
> Everything in this guide is imported from there. The kflow *core* engine
> (`kflow.core`) is internal - runners should not import from it.

- [Quick start](#quick-start)
- [Wiring a runner into a resource](#wiring-a-runner-into-a-resource)
- [The lifecycle hooks](#the-lifecycle-hooks)
- [The `RunnerContext`](#the-runnercontext)
- [Cluster primitives](#cluster-primitives)
- [Dry-run: the one rule that matters](#dry-run-the-one-rule-that-matters)
- [Helpers](#helpers)
- [Instance lifetime & state](#instance-lifetime--state)
- [Error handling](#error-handling)
- [Registration & discovery](#registration--discovery)
- [Testing runners](#testing-runners)
- [API reference](#api-reference)

---

## Quick start

```python
# runners/db_runner.py
from kflow.runners import BaseRunner


class DatabaseRunner(BaseRunner):
    """Create an app database and (optionally) seed/migrate it."""

    description = "Create and seed the app database."   # shown by `kflow runners`

    @property
    def _database(self) -> str:
        return self.config.get("database", "appdb")

    def apply(self, ctx):
        db = self._database
        ctx.log(f"ensuring database {db!r} exists")
        ctx.kubectl_exec(
            ["sh", "-c", f"createdb {db} 2>/dev/null || true"],
            selector="app=postgres",
        )

    def reload(self, ctx):
        # reload defaults to apply; override it to run migrations instead.
        ctx.log("running migrations")
        ctx.kubectl_exec(["sh", "-c", "run-migrations"], selector="app=postgres")

    def destroy(self, ctx):
        ctx.kubectl_exec(
            ["sh", "-c", f"dropdb {self._database} 2>/dev/null || true"],
            selector="app=postgres",
        )

    def health(self, ctx):
        res = ctx.kubectl(["get", "pods", "-l", "app=postgres",
                           "-n", ctx.namespace], check=False)
        return res.returncode == 0
```

Register it globally in the root config and attach it to a resource:

```yaml
# kflow.yaml (root config)
runners:
  - runners/db_runner.py
```

```yaml
# resources/app.yaml
steps:
  - name: migrate
    dependsOn: [deploy]
    runner:
      class: DatabaseRunner
      config:
        database: appdb
        seed: true
```

Now `kflow apply` runs your `apply` hook in dependency order, `kflow destroy`
runs `destroy` in reverse, `kflow reload` runs `reload`, and `kflow health`
folds your `health` result into the overall health check.

---

## Wiring a runner into a resource

A runner is attached as a **step**. Two equivalent forms:

**As an explicit step** (full control over ordering via `dependsOn`):

```yaml
steps:
  - name: migrate
    dependsOn: [deploy]            # run after the deploy step
    runner:
      class: DatabaseRunner        # required: the registry name (see below)
      file: runners/db_runner.py   # optional if registered globally
      config:                      # arbitrary dict, handed to your runner
        database: appdb
        seed: true
```

**As top-level shorthand** (`runners:` becomes one step per entry):

```yaml
runners:
  - name: migrate                  # optional; defaults to the class name
    class: DatabaseRunner
    dependsOn: [deploy]
    config: { database: appdb }
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `class` | yes | The runner's registry name (`name` attr, or the class name). |
| `file` | no | Path to the `.py` file, relative to the resource file. Optional when the file is registered globally under `runners:` in the root config. |
| `config` | no | A dict passed to your runner's constructor; available as `self.config` and `ctx.config`. |
| `name` | no (shorthand) | Step name. Defaults to the class name. |
| `dependsOn` | no | Step-level dependencies (other steps, resources, or `resource.step`). |

The runner **step** participates in ordering like any other step. So
`pre_apply`/`apply`/`post_apply` (see below) all run at the step's position in
the order - to run logic *before your manifests*, order the runner step before
the manifest step with `dependsOn`.

---

## The lifecycle hooks

Every hook receives a [`RunnerContext`](#the-runnercontext) and returns `None`
(except `health` and `status`). All hooks are **no-ops by default** - override
only what you need.

| Hook | Called by | When |
| --- | --- | --- |
| `pre_apply(ctx)` | `apply` | Just before `apply`, for this runner step. |
| `apply(ctx)` | `apply` | The main apply work. |
| `post_apply(ctx)` | `apply` | Just after `apply`, for this runner step. |
| `pre_destroy(ctx)` | `destroy` | Just before `destroy` (backup, drain…). |
| `destroy(ctx)` | `destroy` | The main teardown work. |
| `post_destroy(ctx)` | `destroy` | Just after `destroy` (external cleanup…). |
| `restart(ctx)` | `restart` | Restart whatever the runner owns (no config change). |
| `reload(ctx)` | `reload` | Re-apply config non-destructively. **Defaults to `apply`.** |
| `health(ctx) -> bool` | `health` | Return `True` if external state is healthy. |
| `status(ctx) -> Optional[str]` | - | A short status string. *(See note.)* |

Ordering within one operation:

```
apply    :  pre_apply  →  apply    →  post_apply
destroy  :  pre_destroy →  destroy  →  post_destroy   (whole graph in reverse order)
reload   :  reload                                    (then kflow restarts workloads)
restart  :  restart
```

Notes:

- **`reload` defaults to `apply`.** A runner that "renders config and applies
  it" picks up changes on `kflow reload` with no extra code. Override `reload`
  when reloading means something different from applying (e.g. run migrations
  rather than recreate).
- **`destroy` runs in reverse dependency order**, across the whole selection -
  the mirror of `apply`.
- **`status(ctx)` is currently not invoked by any CLI command.** The hook exists
  for forward compatibility; use `health` for anything that needs to gate the
  `kflow health` exit code today.
- Hooks for a given operation run on **one runner instance** - see
  [Instance lifetime & state](#instance-lifetime--state).

---

## The `RunnerContext`

A fresh `ctx` is built for every hook invocation. It carries the resource's
identity, your config, and the same cluster primitives the engine uses.

| Attribute | Type | Description |
| --- | --- | --- |
| `ctx.resource` | `str` | The resource name. |
| `ctx.namespace` | `str` | The resource's namespace (the default for `ctx`'s mutating helpers). |
| `ctx.config` | `dict` | Your runner's `config:` block (same object as `self.config`). |
| `ctx.operation` | `str` | The lifecycle op running now: `"apply"`, `"destroy"`, `"restart"`, `"reload"`, `"health"`. |
| `ctx.dry_run` | `bool` | Whether `--dry-run` is set. |
| `ctx.workdir` | `Path` | Directory of the resource definition file. |
| `ctx.state` | `dict` | **Read-only** snapshot of kflow's recorded state for this resource. |
| `ctx.extra` | `dict` | Resource metadata: `{"phase": ..., "selector": ...}`. |
| `ctx.kube` | `KubeClient` | The low-level kubectl/helm client (escape hatch). |
| `ctx.console` | `rich.Console` | For direct output (prefer `ctx.log`). |

### Convenience methods

```python
ctx.log("message")                 # styled, prefixed line (default cyan)
ctx.warn("careful")                # yellow log line

ctx.kubectl(["get", "pods"])       # -> CommandResult   (read by default!)
ctx.helm(["status", "rel"])        # -> CommandResult
ctx.apply_manifest(yaml_text)      # kubectl apply -f -  (mutating, dry-run aware)
ctx.rollout_restart("deployment", "web")        # mutating, dry-run aware
ctx.kubectl_exec(["psql", "-c", "..."], selector="app=postgres")  # exec in a pod

ctx.path("schema.sql")             # resolve a path relative to the resource file
```

`ctx.kubectl_exec(command, *, selector=None, pod=None, container=None)` execs in
the first **Running** pod matching `selector` (or an explicit `pod`).

---

## Cluster primitives

For anything the convenience methods don't cover, reach through `ctx.kube`
(a `KubeClient`). The most useful read methods (all degrade gracefully - empty
result instead of raising - when the cluster is unreachable):

```python
ctx.kube.get_workloads(ns, selector)   # [{kind, name, ready, desired, ok}, ...]
ctx.kube.get_pods(ns, selector)        # [{name, phase, ready}, ...]
ctx.kube.get_json(["get", "svc", "x"]) # parsed `-o json`, or {} on failure
ctx.kube.namespace_exists(ns)          # bool
ctx.kube.helm_status(release, ns)      # parsed `helm status -o json`, or {}
```

Mutating methods (honour `--dry-run` automatically):

```python
ctx.kube.apply_file(path, namespace=ns)
ctx.kube.apply_stdin(text, namespace=ns)
ctx.kube.delete_file(path, namespace=ns)
ctx.kube.ensure_namespace(ns)
ctx.kube.helm_upgrade(release, chart, ns, version=..., values_files=..., set_values=...)
ctx.kube.helm_uninstall(release, ns)
```

`ctx.kubectl(...)` / `ctx.helm(...)` accept: `mutating=False`, `check=True`,
`capture=True`, `input_text=None`, `timeout=None`. They return a
`CommandResult` with `.returncode`, `.stdout`, `.stderr`, `.ok`, `.skipped`.

---

## Dry-run: the one rule that matters

`--dry-run` skips **mutating** commands (echoes them instead of running them).
The catch:

> **`ctx.kubectl(...)` and `ctx.helm(...)` default to `mutating=False`** -
> they are treated as *reads* and **execute even under `--dry-run`.**

So this **runs for real during a dry-run** (almost certainly a bug):

```python
ctx.kubectl(["apply", "-f", "x.yaml"])          # ❌ mutates, but not skipped
```

Do one of these instead:

```python
ctx.apply_manifest(open("x.yaml").read())       # ✅ helper, dry-run aware
ctx.kube.apply_file("x.yaml", namespace=ctx.namespace)   # ✅ dry-run aware
ctx.kubectl(["apply", "-f", "x.yaml"], mutating=True)    # ✅ explicit
```

Rule of thumb: **use the helpers** (`apply_manifest`, `rollout_restart`,
`kubectl_exec`, and the `ctx.kube.*` mutating methods) for anything that
changes the cluster - they all pass `mutating=True` for you. Only drop to a raw
`ctx.kubectl(...)` for reads, or pass `mutating=True` explicitly. You can always
branch on `ctx.dry_run` for non-cluster side effects (writing a file, calling an
external API).

---

## Helpers

`kflow.runners.helpers` covers the most common chores:

```python
from kflow.runners.helpers import (
    b64, configmap_manifest, secret_manifest, wait_for,
)

cm = configmap_manifest("app-config", ctx.namespace, {"LOG_LEVEL": "debug"})
ctx.apply_manifest(cm)

sec = secret_manifest("app-secret", ctx.namespace, {"PASSWORD": "s3cret"})
ctx.apply_manifest(sec)                          # stringData by default

# Poll until a predicate holds (or time out). Returns True/False.
ok = wait_for(
    lambda: all(p["ready"] for p in ctx.kube.get_pods(ctx.namespace, "app=db")),
    timeout=120, interval=3, description="db pods ready",
)
```

`secret_manifest(..., string_data=False)` base64-encodes into `data` instead of
`stringData`. `b64(value)` encodes a single string. Both manifest helpers add an
`app.kubernetes.io/managed-by: kflow` label (merge your own via `labels=`).

> `wait_for` sleeps in real time; it does **not** short-circuit under
> `--dry-run`. Guard it with `if not ctx.dry_run:` if a dry-run shouldn't block.

---

## Instance lifetime & state

kflow constructs a **fresh runner instance per operation invocation**, passing
your `config:` block to `__init__`:

- During `apply`, one instance handles `pre_apply` → `apply` → `post_apply`, so
  you may stash intermediate values on `self` *within that operation*.
- `destroy`, `restart`, `reload`, and `health` each get their own fresh instance.

**Do not** rely on `self` carrying state across operations (e.g. from `apply` to
a later `destroy`) or across separate steps - it won't. Persist anything durable
in the cluster, or read it back via `ctx.kube` / `ctx.state`.

`ctx.state` is a read-only snapshot of what kflow recorded for this resource
(phase, last operation, timestamps, per-step info). Treat it as a hint, not the
source of truth - live facts should always be queried fresh from the cluster.

---

## Error handling

- A checked command that fails raises `CommandError`. `ctx.kubectl(...)` /
  `ctx.helm(...)` use `check=True` by default, so a non-zero exit raises unless
  you pass `check=False`. kflow catches `CommandError` and prints a clean,
  single-line error.
- Raise `kflow.runners.CommandError` (or let a checked call raise it) to fail an
  operation cleanly. Any *other* uncaught exception surfaces as a full
  traceback - fine while developing, noisy in production.
- For "best effort" reads (health checks, polling), pass `check=False` and
  inspect `result.returncode` / `result.ok` yourself, as the example
  `health` hook does.

```python
from kflow.runners import CommandError

def apply(self, ctx):
    res = ctx.kubectl(["get", "secret", "tls"], check=False)
    if not res.ok:
        raise CommandError(res.cmd, res.returncode, res.stdout,
                           "TLS secret missing; create it before applying")
```

---

## Registration & discovery

- kflow imports each runner **file by absolute path** and registers every
  `BaseRunner` subclass it defines.
- A class registers under its **registry name**: the `name` class attribute if
  set, otherwise the class's `__name__`. Registry names must be **unique across
  the whole project** - a clash raises `RunnerLoadError`.
- Register files **globally** (`runners:` in the root config) so any resource
  can reference the class by name, or **per resource** via `file:` in the runner
  block/step.
- Two different files may each define a class literally named `Runner` without a
  module clash, but their *registry names* still must differ (set `name`).
- `kflow runners` lists every discovered runner, its `description`, and source
  file.

```python
class MigrationRunner(BaseRunner):
    name = "db-migrate"          # registry name; reference `class: db-migrate`
    description = "Apply Alembic migrations."
```

---

## Testing runners

Runners are plain classes - unit-test them with a fake context. kflow's own
suite fakes `subprocess.run` (see `tests/conftest.py`) so nothing touches a real
cluster. A lightweight pattern:

```python
import json
from types import SimpleNamespace
from kflow.runners import RunnerContext, KubeClient

# kubectl_exec with a selector first lists pods, so the fake must return one.
PODS = {"items": [{"metadata": {"name": "postgres-0"},
                   "status": {"phase": "Running"}}]}

def test_apply_creates_db(monkeypatch):
    ran = []
    def fake_run(cmd, **kw):
        ran.append([str(c) for c in cmd])
        out = json.dumps(PODS) if "json" in [str(c) for c in cmd] else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr("kflow.runners.shell.subprocess.run", fake_run)

    ctx = RunnerContext(resource="app", namespace="demo",
                        config={"database": "appdb"}, kube=KubeClient())
    DatabaseRunner({"database": "appdb"}).apply(ctx)
    assert any("createdb appdb" in " ".join(c) for c in ran)
```

---

## API reference

Imported from `kflow.runners`:

| Name | Kind | Purpose |
| --- | --- | --- |
| `BaseRunner` | class | Subclass this; override hooks. |
| `RunnerContext` | dataclass | Passed to every hook. |
| `KubeClient` | class | kubectl/helm wrapper (`ctx.kube`). |
| `CommandResult` | dataclass | Outcome of a command (`.returncode`, `.stdout`, `.stderr`, `.ok`, `.skipped`). |
| `CommandError` | exception | Raised on a failed checked command. |
| `run_command` | function | Low-level subprocess runner. |
| `format_command` | function | Render a command list as a shell string. |
| `helpers` | module | `b64`, `configmap_manifest`, `secret_manifest`, `wait_for`. |

`BaseRunner` hooks: `pre_apply`, `apply`, `post_apply`, `pre_destroy`,
`destroy`, `post_destroy`, `restart`, `reload`, `health`, `status`.
Class attributes: `name`, `description`.
