"""kubectl / helm wrapper used by the engine and by custom runners.

``KubeClient`` shells out to ``kubectl`` and ``helm``. It honours the active
kubeconfig and (optionally) an explicit context, and it understands kflow's
dry-run semantics: read commands always execute, mutating commands are echoed
and skipped when ``dry_run`` is set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from .shell import CommandResult, format_command, run_command


def _flatten_set_values(values: dict, prefix: str = "") -> list:
    """Flatten a nested dict into helm ``--set key.path=value`` pairs."""
    pairs: list = []
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            pairs.extend(_flatten_set_values(value, path))
        elif isinstance(value, bool):
            pairs.append(f"{path}={'true' if value else 'false'}")
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                pairs.append(f"{path}[{i}]={item}")
        elif value is None:
            pairs.append(f"{path}=null")
        else:
            pairs.append(f"{path}={value}")
    return pairs


class KubeClient:
    """Thin, dry-run-aware wrapper around ``kubectl`` and ``helm``."""

    def __init__(
        self,
        *,
        context: Optional[str] = None,
        kubeconfig: Optional[str] = None,
        dry_run: bool = False,
        server_side: bool = False,
        console=None,
        verbose: bool = False,
    ):
        self.context = context
        self.kubeconfig = kubeconfig
        self.dry_run = dry_run
        self.server_side = server_side
        self.console = console
        self.verbose = verbose

    # -- plumbing ---------------------------------------------------------

    def _base(self, tool: str) -> list:
        cmd = [tool]
        if self.context:
            cmd += (["--context", self.context] if tool == "kubectl"
                    else ["--kube-context", self.context])
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        return cmd

    def _echo(self, cmd: Sequence[str], *, skipped: bool) -> None:
        if self.console is None:
            return
        prefix = "[dim](dry-run)[/dim] " if skipped else ""
        self.console.print(f"  [dim]→[/dim] {prefix}[dim]{format_command(cmd)}[/dim]")

    def _run(
        self,
        tool: str,
        args: Sequence[str],
        *,
        mutating: bool,
        check: bool,
        capture: bool,
        input_text: Optional[str],
        timeout: Optional[float],
    ) -> CommandResult:
        cmd = self._base(tool) + [str(a) for a in args]
        if mutating and self.dry_run:
            self._echo(cmd, skipped=True)
            return CommandResult(cmd=cmd, returncode=0, stdout="", stderr="", skipped=True)
        self._echo(cmd, skipped=False)
        result = run_command(
            cmd, check=check, capture=capture, input_text=input_text, timeout=timeout
        )
        if self.verbose and capture and result.stdout.strip():
            self.console and self.console.print(result.stdout.rstrip())
        return result

    def kubectl(self, args, *, mutating=False, check=True, capture=True,
                input_text=None, timeout=None) -> CommandResult:
        return self._run("kubectl", args, mutating=mutating, check=check,
                         capture=capture, input_text=input_text, timeout=timeout)

    def helm(self, args, *, mutating=False, check=True, capture=True,
             input_text=None, timeout=None) -> CommandResult:
        return self._run("helm", args, mutating=mutating, check=check,
                         capture=capture, input_text=input_text, timeout=timeout)

    # -- namespaces -------------------------------------------------------

    def namespace_exists(self, namespace: str) -> bool:
        res = self.kubectl(["get", "namespace", namespace], check=False)
        return res.returncode == 0 and not res.skipped

    def ensure_namespace(self, namespace: Optional[str]) -> None:
        """Create ``namespace`` if it does not exist (idempotent)."""
        if not namespace or namespace == "default":
            return
        if self.namespace_exists(namespace):
            return
        manifest = (
            "apiVersion: v1\nkind: Namespace\n"
            f"metadata:\n  name: {namespace}\n"
            "  labels:\n    app.kubernetes.io/managed-by: kflow\n"
        )
        self.apply_stdin(manifest)

    def namespace_apply(self, name: str, *,
                        labels: Optional[dict] = None,
                        annotations: Optional[dict] = None) -> CommandResult:
        """Create or update a namespace with optional labels/annotations (idempotent)."""
        all_labels = {"app.kubernetes.io/managed-by": "kflow"}
        all_labels.update(labels or {})
        manifest = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {name}\n  labels:\n"
        for k, v in all_labels.items():
            manifest += f"    {k}: {v}\n"
        if annotations:
            manifest += "  annotations:\n"
            for k, v in annotations.items():
                manifest += f"    {k}: {v}\n"
        return self.apply_stdin(manifest)

    def delete_namespace(self, namespace: str, *, wait: bool = False) -> CommandResult:
        args = ["delete", "namespace", namespace, "--ignore-not-found"]
        if not wait:
            args.append("--wait=false")
        return self.kubectl(args, mutating=True, check=False)

    def resource_exists(self, kind: str, name: str,
                        namespace: Optional[str] = None) -> bool:
        """Return True if the named resource exists in the cluster."""
        args = ["get", kind, name]
        if namespace:
            args += ["-n", namespace]
        res = self.kubectl(args, check=False)
        return res.returncode == 0 and not res.skipped

    # -- manifests --------------------------------------------------------

    def apply_file(self, path, *, namespace: Optional[str] = None) -> CommandResult:
        args = ["apply"]
        if self.server_side:
            args.append("--server-side")
        if namespace:
            args += ["-n", namespace]
        args += ["-f", str(path)]
        return self.kubectl(args, mutating=True)

    def apply_stdin(self, manifest: str, *, namespace: Optional[str] = None) -> CommandResult:
        args = ["apply"]
        if self.server_side:
            args.append("--server-side")
        if namespace:
            args += ["-n", namespace]
        args += ["-f", "-"]
        return self.kubectl(args, mutating=True, input_text=manifest)

    def delete_file(self, path, *, namespace: Optional[str] = None,
                    ignore_not_found: bool = True) -> CommandResult:
        args = ["delete", "-f", str(path)]
        if namespace:
            args = ["delete", "-n", namespace, "-f", str(path)]
        if ignore_not_found:
            args.append("--ignore-not-found")
        return self.kubectl(args, mutating=True, check=False)

    def apply_kustomize(self, path) -> CommandResult:
        args = ["apply"]
        if self.server_side:
            args.append("--server-side")
        args += ["-k", str(path)]
        return self.kubectl(args, mutating=True)

    def delete_kustomize(self, path, *, ignore_not_found: bool = True) -> CommandResult:
        args = ["delete", "-k", str(path)]
        if ignore_not_found:
            args.append("--ignore-not-found")
        return self.kubectl(args, mutating=True, check=False)

    def wait_for(self, resource: str, condition: Optional[str] = None,
                 namespace: Optional[str] = None, timeout: int = 120,
                 jsonpath: Optional[str] = None) -> CommandResult:
        """Wait for a resource condition or jsonpath expression.

        Pass ``condition`` for ``--for=condition=X`` (e.g. ``"available"``).
        Pass ``jsonpath`` for ``--for=jsonpath='{...}'`` (kubectl ≥ 1.23).
        """
        if condition:
            for_arg = f"--for=condition={condition}"
        elif jsonpath:
            for_arg = f"--for=jsonpath={jsonpath}"
        else:
            raise ValueError("wait_for requires either condition or jsonpath")
        args = ["wait", resource, for_arg, f"--timeout={timeout}s"]
        if namespace:
            args += ["-n", namespace]
        return self.kubectl(args, check=False)

    # -- rollouts ---------------------------------------------------------

    def rollout_restart(self, kind: str, name: str, namespace: str) -> CommandResult:
        return self.kubectl(
            ["rollout", "restart", f"{kind.lower()}/{name}", "-n", namespace],
            mutating=True,
        )

    def rollout_status(self, kind: str, name: str, namespace: str,
                       timeout: int = 300) -> CommandResult:
        return self.kubectl(
            ["rollout", "status", f"{kind.lower()}/{name}", "-n", namespace,
             f"--timeout={timeout}s"],
            check=False,
        )

    def rollout_wait_all(self, namespace: str, *,
                         kinds: Optional[Sequence[str]] = None,
                         selector: Optional[str] = None,
                         timeout: int = 300) -> None:
        """Wait for every rollout of the given kinds in ``namespace`` to complete.

        Mirrors the bash wait_for_rollouts helper: list resources of each kind,
        then run ``kubectl rollout status`` on each one. Raises ``CommandError``
        on the first failure.
        """
        from .shell import CommandError  # local import to avoid circular at module level
        if kinds is None:
            kinds = ["deployment", "statefulset", "daemonset"]
        for kind in kinds:
            args = ["get", kind, "-o", "name", "-n", namespace]
            if selector:
                args += ["-l", selector]
            result = self.kubectl(args, check=False)
            if result.returncode != 0 or not result.stdout.strip():
                continue
            for resource in result.stdout.strip().splitlines():
                resource = resource.strip()
                if not resource:
                    continue
                r = self.kubectl(
                    ["rollout", "status", resource, "-n", namespace,
                     f"--timeout={timeout}s"],
                    check=False,
                )
                if not r.skipped and r.returncode != 0:
                    raise CommandError(r.cmd, r.returncode, r.stdout, r.stderr)

    # -- queries ----------------------------------------------------------

    def get_json(self, args) -> dict:
        res = self.kubectl(list(args) + ["-o", "json"], check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            return {}

    _WORKLOAD_PLURAL = {
        "deployment": "deployments",
        "statefulset": "statefulsets",
        "daemonset": "daemonsets",
        "replicaset": "replicasets",
    }

    def get_workloads(self, namespace: str, selector: Optional[str] = None,
                      kinds: Optional[Sequence[str]] = None) -> list:
        """Return workload readiness dicts for the given workload kinds."""
        if kinds is not None:
            plural = [self._WORKLOAD_PLURAL.get(k.lower(), k.lower() + "s") for k in kinds]
        else:
            plural = ["deployments", "statefulsets", "daemonsets"]
        resource_list = ",".join(plural)
        args = ["get", resource_list, "-n", namespace]
        if selector:
            args += ["-l", selector]
        data = self.get_json(args)
        workloads = []
        for item in data.get("items", []):
            kind = item.get("kind", "")
            name = item.get("metadata", {}).get("name", "")
            status = item.get("status", {})
            spec = item.get("spec", {})
            if kind == "DaemonSet":
                ready = status.get("numberReady", 0)
                desired = status.get("desiredNumberScheduled", 0)
            else:
                ready = status.get("readyReplicas", 0)
                desired = spec.get("replicas", status.get("replicas", 0))
            workloads.append({
                "kind": kind,
                "name": name,
                "ready": ready,
                "desired": desired,
                "ok": desired == 0 or ready >= desired,
            })
        return workloads

    def get_pods(self, namespace: str, selector: Optional[str] = None) -> list:
        args = ["get", "pods", "-n", namespace]
        if selector:
            args += ["-l", selector]
        data = self.get_json(args)
        pods = []
        for item in data.get("items", []):
            status = item.get("status", {})
            pods.append({
                "name": item.get("metadata", {}).get("name", ""),
                "phase": status.get("phase", "Unknown"),
                "ready": all(
                    cs.get("ready", False)
                    for cs in status.get("containerStatuses", [])
                ) if status.get("containerStatuses") else False,
            })
        return pods

    # -- logs -------------------------------------------------------------

    def logs(self, namespace: str, *, selector: Optional[str] = None,
             workload: Optional[str] = None, pod: Optional[str] = None,
             container: Optional[str] = None, follow: bool = False,
             tail: Optional[int] = None, since: Optional[str] = None,
             previous: bool = False) -> CommandResult:
        args = ["logs", "-n", namespace]
        if pod:
            args.append(pod)
        elif workload:
            args.append(workload)
        elif selector:
            args += ["-l", selector]
        if container:
            args += ["-c", container]
        else:
            args.append("--all-containers=true")
        if not pod:
            args.append("--prefix=true")
        if tail is not None:
            args += ["--tail", str(tail)]
        if since:
            args += ["--since", since]
        if previous:
            args.append("--previous")
        if follow:
            args.append("-f")
        return self.kubectl(args, capture=not follow, check=False)

    def exec(self, namespace: str, *, command, selector: Optional[str] = None,
             pod: Optional[str] = None, container: Optional[str] = None) -> CommandResult:
        """Exec a command inside a pod (first running pod matching ``selector`` if given)."""
        target = pod
        if target is None and selector is not None:
            pods = self.get_pods(namespace, selector)
            running = [p for p in pods if p["phase"] == "Running"] or pods
            if not running:
                return CommandResult(cmd=["kubectl", "exec"], returncode=1,
                                     stderr=f"no pods match selector {selector!r} in {namespace}")
            target = running[0]["name"]
        if target is None:
            return CommandResult(cmd=["kubectl", "exec"], returncode=1,
                                 stderr="exec requires either pod or selector")
        args = ["exec", "-n", namespace, target]
        if container:
            args += ["-c", container]
        args += ["--", *command]
        return self.kubectl(args, mutating=True, check=False)

    # -- secrets / configmaps ---------------------------------------------

    def secret_apply(self, name: str, namespace: str, *,
                     literals: Optional[dict] = None,
                     from_files: Optional[list] = None,
                     from_env_file=None) -> CommandResult:
        """Create or update a generic secret via ``--dry-run=client | apply``."""
        args = ["create", "secret", "generic", name, "-n", namespace,
                "--dry-run=client", "-o", "yaml"]
        for k, v in (literals or {}).items():
            args.append(f"--from-literal={k}={v}")
        for f in (from_files or []):
            args.append(f"--from-file={f}")
        if from_env_file:
            args.append(f"--from-env-file={from_env_file}")
        gen = self.kubectl(args, mutating=False, check=True)
        if gen.skipped or not gen.stdout.strip():
            return gen
        return self.apply_stdin(gen.stdout)

    def secret_delete(self, name: str, namespace: str,
                      *, ignore_not_found: bool = True) -> CommandResult:
        args = ["delete", "secret", name, "-n", namespace]
        if ignore_not_found:
            args.append("--ignore-not-found")
        return self.kubectl(args, mutating=True, check=False)

    def configmap_apply(self, name: str, namespace: str, *,
                        literals: Optional[dict] = None,
                        from_files: Optional[list] = None,
                        from_dir=None) -> CommandResult:
        """Create or update a ConfigMap via ``--dry-run=client | apply``."""
        args = ["create", "configmap", name, "-n", namespace,
                "--dry-run=client", "-o", "yaml"]
        for k, v in (literals or {}).items():
            args.append(f"--from-literal={k}={v}")
        for f in (from_files or []):
            args.append(f"--from-file={f}")
        if from_dir:
            # Walk recursively; encode subdirectory separators as "---" so the
            # init-container templater can reconstruct the original path tree.
            for file_path in sorted(Path(from_dir).rglob("*")):
                if file_path.is_file():
                    rel = file_path.relative_to(from_dir)
                    key = str(rel).replace("/", "---")
                    args.append(f"--from-file={key}={file_path}")
        gen = self.kubectl(args, mutating=False, check=True)
        if gen.skipped or not gen.stdout.strip():
            return gen
        return self.apply_stdin(gen.stdout)

    def configmap_delete(self, name: str, namespace: str,
                         *, ignore_not_found: bool = True) -> CommandResult:
        args = ["delete", "configmap", name, "-n", namespace]
        if ignore_not_found:
            args.append("--ignore-not-found")
        return self.kubectl(args, mutating=True, check=False)

    # -- helm -------------------------------------------------------------

    def helm_repo_add(self, name: str, url: str) -> None:
        self.helm(["repo", "add", name, url], check=False)
        self.helm(["repo", "update", name], check=False)

    def helm_upgrade(self, release: str, chart: str, namespace: str, *,
                     version: Optional[str] = None, values_files=None,
                     set_values: Optional[dict] = None,
                     repo_name: Optional[str] = None,
                     repo_url: Optional[str] = None,
                     create_namespace: bool = True, wait: bool = False,
                     timeout: int = 300) -> CommandResult:
        if repo_name and repo_url:
            self.helm_repo_add(repo_name, repo_url)
        args = ["upgrade", "--install", release, chart, "-n", namespace]
        if create_namespace:
            args.append("--create-namespace")
        if version:
            args += ["--version", version]
        for vf in (values_files or []):
            args += ["-f", str(vf)]
        for pair in _flatten_set_values(set_values or {}):
            args += ["--set", pair]
        if wait:
            args += ["--wait", f"--timeout={timeout}s"]
        if self.dry_run:
            args.append("--dry-run")
            # helm --dry-run is a read-only render; run it for real to validate.
            return self.helm(args, mutating=False, check=False)
        return self.helm(args, mutating=True)

    def helm_uninstall(self, release: str, namespace: str) -> CommandResult:
        return self.helm(
            ["uninstall", release, "-n", namespace, "--ignore-not-found"],
            mutating=True, check=False,
        )

    def helm_status(self, release: str, namespace: str) -> dict:
        res = self.helm(["status", release, "-n", namespace, "-o", "json"], check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            return {}
