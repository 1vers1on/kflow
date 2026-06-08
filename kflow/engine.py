"""Kflow orchestration engine."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from .graph import DependencyGraph
from .loader import load_root_config
from .models import (
    DockerBuildSpec,
    HelmSpec,
    KflowError,
    ResourceDef,
    RootConfig,
    ScriptSpec,
    StepDef,
)
from .state import StateManager
from .runners import KubeClient, RunnerContext, RunnerRegistry
from .runners.registry import RunnerLoadError
from .runners.shell import CommandError, run_command

_default_console = Console()


class Kflow:
    """The orchestration engine. One instance per loaded configuration."""

    def __init__(self, config: RootConfig, *, dry_run: bool = False,
                 context: Optional[str] = None, verbose: bool = False,
                 console_: Optional[Console] = None):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose
        self.console = console_ or _default_console
        self.context = context or config.context
        self.graph = DependencyGraph(config)
        self.kube = KubeClient(context=self.context, dry_run=dry_run,
                               console=self.console, verbose=verbose)
        cluster_key = self.context or "default"
        self.state = StateManager(config.state_dir, cluster_key)
        self.registry = RunnerRegistry(console=self.console)
        self._keyring = None  # lazily built KeyRing for encrypted manifests
        self._load_runners()

    @classmethod
    def load(cls, config_path, **kwargs) -> "Kflow":
        return cls(load_root_config(config_path), **kwargs)

    # -- namespace helpers ------------------------------------------------

    def _eff_ns(self, resource: ResourceDef, step: StepDef) -> Optional[str]:
        """Effective namespace for a step.

        Priority: step.no_namespace > step.namespace > resource.namespace.
        Returns None when the step is cluster-scoped (no_namespace=True).
        """
        if step.no_namespace:
            return None
        if step.namespace is not None:
            return step.namespace
        return resource.namespace

    def _should_create_ns(self, resource: ResourceDef) -> bool:
        """Whether to auto-create missing namespaces for this resource."""
        if resource.auto_create_namespace is not None:
            return resource.auto_create_namespace
        return self.config.auto_create_namespace

    # -- runner wiring ----------------------------------------------------

    def _load_runners(self) -> None:
        # Global runner files registered in the root config.
        for f in self.config.runner_files:
            self.registry.load_file(f)
        # Per-resource runner files.
        for res in self.config.resources:
            for step in res.runner_steps:
                if step.runner and step.runner.file:
                    self.registry.load_file(step.runner.file)

    def _runner_ctx(self, resource: ResourceDef, step: StepDef,
                    operation: str) -> RunnerContext:
        return RunnerContext(
            resource=resource.name,
            namespace=self._eff_ns(resource, step) or resource.namespace,
            config=step.runner.config if step.runner else {},
            kube=self.kube,
            console=self.console,
            dry_run=self.dry_run,
            operation=operation,
            workdir=(resource.source_file.parent if resource.source_file else Path.cwd()),
            state=self.state.get(resource.name) or {},
            extra={"phase": resource.phase_name, "selector": resource.selector},
        )

    # -- target selection -------------------------------------------------

    def resolve_targets(self, names, *, operation: str, with_deps: bool) -> List[str]:
        all_names = self.graph.resource_order
        if not names:
            return list(all_names)
        selected: set = set()
        known = set(self.config.resource_map)
        for pattern in names:
            matches = [n for n in known if n == pattern or fnmatch.fnmatch(n, pattern)]
            if not matches:
                raise KflowError(f"no resource matches {pattern!r}")
            selected.update(matches)
        if with_deps:
            selected = self.graph.closure(selected,
                                          dependents=(operation == "destroy"))
        return [n for n in all_names if n in selected]

    # -- banner -----------------------------------------------------------

    def _banner(self, op: str, targets: List[str]) -> None:
        mode = " [yellow](dry-run)[/yellow]" if self.dry_run else ""
        self.console.rule(f"[bold]{op}[/bold]{mode} [dim]{len(targets)} resource(s)[/dim]")

    # -- step execution ---------------------------------------------------

    def _step_header(self, resource: ResourceDef, step: StepDef, verb: str) -> None:
        self.console.print(
            f"  [bold]{verb}[/bold] [cyan]{resource.name}[/cyan]"
            f"[dim].{step.name}[/dim] [dim]({step.kind})[/dim]"
        )

    def _check_wait_result(self, result) -> None:
        if not result.skipped and result.returncode != 0:
            raise CommandError(result.cmd, result.returncode,
                               result.stdout, result.stderr)

    def _with_server_side(self, step: StepDef, fn):
        """Run fn() with kube.server_side elevated if the step requests it."""
        orig = self.kube.server_side
        if step.server_side:
            self.kube.server_side = True
        try:
            return fn()
        finally:
            self.kube.server_side = orig

    # -- encrypted manifests ---------------------------------------------

    def keyring(self):
        """Lazily build the :class:`KeyRing` from the environment and ``.env``
        files next to the config and in the working directory."""
        if self._keyring is None:
            from .crypto import KeyRing
            search: List[Path] = []
            if self.config.path:
                search.append(Path(self.config.path).parent)
            search.append(Path.cwd())
            self._keyring = KeyRing.from_environment(search)
        return self._keyring

    def _decrypt_manifest(self, step: StepDef, manifest) -> str:
        """Read an encrypted manifest file and return its decrypted YAML text."""
        from .crypto import EncryptionError, Envelope
        from .loader import _is_url
        if _is_url(str(manifest)):
            raise KflowError(
                f"step {step.name!r}: encrypted manifests must be local files, "
                f"not URLs ({manifest})"
            )
        path = Path(manifest)
        try:
            envelope_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise KflowError(f"cannot read encrypted manifest {path}: {exc}") from exc
        ring = self.keyring()
        try:
            if step.encryption_key_id:
                # Honour the explicit override even if the envelope names another kid.
                from .crypto import _decrypt_token
                key = ring.require(step.encryption_key_id)
                env = Envelope.loads(envelope_text)
                data = _decrypt_token(env.token, key)
            else:
                data = ring.decrypt(envelope_text)
        except EncryptionError as exc:
            raise KflowError(
                f"step {step.name!r}: failed to decrypt {path.name}: {exc}"
            ) from exc
        return data.decode("utf-8")

    def _apply_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "apply")
        ns = self._eff_ns(resource, step)
        if step.kind == "manifest":
            for m in step.manifests:
                if step.encrypted:
                    self.kube.apply_stdin(self._decrypt_manifest(step, m), namespace=ns)
                else:
                    self.kube.apply_file(m, namespace=ns)
        elif step.kind == "helm" and step.helm:
            self._helm_upgrade(step.helm, resource)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.apply_kustomize(step.kustomize.path)
        elif step.kind == "wait" and step.wait:
            wait_ns = step.wait.namespace or ns
            result = self.kube.wait_for(
                step.wait.for_resource,
                step.wait.condition,
                namespace=wait_ns,
                timeout=step.wait.timeout,
                jsonpath=step.wait.jsonpath,
            )
            self._check_wait_result(result)
        elif step.kind == "rollout-wait" and step.rollout_wait:
            spec = step.rollout_wait
            rollout_ns = spec.namespace or ns or resource.namespace
            self.kube.rollout_wait_all(rollout_ns, kinds=spec.kinds, selector=spec.selector,
                                       timeout=spec.timeout)
        elif step.kind == "script" and step.script:
            self._run_script(resource, step.script, step.script.run)
        elif step.kind == "runner" and step.runner:
            runner = self.registry.instantiate(step.runner.class_name, step.runner.config)
            ctx = self._runner_ctx(resource, step, "apply")
            runner.pre_apply(ctx)
            runner.apply(ctx)
            runner.post_apply(ctx)
        elif step.kind == "secret" and step.secret:
            self._apply_secret(resource, step)
        elif step.kind == "configmap" and step.configmap:
            self._apply_configmap(resource, step)
        elif step.kind == "exec" and step.exec_spec:
            self._exec_step(resource, step, step.exec_spec.command)
        elif step.kind == "docker-build" and step.docker_build:
            self._run_docker_build(resource, step.docker_build)
        elif step.kind == "create-namespace" and step.namespace_spec:
            self._apply_namespace_step(resource, step)

    def _destroy_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "destroy")
        ns = self._eff_ns(resource, step)
        if step.kind == "manifest":
            for m in reversed(step.manifests):
                if step.encrypted:
                    self.kube.delete_stdin(self._decrypt_manifest(step, m), namespace=ns)
                else:
                    self.kube.delete_file(m, namespace=ns)
        elif step.kind == "helm" and step.helm:
            self.kube.helm_uninstall(step.helm.release, step.helm.namespace)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.delete_kustomize(step.kustomize.path)
        elif step.kind == "wait":
            pass  # nothing to undo for a wait step
        elif step.kind == "rollout-wait":
            pass  # nothing to undo for a rollout-wait step
        elif step.kind == "script" and step.script and step.script.on_destroy:
            self._run_script(resource, step.script, step.script.on_destroy)
        elif step.kind == "runner" and step.runner:
            runner = self.registry.instantiate(step.runner.class_name, step.runner.config)
            ctx = self._runner_ctx(resource, step, "destroy")
            runner.pre_destroy(ctx)
            runner.destroy(ctx)
            runner.post_destroy(ctx)
        elif step.kind == "secret" and step.secret:
            if not step.secret.if_not_exists:
                name = step.secret.name or step.name
                secret_ns = step.secret.namespace or ns or resource.namespace
                self.kube.secret_delete(name, secret_ns)
        elif step.kind == "configmap" and step.configmap:
            if not step.configmap.if_not_exists:
                name = step.configmap.name or step.name
                cm_ns = step.configmap.namespace or ns or resource.namespace
                self.kube.configmap_delete(name, cm_ns)
        elif step.kind == "exec" and step.exec_spec:
            if step.exec_spec.on_destroy is not None:
                self._exec_step(resource, step, step.exec_spec.on_destroy)
        elif step.kind == "docker-build":
            pass  # docker images are not removed on destroy
        elif step.kind == "create-namespace" and step.namespace_spec:
            spec = step.namespace_spec
            if spec.delete_on_destroy:
                ns_name = spec.name or self._eff_ns(resource, step) or resource.namespace
                self.console.print(f"  [dim]deleting namespace {ns_name}[/dim]")
                self.kube.delete_namespace(ns_name)

    def _reload_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "reload")
        ns = self._eff_ns(resource, step)
        if step.kind == "manifest":
            for m in step.manifests:
                if step.encrypted:
                    self.kube.apply_stdin(self._decrypt_manifest(step, m), namespace=ns)
                else:
                    self.kube.apply_file(m, namespace=ns)
        elif step.kind == "helm" and step.helm:
            self._helm_upgrade(step.helm, resource)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.apply_kustomize(step.kustomize.path)
        elif step.kind == "wait" and step.wait:
            wait_ns = step.wait.namespace or ns
            result = self.kube.wait_for(
                step.wait.for_resource,
                step.wait.condition,
                namespace=wait_ns,
                timeout=step.wait.timeout,
                jsonpath=step.wait.jsonpath,
            )
            self._check_wait_result(result)
        elif step.kind == "rollout-wait" and step.rollout_wait:
            spec = step.rollout_wait
            rollout_ns = spec.namespace or ns or resource.namespace
            self.kube.rollout_wait_all(rollout_ns, kinds=spec.kinds, selector=spec.selector,
                                       timeout=spec.timeout)
        elif step.kind == "script" and step.script:
            cmd = step.script.on_reload if step.script.on_reload is not None else step.script.run
            self._run_script(resource, step.script, cmd)
        elif step.kind == "runner" and step.runner:
            runner = self.registry.instantiate(step.runner.class_name, step.runner.config)
            ctx = self._runner_ctx(resource, step, "reload")
            runner.reload(ctx)
        elif step.kind == "secret" and step.secret:
            self._apply_secret(resource, step)
        elif step.kind == "configmap" and step.configmap:
            self._apply_configmap(resource, step)
        elif step.kind == "exec" and step.exec_spec:
            spec = step.exec_spec
            cmd = spec.on_reload if spec.on_reload is not None else spec.command
            self._exec_step(resource, step, cmd)
        elif step.kind == "docker-build" and step.docker_build:
            if step.docker_build.on_reload != "skip":
                self._run_docker_build(resource, step.docker_build)
            else:
                self.console.print(
                    f"  [dim]skipping docker build on reload (onReload: skip)[/dim]"
                )
        elif step.kind == "create-namespace" and step.namespace_spec:
            self._apply_namespace_step(resource, step)

    def _run_script(self, resource: ResourceDef, script: ScriptSpec, cmd: str) -> None:
        if self.dry_run:
            self.console.print(f"  [dim](dry-run) would run: {cmd!r}[/dim]")
            return
        workdir = script.workdir or (
            resource.source_file.parent if resource.source_file else Path.cwd()
        )
        run_command(["sh", "-c", cmd], check=True, capture=not self.verbose,
                    cwd=str(workdir))

    def _run_command_literals(self, commands: dict, step_name: str) -> dict:
        """Run each shell command and return a dict of key → stripped stdout."""
        out = {}
        for key, cmd in commands.items():
            if self.dry_run:
                self.console.print(
                    f"  [dim](dry-run) would run for {key!r}: {cmd!r}[/dim]"
                )
                out[key] = ""
                continue
            result = run_command(["sh", "-c", cmd], check=False, capture=True)
            if result.returncode != 0:
                raise KflowError(
                    f"fromCommand for key {key!r} in step {step_name!r} failed "
                    f"(exit {result.returncode}): {(result.stderr or result.stdout).strip()}"
                )
            out[key] = result.stdout.strip()
        return out

    def _apply_secret(self, resource: ResourceDef, step: StepDef) -> None:
        spec = step.secret
        name = spec.name or step.name
        ns = spec.namespace or self._eff_ns(resource, step) or resource.namespace
        if spec.if_not_exists and self.kube.resource_exists("secret", name, ns):
            self.console.print(f"  [dim]secret/{name} already exists; skipping[/dim]")
            return
        literals = dict(spec.literals)
        for field_name, env_var in spec.from_env.items():
            val = os.environ.get(env_var)
            if val is None:
                raise KflowError(
                    f"env var {env_var!r} required by step {step.name!r} is not set"
                )
            literals[field_name] = val
        literals.update(self._run_command_literals(spec.from_command, step.name))
        self.kube.secret_apply(
            name, ns,
            literals=literals,
            from_files=spec.from_files,
            from_env_file=spec.from_env_file,
        )

    def _apply_configmap(self, resource: ResourceDef, step: StepDef) -> None:
        spec = step.configmap
        name = spec.name or step.name
        ns = spec.namespace or self._eff_ns(resource, step) or resource.namespace
        if spec.if_not_exists and self.kube.resource_exists("configmap", name, ns):
            self.console.print(f"  [dim]configmap/{name} already exists; skipping[/dim]")
            return
        literals = dict(spec.literals)
        literals.update(self._run_command_literals(spec.from_command, step.name))
        self.kube.configmap_apply(
            name, ns,
            literals=literals,
            from_files=spec.from_files,
            from_dir=spec.from_dir,
        )

    def _apply_namespace_step(self, resource: ResourceDef, step: StepDef) -> None:
        spec = step.namespace_spec
        ns_name = spec.name or self._eff_ns(resource, step) or resource.namespace
        if spec.if_not_exists and self.kube.namespace_exists(ns_name):
            self.console.print(f"  [dim]namespace/{ns_name} already exists; skipping[/dim]")
            return
        self.kube.namespace_apply(ns_name, labels=spec.labels, annotations=spec.annotations)

    def _exec_step(self, resource: ResourceDef, step: StepDef,
                   command: List[str]) -> None:
        spec = step.exec_spec
        ns = self._eff_ns(resource, step) or resource.namespace
        result = self.kube.exec(
            ns,
            command=command,
            pod=spec.pod,
            selector=spec.selector,
            container=spec.container,
        )
        if not result.skipped and result.returncode != 0:
            raise CommandError(result.cmd, result.returncode,
                               result.stdout, result.stderr)
        if not result.skipped and self.verbose and result.stdout.strip():
            self.console.print(result.stdout.rstrip())

    def _run_docker_build(self, resource: ResourceDef, spec: DockerBuildSpec) -> None:
        # Compute primary tag, optionally prefixing with a registry host.
        primary_tag = f"{spec.registry}/{spec.tag}" if spec.registry else spec.tag

        cmd: List[str] = ["docker", "buildx", "build"]

        if spec.builder:
            cmd += ["--builder", spec.builder]

        cmd += ["-t", primary_tag]
        for extra in spec.extra_tags:
            cmd += ["-t", extra]

        if spec.file:
            cmd += ["-f", str(spec.file)]

        for k, v in spec.build_args.items():
            cmd += ["--build-arg", f"{k}={v}"]

        for k, v in spec.labels.items():
            cmd += ["--label", f"{k}={v}"]

        if spec.platform:
            cmd += ["--platform", spec.platform]

        if spec.target:
            cmd += ["--target", spec.target]

        if spec.network:
            cmd += ["--network", spec.network]

        for cf in spec.cache_from:
            cmd += ["--cache-from", cf]

        if spec.cache_to:
            cmd += ["--cache-to", spec.cache_to]

        if spec.push:
            cmd += ["--push"]
        elif spec.load:
            cmd += ["--load"]

        if spec.no_cache:
            cmd += ["--no-cache"]

        if spec.pull:
            cmd += ["--pull"]

        if spec.provenance is not None:
            cmd += ["--provenance", spec.provenance]

        if spec.sbom is not None:
            cmd += ["--sbom", spec.sbom]

        cmd += [str(spec.context)]

        if self.dry_run:
            self.console.print(
                f"  [dim](dry-run) would run: {' '.join(cmd)}[/dim]"
            )
            return
        run_command(cmd, check=True, capture=not self.verbose)

    def _helm_upgrade(self, helm: HelmSpec, resource: ResourceDef, *,
                      wait: bool = False, timeout: int = 300) -> None:
        self.kube.helm_upgrade(
            helm.release, helm.chart, helm.namespace,
            version=helm.version, values_files=helm.values_files,
            set_values=helm.set_values, repo_name=helm.repo_name,
            repo_url=helm.repo_url, wait=wait, timeout=timeout,
            create_namespace=self._should_create_ns(resource),
        )

    # -- workload targeting ----------------------------------------------

    def _resolve_selector(self, resource: ResourceDef) -> Optional[str]:
        """The label selector used to locate a resource's live workloads.

        An explicit ``selector:`` wins; otherwise a helm-backed resource falls
        back to the release's ``app.kubernetes.io/instance`` label. A resource
        with neither has no inferable selector and returns ``None`` - kflow must
        not guess by scanning the whole namespace, because that namespace may
        hold unrelated workloads owned by other resources.
        """
        if resource.selector:
            return resource.selector
        if resource.helm:
            return f"app.kubernetes.io/instance={resource.helm.release}"
        return None

    def _target_workloads(self, resource: ResourceDef) -> List[tuple]:
        """Return (kind, name) workloads to restart for a resource."""
        if resource.workloads:
            out = []
            for w in resource.workloads:
                kind, _, name = w.partition("/")
                if not name:
                    self.console.print(
                        f"  [yellow]![/yellow] ignoring malformed workload {w!r} "
                        "(expected kind/name)"
                    )
                    continue
                out.append((kind, name))
            return out
        selector = self._resolve_selector(resource)
        if not selector:
            # No explicit targets and nothing to derive a selector from: target
            # nothing rather than every workload in the namespace.
            return []
        live = self.kube.get_workloads(resource.namespace, selector)
        return [(w["kind"], w["name"]) for w in live]

    def _restart_workloads(self, resource: ResourceDef, *, wait: bool, timeout: int) -> int:
        workloads = self._target_workloads(resource)
        if not workloads:
            self.console.print(
                f"  [yellow]![/yellow] [cyan]{resource.name}[/cyan]: no workloads found "
                "(declare 'workloads:' or 'selector:' to target a restart)"
            )
            return 0
        for kind, name in workloads:
            self.console.print(
                f"  [bold]restart[/bold] [cyan]{resource.name}[/cyan] "
                f"[dim]{kind.lower()}/{name}[/dim]"
            )
            self.kube.rollout_restart(kind, name, resource.namespace)
            if wait and not self.dry_run:
                self.kube.rollout_status(kind, name, resource.namespace, timeout)
        return len(workloads)

    def _wait_resource(self, resource: ResourceDef, timeout: int) -> None:
        if self.dry_run:
            return
        for kind, name in self._target_workloads(resource):
            self.console.print(
                f"  [dim]…waiting for {kind.lower()}/{name}[/dim]"
            )
            self.kube.rollout_status(kind, name, resource.namespace, timeout)

    # -- operations -------------------------------------------------------

    def apply(self, names=None, *, with_deps: bool = True, wait: bool = True,
              timeout: int = 300) -> List[str]:
        targets = self.resolve_targets(names, operation="apply", with_deps=with_deps)
        self._banner("apply", targets)
        ns_ensured: set = set()
        for nid in self.graph.node_order:
            rname = self.graph.node_res[nid]
            if rname not in targets:
                continue
            resource = self.config.resource_map[rname]
            step = self.graph.node_step[nid]
            if self._should_create_ns(resource):
                eff_ns = self._eff_ns(resource, step)
                if eff_ns and eff_ns not in ns_ensured:
                    self.kube.ensure_namespace(eff_ns)
                    ns_ensured.add(eff_ns)
                if step.kind == "helm" and step.helm:
                    helm_ns = step.helm.namespace
                    if helm_ns and helm_ns not in ns_ensured:
                        self.kube.ensure_namespace(helm_ns)
                        ns_ensured.add(helm_ns)
            self._with_server_side(step, lambda: self._apply_step(resource, step))
            if wait and nid == self.graph.last_node[rname]:
                self._wait_resource(resource, timeout)
        for rname in targets:
            self.state.record_apply(self.config.resource_map[rname])
        self.state.save()
        return targets

    def destroy(self, names=None, *, with_deps: bool = True,
                delete_namespaces: bool = False, timeout: int = 300) -> List[str]:
        targets = self.resolve_targets(names, operation="destroy", with_deps=with_deps)
        self._banner("destroy", targets)
        namespaces: List[tuple] = []
        for nid in reversed(self.graph.node_order):
            rname = self.graph.node_res[nid]
            if rname not in targets:
                continue
            resource = self.config.resource_map[rname]
            self._destroy_step(resource, self.graph.node_step[nid])
            ns_entry = (resource.namespace, resource.keep_namespace)
            if ns_entry not in namespaces:
                namespaces.append(ns_entry)
        if delete_namespaces:
            for ns, keep in namespaces:
                if keep or ns == "default":
                    continue
                self.console.print(f"  [bold]delete namespace[/bold] [cyan]{ns}[/cyan]")
                self.kube.delete_namespace(ns)
        for rname in targets:
            self.state.record_operation(rname, "destroy")
        self.state.save()
        return targets

    def restart(self, names=None, *, with_deps: bool = False, wait: bool = True,
                timeout: int = 300) -> List[str]:
        targets = self.resolve_targets(names, operation="restart", with_deps=with_deps)
        self._banner("restart", targets)
        for rname in targets:
            resource = self.config.resource_map[rname]
            self._restart_workloads(resource, wait=wait, timeout=timeout)
            for step in resource.runner_steps:
                runner = self.registry.instantiate(step.runner.class_name, step.runner.config)
                runner.restart(self._runner_ctx(resource, step, "restart"))
            self.state.record_operation(rname, "restart")
        self.state.save()
        return targets

    def reload(self, names=None, *, with_deps: bool = True, wait: bool = True,
               timeout: int = 300) -> List[str]:
        targets = self.resolve_targets(names, operation="reload", with_deps=with_deps)
        self._banner("reload", targets)
        ns_ensured: set = set()
        # 1) re-apply manifests/helm/runner config non-destructively, in order.
        for nid in self.graph.node_order:
            rname = self.graph.node_res[nid]
            if rname not in targets:
                continue
            resource = self.config.resource_map[rname]
            step = self.graph.node_step[nid]
            if self._should_create_ns(resource):
                eff_ns = self._eff_ns(resource, step)
                if eff_ns and eff_ns not in ns_ensured:
                    self.kube.ensure_namespace(eff_ns)
                    ns_ensured.add(eff_ns)
                if step.kind == "helm" and step.helm:
                    helm_ns = step.helm.namespace
                    if helm_ns and helm_ns not in ns_ensured:
                        self.kube.ensure_namespace(helm_ns)
                        ns_ensured.add(helm_ns)
            self._with_server_side(step, lambda: self._reload_step(resource, step))
        # 2) restart affected workloads so they pick up new config.
        for rname in targets:
            resource = self.config.resource_map[rname]
            self._restart_workloads(resource, wait=wait, timeout=timeout)
            self.state.record_apply(resource)
            self.state.record_operation(rname, "reload")
        self.state.save()
        return targets

    def helm_sync(self, names=None, *, with_deps: bool = True) -> List[str]:
        """Run helm upgrade --install for every helm-backed target."""
        targets = self.resolve_targets(names, operation="apply", with_deps=with_deps)
        self._banner("helm", targets)
        touched = []
        ns_ensured: set = set()
        for rname in targets:
            resource = self.config.resource_map[rname]
            for step in resource.steps:
                if step.kind == "helm" and step.helm:
                    if self._should_create_ns(resource):
                        helm_ns = step.helm.namespace
                        if helm_ns and helm_ns not in ns_ensured:
                            self.kube.ensure_namespace(helm_ns)
                            ns_ensured.add(helm_ns)
                    self._step_header(resource, step, "helm")
                    self._helm_upgrade(step.helm, resource)
                    touched.append(rname)
        if not touched:
            self.console.print("  [yellow]no helm-backed resources in selection[/yellow]")
        return touched

    # -- inspection -------------------------------------------------------

    def status(self, names=None) -> List[dict]:
        targets = self.resolve_targets(names, operation="status", with_deps=False)
        rows = []
        for rname in targets:
            resource = self.config.resource_map[rname]
            entry = self.state.get(rname) or {}
            selector = self._resolve_selector(resource)
            live = self.kube.get_workloads(resource.namespace, selector) \
                if selector and not self.dry_run else []
            ready = sum(1 for w in live if w["ok"])
            helm_state = ""
            if resource.helm and not self.dry_run:
                hs = self.kube.helm_status(resource.helm.release, resource.helm.namespace)
                helm_state = (hs.get("info", {}) or {}).get("status", "")
            drift = self.state.drift(resource)
            rows.append({
                "name": rname,
                "phase": resource.phase_name,
                "namespace": resource.namespace,
                "state": entry.get("status", "unknown"),
                "last": entry.get("last_applied", "-"),
                "helm": helm_state or ("-" if resource.helm else ""),
                "workloads": f"{ready}/{len(live)}" if live else ("0/0" if selector else "-"),
                "drift": len(drift),
            })
        return rows

    def health(self, names=None) -> List[dict]:
        targets = self.resolve_targets(names, operation="health", with_deps=False)
        results = []
        for rname in targets:
            resource = self.config.resource_map[rname]
            selector = self._resolve_selector(resource)
            live = self.kube.get_workloads(resource.namespace, selector) \
                if selector and not self.dry_run else []
            healthy = all(w["ok"] for w in live) if live else None
            detail = ", ".join(
                f"{w['kind'].lower()}/{w['name']} {w['ready']}/{w['desired']}"
                for w in live
            ) or "no workloads"
            # runner health hooks
            for step in resource.runner_steps:
                runner = self.registry.instantiate(step.runner.class_name, step.runner.config)
                ok = runner.health(self._runner_ctx(resource, step, "health"))
                healthy = ok if healthy is None else (healthy and ok)
            results.append({
                "name": rname,
                "namespace": resource.namespace,
                "healthy": healthy,
                "detail": detail,
            })
        return results

    def logs(self, name, *, follow=False, tail=None, since=None,
             container=None, selector=None, previous=False):
        resource = self.config.resource_map.get(name)
        if resource is None:
            raise KflowError(f"no resource named {name!r}")
        sel = selector or resource.selector
        if not sel and resource.helm:
            sel = f"app.kubernetes.io/instance={resource.helm.release}"
        workload = None
        if not sel and resource.workloads:
            workload = resource.workloads[0]
        if not sel and not workload:
            raise KflowError(
                f"resource {name!r} has no selector/workloads; "
                "pass --selector to target pods"
            )
        return self.kube.logs(resource.namespace, selector=sel, workload=workload,
                              container=container, follow=follow, tail=tail,
                              since=since, previous=previous)
