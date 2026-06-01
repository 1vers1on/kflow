"""kflow core engine and CLI (the single-file core system).

This module contains everything except the custom-runner API, which lives in
the :mod:`kflow.runners` sub-library. The pieces here are:

* configuration model + loader (root config and resource definitions)
* the dependency graph with phase ordering and cycle-breaking
* local state tracking
* the :class:`Kflow` engine implementing every lifecycle operation
* the ``click`` command-line interface

YAML identity
-------------
kflow files are distinguished from raw Kubernetes manifests by a top-level
``kflow:`` block::

    kflow:
      version: v1
      kind: Config            # or: ResourceDefinition

A document without that block is treated as a normal Kubernetes manifest and is
handed to ``kubectl`` verbatim.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from rich import box

from .runners import KubeClient, RunnerContext, RunnerRegistry
from .runners.registry import RunnerLoadError
from .runners.shell import CommandError, run_command

__version__ = "0.1.0"

# Top-level identifier block key. Its presence marks a file as a kflow file.
KFLOW_KEY = "kflow"
KIND_CONFIG = "Config"
KIND_RESOURCE = "ResourceDefinition"
DEFAULT_PHASE = "__default__"

console = Console()
err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class KflowError(Exception):
    """Base error for user-facing kflow failures."""


class ConfigError(KflowError):
    """Raised when configuration is invalid (bad schema, missing refs, …)."""


# --------------------------------------------------------------------------- #
# Configuration model
# --------------------------------------------------------------------------- #


@dataclass
class HelmSpec:
    release: str
    chart: str
    namespace: str
    version: Optional[str] = None
    repo_name: Optional[str] = None
    repo_url: Optional[str] = None
    values_files: List[Path] = field(default_factory=list)
    set_values: dict = field(default_factory=dict)


@dataclass
class RunnerSpec:
    class_name: str
    file: Optional[Path] = None
    config: dict = field(default_factory=dict)


@dataclass
class KustomizeSpec:
    path: Path


@dataclass
class WaitSpec:
    for_resource: str
    condition: Optional[str] = None   # --for=condition=X (e.g. "available")
    jsonpath: Optional[str] = None    # --for=jsonpath='{...}' (kubectl >= 1.23)
    namespace: Optional[str] = None
    timeout: int = 120


@dataclass
class ScriptSpec:
    run: str
    on_destroy: Optional[str] = None  # None = skip on destroy
    on_reload: Optional[str] = None   # None = re-run `run`
    workdir: Optional[Path] = None


@dataclass
class SecretSpec:
    """Declaratively create or upsert a Kubernetes generic Secret."""
    name: Optional[str] = None          # defaults to the step name
    namespace: Optional[str] = None     # defaults to the resource namespace
    literals: dict = field(default_factory=dict)         # key: value
    from_env: dict = field(default_factory=dict)         # key: ENV_VAR_NAME
    from_files: List[str] = field(default_factory=list)  # "path" or "key=path"
    from_env_file: Optional[Path] = None
    from_command: dict = field(default_factory=dict)     # key: shell command
    if_not_exists: bool = False  # skip if the secret already exists in the cluster


@dataclass
class ConfigMapSpec:
    """Declaratively create or upsert a Kubernetes ConfigMap."""
    name: Optional[str] = None
    namespace: Optional[str] = None
    literals: dict = field(default_factory=dict)
    from_files: List[str] = field(default_factory=list)  # "path" or "key=path"
    from_dir: Optional[Path] = None    # pass a whole directory to --from-file
    from_command: dict = field(default_factory=dict)     # key: shell command
    if_not_exists: bool = False


@dataclass
class ExecSpec:
    """Run a command inside a pod."""
    command: List[str]
    pod: Optional[str] = None       # literal pod name
    selector: Optional[str] = None  # label selector; picks first running pod
    container: Optional[str] = None
    on_destroy: Optional[List[str]] = None  # None = skip on destroy
    on_reload: Optional[List[str]] = None   # None = re-run command


@dataclass
class DockerBuildSpec:
    """Build (and optionally push) a Docker image."""
    context: Path
    tag: str
    file: Optional[Path] = None      # path to Dockerfile
    build_args: dict = field(default_factory=dict)
    push: bool = False
    platform: Optional[str] = None   # e.g. "linux/amd64,linux/arm64"
    target: Optional[str] = None     # multi-stage --target


@dataclass
class StepDef:
    name: str
    kind: str  # manifest | helm | kustomize | wait | script | runner |
               # secret | configmap | exec | docker-build
    depends_on: List[str] = field(default_factory=list)
    manifests: List[Union[Path, str]] = field(default_factory=list)  # Path or URL
    helm: Optional[HelmSpec] = None
    kustomize: Optional[KustomizeSpec] = None
    wait: Optional[WaitSpec] = None
    script: Optional[ScriptSpec] = None
    runner: Optional[RunnerSpec] = None
    secret: Optional[SecretSpec] = None
    configmap: Optional[ConfigMapSpec] = None
    exec_spec: Optional[ExecSpec] = None
    docker_build: Optional[DockerBuildSpec] = None


@dataclass
class ResourceDef:
    name: str
    namespace: str
    phase: Optional[str]
    steps: List[StepDef]
    depends_on: List[str] = field(default_factory=list)
    selector: Optional[str] = None
    workloads: List[str] = field(default_factory=list)
    keep_namespace: bool = False
    source_file: Optional[Path] = None
    description: str = ""
    # populated during graph build:
    phase_name: str = DEFAULT_PHASE
    phase_index: int = 0

    @property
    def helm(self) -> Optional[HelmSpec]:
        for step in self.steps:
            if step.kind == "helm":
                return step.helm
        return None

    @property
    def runner_steps(self) -> List[StepDef]:
        return [s for s in self.steps if s.kind == "runner"]


@dataclass
class PhaseDef:
    name: str
    description: str = ""


@dataclass
class RootConfig:
    path: Path
    state_dir: Path
    context: Optional[str]
    phases: List[PhaseDef]
    runner_files: List[Path]
    resources: List[ResourceDef]

    @property
    def resource_map(self) -> Dict[str, ResourceDef]:
        return {r.name: r for r in self.resources}


# --------------------------------------------------------------------------- #
# YAML helpers
# --------------------------------------------------------------------------- #


def is_kflow_doc(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get(KFLOW_KEY), dict)


def kflow_kind(doc) -> Optional[str]:
    if not is_kflow_doc(doc):
        return None
    return doc[KFLOW_KEY].get("kind")


def detect_doc_type(doc) -> str:
    """Classify a YAML document: 'config', 'resource', 'manifest' or 'unknown'."""
    if is_kflow_doc(doc):
        kind = kflow_kind(doc)
        if kind == KIND_CONFIG:
            return "config"
        if kind == KIND_RESOURCE:
            return "resource"
        return "unknown"
    if isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc:
        return "manifest"
    return "unknown"


def _load_yaml(path: Path):
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def _load_yaml_all(path: Path):
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def _resolve(base: Path, value: str) -> Path:
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else (base / p).resolve()


def _is_url(value) -> bool:
    return str(value).startswith(("http://", "https://"))


def file_hash(path) -> Optional[str]:
    p = str(path)
    if _is_url(p):
        return None  # remote resources can't be hashed locally
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Configuration loader
# --------------------------------------------------------------------------- #


def _parse_helm(spec: dict, default_ns: str, base: Path, resource_name: str) -> HelmSpec:
    if "chart" not in spec:
        raise ConfigError(f"helm block for {resource_name!r} is missing 'chart'")
    repo = spec.get("repo") or {}
    return HelmSpec(
        release=spec.get("release", resource_name),
        chart=spec["chart"],
        namespace=spec.get("namespace", default_ns),
        version=spec.get("version"),
        repo_name=repo.get("name"),
        repo_url=repo.get("url"),
        values_files=[_resolve(base, v) for v in (spec.get("valuesFiles") or [])],
        set_values=spec.get("values") or {},
    )


def _parse_runner(spec: dict, base: Path, resource_name: str) -> RunnerSpec:
    if "class" not in spec:
        raise ConfigError(f"runner block for {resource_name!r} is missing 'class'")
    return RunnerSpec(
        class_name=spec["class"],
        file=_resolve(base, spec["file"]) if spec.get("file") else None,
        config=spec.get("config") or {},
    )


def _parse_kustomize(spec: dict, base: Path, resource_name: str) -> KustomizeSpec:
    if "path" not in spec:
        raise ConfigError(f"kustomize block for {resource_name!r} is missing 'path'")
    return KustomizeSpec(path=_resolve(base, spec["path"]))


def _parse_wait(spec: dict, resource_name: str) -> WaitSpec:
    if "for" not in spec:
        raise ConfigError(f"wait block for {resource_name!r} is missing 'for'")
    condition = spec.get("condition")
    jsonpath = spec.get("jsonpath")
    if not condition and not jsonpath:
        raise ConfigError(
            f"wait block for {resource_name!r} requires 'condition' or 'jsonpath'"
        )
    return WaitSpec(
        for_resource=spec["for"],
        condition=condition,
        jsonpath=jsonpath,
        namespace=spec.get("namespace"),
        timeout=int(spec.get("timeout", 120)),
    )


def _parse_script(spec: dict, base: Path, resource_name: str) -> ScriptSpec:
    if "run" not in spec:
        raise ConfigError(f"script block for {resource_name!r} is missing 'run'")
    workdir_raw = spec.get("workdir")
    return ScriptSpec(
        run=spec["run"],
        on_destroy=spec.get("onDestroy"),
        on_reload=spec.get("onReload"),
        workdir=_resolve(base, workdir_raw) if workdir_raw else None,
    )


def _parse_manifests(specs: list, base: Path) -> List[Union[Path, str]]:
    result: List[Union[Path, str]] = []
    for m in specs:
        s = str(m)
        if _is_url(s):
            result.append(s)
        else:
            result.append(_resolve(base, s))
    return result


def _parse_secret(spec: dict, base: Path, resource_name: str, step_name: str) -> SecretSpec:
    from_files: List[str] = []
    for entry in (spec.get("fromFiles") or []):
        s = str(entry)
        if "=" in s:
            key, _, path_part = s.partition("=")
            from_files.append(f"{key}={_resolve(base, path_part)}")
        else:
            from_files.append(str(_resolve(base, s)))
    env_file_raw = spec.get("fromEnvFile")
    return SecretSpec(
        name=spec.get("name"),
        namespace=spec.get("namespace"),
        literals=dict(spec.get("literals") or {}),
        from_env=dict(spec.get("fromEnv") or {}),
        from_files=from_files,
        from_env_file=_resolve(base, env_file_raw) if env_file_raw else None,
        from_command=dict(spec.get("fromCommand") or {}),
        if_not_exists=bool(spec.get("ifNotExists", False)),
    )


def _parse_configmap(spec: dict, base: Path, resource_name: str, step_name: str) -> ConfigMapSpec:
    from_files: List[str] = []
    for entry in (spec.get("fromFiles") or []):
        s = str(entry)
        if "=" in s:
            key, _, path_part = s.partition("=")
            from_files.append(f"{key}={_resolve(base, path_part)}")
        else:
            from_files.append(str(_resolve(base, s)))
    from_dir_raw = spec.get("fromDir")
    return ConfigMapSpec(
        name=spec.get("name"),
        namespace=spec.get("namespace"),
        literals=dict(spec.get("literals") or {}),
        from_files=from_files,
        from_dir=_resolve(base, from_dir_raw) if from_dir_raw else None,
        from_command=dict(spec.get("fromCommand") or {}),
        if_not_exists=bool(spec.get("ifNotExists", False)),
    )


def _parse_exec(spec: dict, resource_name: str) -> ExecSpec:
    command = spec.get("command")
    if not command:
        raise ConfigError(f"exec block for {resource_name!r} is missing 'command'")
    if isinstance(command, str):
        command = ["sh", "-c", command]

    pod = spec.get("pod")
    selector = spec.get("selector")
    if not pod and not selector:
        raise ConfigError(
            f"exec block for {resource_name!r} requires 'pod' or 'selector'"
        )

    def _coerce_cmd(raw) -> Optional[List[str]]:
        if raw is None:
            return None
        if isinstance(raw, str):
            return ["sh", "-c", raw] if raw else None
        return list(raw) if raw else None

    return ExecSpec(
        command=list(command),
        pod=pod,
        selector=selector,
        container=spec.get("container"),
        on_destroy=_coerce_cmd(spec.get("onDestroy")),
        on_reload=_coerce_cmd(spec.get("onReload")),
    )


def _parse_docker_build(spec: dict, base: Path, resource_name: str) -> DockerBuildSpec:
    if "context" not in spec:
        raise ConfigError(f"dockerBuild block for {resource_name!r} is missing 'context'")
    if "tag" not in spec:
        raise ConfigError(f"dockerBuild block for {resource_name!r} is missing 'tag'")
    file_raw = spec.get("file")
    return DockerBuildSpec(
        context=_resolve(base, spec["context"]),
        tag=spec["tag"],
        file=_resolve(base, file_raw) if file_raw else None,
        build_args=dict(spec.get("buildArgs") or {}),
        push=bool(spec.get("push", False)),
        platform=spec.get("platform"),
        target=spec.get("target"),
    )


def _parse_step(spec: dict, default_ns: str, base: Path, resource_name: str) -> StepDef:
    name = spec.get("name")
    if not name:
        raise ConfigError(f"a step in {resource_name!r} is missing 'name'")
    depends_on = list(spec.get("dependsOn") or [])
    if spec.get("manifests"):
        return StepDef(name=name, kind="manifest", depends_on=depends_on,
                       manifests=_parse_manifests(spec["manifests"], base))
    if spec.get("helm"):
        return StepDef(name=name, kind="helm", depends_on=depends_on,
                       helm=_parse_helm(spec["helm"], default_ns, base, resource_name))
    if spec.get("kustomize"):
        return StepDef(name=name, kind="kustomize", depends_on=depends_on,
                       kustomize=_parse_kustomize(spec["kustomize"], base, resource_name))
    if spec.get("wait"):
        return StepDef(name=name, kind="wait", depends_on=depends_on,
                       wait=_parse_wait(spec["wait"], resource_name))
    if spec.get("script"):
        return StepDef(name=name, kind="script", depends_on=depends_on,
                       script=_parse_script(spec["script"], base, resource_name))
    if spec.get("runner"):
        return StepDef(name=name, kind="runner", depends_on=depends_on,
                       runner=_parse_runner(spec["runner"], base, resource_name))
    if spec.get("secret"):
        return StepDef(name=name, kind="secret", depends_on=depends_on,
                       secret=_parse_secret(spec["secret"], base, resource_name, name))
    if spec.get("configmap"):
        return StepDef(name=name, kind="configmap", depends_on=depends_on,
                       configmap=_parse_configmap(spec["configmap"], base, resource_name, name))
    if spec.get("exec"):
        return StepDef(name=name, kind="exec", depends_on=depends_on,
                       exec_spec=_parse_exec(spec["exec"], resource_name))
    if spec.get("dockerBuild"):
        return StepDef(name=name, kind="docker-build", depends_on=depends_on,
                       docker_build=_parse_docker_build(spec["dockerBuild"], base, resource_name))
    raise ConfigError(
        f"step {name!r} in {resource_name!r} must define one of: "
        "manifests, helm, kustomize, wait, script, runner, "
        "secret, configmap, exec, dockerBuild"
    )


def _build_resource(doc: dict, source: Path) -> ResourceDef:
    base = source.parent
    name = doc.get("name")
    if not name:
        raise ConfigError(f"resource definition in {source} is missing 'name'")
    namespace = doc.get("namespace", "default")
    steps: List[StepDef] = []

    for sspec in (doc.get("steps") or []):
        steps.append(_parse_step(sspec, namespace, base, name))

    step_names = [s.name for s in steps]
    if len(step_names) != len(set(step_names)):
        dupes = sorted({n for n in step_names if step_names.count(n) > 1})
        raise ConfigError(f"resource {name!r} has duplicate step names: {dupes}")

    return ResourceDef(
        name=name,
        namespace=namespace,
        phase=doc.get("phase"),
        steps=steps,
        depends_on=list(doc.get("dependsOn") or []),
        selector=doc.get("selector"),
        workloads=list(doc.get("workloads") or []),
        keep_namespace=bool(doc.get("keepNamespace", False)),
        source_file=source,
        description=doc.get("description", ""),
    )


def _collect_resource_files(base: Path, entry: str) -> List[Path]:
    path = _resolve(base, entry)
    if path.is_dir():
        files = sorted(p for p in path.iterdir()
                       if p.suffix in (".yaml", ".yml") and p.is_file())
        if not files:
            raise ConfigError(f"resource directory has no YAML files: {path}")
        return files
    if not path.exists():
        raise ConfigError(f"resource path not found: {path}")
    return [path]


def load_root_config(config_path) -> RootConfig:
    """Load and validate the root config and all resource definitions."""
    path = Path(os.path.expanduser(str(config_path))).resolve()
    if not path.exists():
        raise ConfigError(
            f"root config not found: {path}\n"
            "Pass --config/-c or create a kflow.yaml in the current directory."
        )
    doc = _load_yaml(path)
    if detect_doc_type(doc) != "config":
        raise ConfigError(
            f"{path} is not a kflow Config file. It must start with:\n"
            "  kflow:\n    version: v1\n    kind: Config"
        )
    base = path.parent

    state_block = doc.get("state") or {}
    state_dir = Path(os.path.expanduser(state_block.get("dir", "~/.kflow")))

    phases: List[PhaseDef] = []
    for entry in (doc.get("phases") or []):
        if isinstance(entry, str):
            phases.append(PhaseDef(name=entry))
        elif isinstance(entry, dict) and entry.get("name"):
            phases.append(PhaseDef(name=entry["name"],
                                   description=entry.get("description", "")))
        else:
            raise ConfigError(f"invalid phase entry: {entry!r}")

    runner_files = [_resolve(base, f) for f in (doc.get("runners") or [])]

    resources: List[ResourceDef] = []
    seen: set = set()
    for entry in (doc.get("resources") or []):
        for rfile in _collect_resource_files(base, entry):
            for rdoc in _load_yaml_all(rfile):
                dtype = detect_doc_type(rdoc)
                if dtype != "resource":
                    raise ConfigError(
                        f"{rfile} contains a non-resource document (type={dtype}). "
                        "Resource files must use 'kflow.kind: ResourceDefinition'."
                    )
                res = _build_resource(rdoc, rfile)
                if res.name in seen:
                    raise ConfigError(f"duplicate resource name: {res.name!r}")
                seen.add(res.name)
                resources.append(res)

    if not resources:
        raise ConfigError("root config lists no resources")

    return RootConfig(
        path=path,
        state_dir=state_dir,
        context=doc.get("context"),
        phases=phases,
        runner_files=runner_files,
        resources=resources,
    )


# --------------------------------------------------------------------------- #
# Dependency graph + phase ordering
# --------------------------------------------------------------------------- #


class DependencyGraph:
    """Step-level dependency graph with strict phase ordering.

    Nodes are ``"resource.step"`` ids. Edges encode "depends on" relationships
    (the dependency runs first). Phases are a strict outer ordering: every step
    of phase *N* runs before any step of phase *N+1*. Backward cross-phase
    dependencies are satisfied automatically; forward ones are reported and
    ignored (this is how circular relationships like longhorn↔traefik are
    resolved without erroring). Genuine same-phase cycles are broken
    deterministically with a warning rather than raising.
    """

    def __init__(self, config: RootConfig):
        self.config = config
        self.resources = config.resource_map
        self.warnings: List[str] = []

        self._assign_phases()
        self._build_nodes()
        self._build_edges()
        self.node_order = self._compute_order()
        self._build_resource_views()

    # -- phases -----------------------------------------------------------

    def _assign_phases(self) -> None:
        phase_names = [p.name for p in self.config.phases]
        needs_default = any(r.phase is None for r in self.config.resources)
        if needs_default or not phase_names:
            phase_names = phase_names + [DEFAULT_PHASE]
        self.phase_names = phase_names
        index = {name: i for i, name in enumerate(phase_names)}
        for res in self.config.resources:
            pname = res.phase or DEFAULT_PHASE
            if pname not in index:
                raise ConfigError(
                    f"resource {res.name!r} references unknown phase {pname!r}. "
                    f"Declared phases: {', '.join(n for n in phase_names if n != DEFAULT_PHASE) or '(none)'}"
                )
            res.phase_name = pname
            res.phase_index = index[pname]

    # -- nodes / edges ----------------------------------------------------

    @staticmethod
    def node_id(resource: str, step: str) -> str:
        return f"{resource}.{step}"

    def _build_nodes(self) -> None:
        self.nodes: List[str] = []
        self.node_res: Dict[str, str] = {}
        self.node_step: Dict[str, StepDef] = {}
        self.phase_idx: Dict[str, int] = {}
        self.seq: Dict[str, int] = {}
        counter = 0
        for res in self.config.resources:
            for step in res.steps:
                nid = self.node_id(res.name, step.name)
                self.nodes.append(nid)
                self.node_res[nid] = res.name
                self.node_step[nid] = step
                self.phase_idx[nid] = res.phase_index
                self.seq[nid] = counter
                counter += 1

    def _resolve_ref(self, ref: str, resource: ResourceDef) -> str:
        """Resolve a dependency reference to a node id."""
        if "." in ref:
            res_name, _, step_name = ref.partition(".")
            target = self.node_id(res_name, step_name)
            if target not in self.node_step:
                raise ConfigError(
                    f"{resource.name!r} depends on unknown step {ref!r}"
                )
            return target
        # bare ref: prefer a step in the same resource, else a resource name.
        same = self.node_id(resource.name, ref)
        if same in self.node_step:
            return same
        if ref in self.resources:
            return self._last_node(ref)
        raise ConfigError(
            f"{resource.name!r} depends on unknown step/resource {ref!r}"
        )

    def _last_node(self, resource_name: str) -> str:
        res = self.resources[resource_name]
        if not res.steps:
            raise ConfigError(
                f"resource {resource_name!r} has no steps but is used as a dependency"
            )
        return self.node_id(resource_name, res.steps[-1].name)

    def _first_node(self, resource_name: str) -> str:
        res = self.resources[resource_name]
        if not res.steps:
            raise ConfigError(f"resource {resource_name!r} has no steps")
        return self.node_id(resource_name, res.steps[0].name)

    def _build_edges(self) -> None:
        # edges as (dependent, dependency): dependency must run before dependent
        self.edges: List[tuple] = []
        for res in self.config.resources:
            # sequential ordering within a resource
            for i, step in enumerate(res.steps):
                nid = self.node_id(res.name, step.name)
                if i > 0:
                    prev = self.node_id(res.name, res.steps[i - 1].name)
                    self.edges.append((nid, prev))
                for ref in step.depends_on:
                    self.edges.append((nid, self._resolve_ref(ref, res)))
            # resource-level dependency: first step waits for the whole target
            if res.steps:
                first = self._first_node(res.name)
                for ref in res.depends_on:
                    self.edges.append((first, self._resolve_ref(ref, res)))
        # Deduplicate while preserving order (an explicit dependsOn may coincide
        # with the implicit sequential edge between adjacent steps).
        seen: set = set()
        deduped: List[tuple] = []
        for edge in self.edges:
            if edge not in seen:
                seen.add(edge)
                deduped.append(edge)
        self.edges = deduped

    # -- ordering ---------------------------------------------------------

    def _compute_order(self) -> List[str]:
        order: List[str] = []
        by_phase: Dict[int, List[str]] = defaultdict(list)
        for nid in self.nodes:
            by_phase[self.phase_idx[nid]].append(nid)

        for pidx in sorted(by_phase):
            phase_nodes = set(by_phase[pidx])
            intra: List[tuple] = []
            for dependent, dependency in self.edges:
                if dependent not in phase_nodes:
                    continue
                if dependency in phase_nodes:
                    intra.append((dependent, dependency))
                elif self.phase_idx[dependency] > pidx:
                    self.warnings.append(
                        f"{dependent} depends on {dependency} in a later phase; "
                        "ignoring (phase order takes precedence)"
                    )
                # earlier-phase dependency is already satisfied
            order.extend(self._topo(phase_nodes, intra))
        return order

    def _topo(self, nodes: set, edges: List[tuple]) -> List[str]:
        indeg = {n: 0 for n in nodes}
        adj: Dict[str, List[str]] = defaultdict(list)
        for dependent, dependency in edges:
            adj[dependency].append(dependent)
            indeg[dependent] += 1

        remaining = set(nodes)
        out: List[str] = []
        while remaining:
            avail = sorted((n for n in remaining if indeg[n] == 0),
                           key=lambda n: self.seq[n])
            if not avail:
                forced = min(remaining, key=lambda n: self.seq[n])
                self.warnings.append(
                    "circular dependency among "
                    f"{sorted(remaining, key=lambda n: self.seq[n])}; "
                    f"breaking at {forced}"
                )
                indeg[forced] = 0
                avail = [forced]
            nxt = avail[0]
            out.append(nxt)
            remaining.discard(nxt)
            for m in adj[nxt]:
                if m in remaining:
                    indeg[m] -= 1
        return out

    # -- resource-level views --------------------------------------------

    def _build_resource_views(self) -> None:
        self.resource_order: List[str] = []
        seen = set()
        for nid in self.node_order:
            r = self.node_res[nid]
            if r not in seen:
                seen.add(r)
                self.resource_order.append(r)
        self.last_node: Dict[str, str] = {}
        for nid in self.node_order:
            self.last_node[self.node_res[nid]] = nid

        self.res_depends: Dict[str, set] = defaultdict(set)
        for dependent, dependency in self.edges:
            ra, rb = self.node_res[dependent], self.node_res[dependency]
            if ra != rb:
                self.res_depends[ra].add(rb)
        self.res_dependents: Dict[str, set] = defaultdict(set)
        for r, deps in self.res_depends.items():
            for d in deps:
                self.res_dependents[d].add(r)

    def closure(self, names, *, dependents: bool) -> set:
        graph = self.res_dependents if dependents else self.res_depends
        out = set(names)
        stack = list(names)
        while stack:
            cur = stack.pop()
            for nxt in graph.get(cur, ()):  # noqa: B007
                if nxt not in out:
                    out.add(nxt)
                    stack.append(nxt)
        return out


# --------------------------------------------------------------------------- #
# State tracking (local JSON)
# --------------------------------------------------------------------------- #


class StateManager:
    """Local, file-based state for what kflow has applied.

    Live cluster facts (pod readiness, rollout status, helm release status) are
    always queried fresh; this store only records kflow's own bookkeeping
    (phase, last operation, per-step manifest hashes for drift detection).
    """

    def __init__(self, state_dir: Path, cluster_key: str):
        self.path = Path(state_dir) / "state.json"
        self.cluster_key = cluster_key
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        return {"version": 1, "clusters": {}}

    @property
    def cluster(self) -> dict:
        clusters = self.data.setdefault("clusters", {})
        return clusters.setdefault(self.cluster_key, {"resources": {}})

    def get(self, name: str) -> Optional[dict]:
        return self.cluster["resources"].get(name)

    def all(self) -> dict:
        return self.cluster["resources"]

    def record_apply(self, resource: ResourceDef) -> None:
        entry = self.cluster["resources"].setdefault(resource.name, {})
        entry["phase"] = resource.phase_name
        entry["namespace"] = resource.namespace
        entry["status"] = "applied"
        entry["last_operation"] = "apply"
        entry["last_applied"] = now_iso()
        steps: dict = {}
        for step in resource.steps:
            if step.kind == "manifest":
                steps[step.name] = {
                    "kind": "manifest",
                    "manifests": {str(m): file_hash(m) for m in step.manifests},
                }
            elif step.kind == "helm" and step.helm:
                steps[step.name] = {"kind": "helm", "release": step.helm.release}
                entry["helm_release"] = step.helm.release
            elif step.kind == "kustomize" and step.kustomize:
                steps[step.name] = {"kind": "kustomize", "path": str(step.kustomize.path)}
            elif step.kind == "wait":
                steps[step.name] = {"kind": "wait"}
            elif step.kind == "script":
                steps[step.name] = {"kind": "script"}
            elif step.kind == "runner" and step.runner:
                steps[step.name] = {"kind": "runner", "class": step.runner.class_name}
            elif step.kind == "secret" and step.secret:
                steps[step.name] = {"kind": "secret",
                                    "name": step.secret.name or step.name}
            elif step.kind == "configmap" and step.configmap:
                steps[step.name] = {"kind": "configmap",
                                    "name": step.configmap.name or step.name}
            elif step.kind == "exec":
                steps[step.name] = {"kind": "exec"}
            elif step.kind == "docker-build" and step.docker_build:
                steps[step.name] = {"kind": "docker-build",
                                    "tag": step.docker_build.tag}
        entry["steps"] = steps

    def record_operation(self, name: str, operation: str) -> None:
        entry = self.cluster["resources"].setdefault(name, {})
        entry["last_operation"] = operation
        entry[f"last_{operation}"] = now_iso()
        if operation == "destroy":
            entry["status"] = "destroyed"

    def drift(self, resource: ResourceDef) -> List[str]:
        """Return manifest paths whose on-disk hash differs from last apply."""
        entry = self.get(resource.name)
        if not entry:
            return []
        changed = []
        for step in resource.steps:
            if step.kind != "manifest":
                continue
            recorded = (entry.get("steps", {}).get(step.name, {})
                        .get("manifests", {}))
            for m in step.manifests:
                key = str(m)
                if _is_url(key):
                    continue  # remote resources can't be checked for drift
                if recorded.get(key) != file_hash(m):
                    changed.append(key)
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")

    def clear(self) -> None:
        self.data.setdefault("clusters", {})[self.cluster_key] = {"resources": {}}
        self.save()


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class Kflow:
    """The orchestration engine. One instance per loaded configuration."""

    def __init__(self, config: RootConfig, *, dry_run: bool = False,
                 context: Optional[str] = None, verbose: bool = False,
                 console_: Optional[Console] = None):
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose
        self.console = console_ or console
        self.context = context or config.context
        self.graph = DependencyGraph(config)
        self.kube = KubeClient(context=self.context, dry_run=dry_run,
                               console=self.console, verbose=verbose)
        cluster_key = self.context or "default"
        self.state = StateManager(config.state_dir, cluster_key)
        self.registry = RunnerRegistry(console=self.console)
        self._load_runners()

    @classmethod
    def load(cls, config_path, **kwargs) -> "Kflow":
        return cls(load_root_config(config_path), **kwargs)

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
            namespace=resource.namespace,
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

    def _apply_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "apply")
        if step.kind == "manifest":
            for m in step.manifests:
                self.kube.apply_file(m, namespace=resource.namespace)
        elif step.kind == "helm" and step.helm:
            self._helm_upgrade(step.helm)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.apply_kustomize(step.kustomize.path)
        elif step.kind == "wait" and step.wait:
            ns = step.wait.namespace or resource.namespace
            result = self.kube.wait_for(
                step.wait.for_resource,
                step.wait.condition,
                namespace=ns,
                timeout=step.wait.timeout,
                jsonpath=step.wait.jsonpath,
            )
            self._check_wait_result(result)
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
            self._exec_step(resource, step.exec_spec, step.exec_spec.command)
        elif step.kind == "docker-build" and step.docker_build:
            self._run_docker_build(resource, step.docker_build)

    def _destroy_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "destroy")
        if step.kind == "manifest":
            for m in reversed(step.manifests):
                self.kube.delete_file(m, namespace=resource.namespace)
        elif step.kind == "helm" and step.helm:
            self.kube.helm_uninstall(step.helm.release, step.helm.namespace)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.delete_kustomize(step.kustomize.path)
        elif step.kind == "wait":
            pass  # nothing to undo for a wait step
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
                ns = step.secret.namespace or resource.namespace
                self.kube.secret_delete(name, ns)
        elif step.kind == "configmap" and step.configmap:
            if not step.configmap.if_not_exists:
                name = step.configmap.name or step.name
                ns = step.configmap.namespace or resource.namespace
                self.kube.configmap_delete(name, ns)
        elif step.kind == "exec" and step.exec_spec:
            if step.exec_spec.on_destroy is not None:
                self._exec_step(resource, step.exec_spec, step.exec_spec.on_destroy)
        elif step.kind == "docker-build":
            pass  # docker images are not removed on destroy

    def _reload_step(self, resource: ResourceDef, step: StepDef) -> None:
        self._step_header(resource, step, "reload")
        if step.kind == "manifest":
            for m in step.manifests:
                self.kube.apply_file(m, namespace=resource.namespace)
        elif step.kind == "helm" and step.helm:
            self._helm_upgrade(step.helm)
        elif step.kind == "kustomize" and step.kustomize:
            self.kube.apply_kustomize(step.kustomize.path)
        elif step.kind == "wait" and step.wait:
            ns = step.wait.namespace or resource.namespace
            result = self.kube.wait_for(
                step.wait.for_resource,
                step.wait.condition,
                namespace=ns,
                timeout=step.wait.timeout,
                jsonpath=step.wait.jsonpath,
            )
            self._check_wait_result(result)
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
            self._exec_step(resource, spec, cmd)
        elif step.kind == "docker-build" and step.docker_build:
            self._run_docker_build(resource, step.docker_build)

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
        ns = spec.namespace or resource.namespace
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
        ns = spec.namespace or resource.namespace
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

    def _exec_step(self, resource: ResourceDef, spec: ExecSpec,
                   command: List[str]) -> None:
        result = self.kube.exec(
            resource.namespace,
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
        cmd = ["docker", "build", "-t", spec.tag, str(spec.context)]
        if spec.file:
            cmd += ["-f", str(spec.file)]
        for k, v in spec.build_args.items():
            cmd += ["--build-arg", f"{k}={v}"]
        if spec.platform:
            cmd += ["--platform", spec.platform]
        if spec.target:
            cmd += ["--target", spec.target]
        if self.dry_run:
            self.console.print(
                f"  [dim](dry-run) would run: {' '.join(cmd)}[/dim]"
            )
            return
        run_command(cmd, check=True, capture=not self.verbose)
        if spec.push:
            run_command(["docker", "push", spec.tag],
                        check=True, capture=not self.verbose)

    def _helm_upgrade(self, helm: HelmSpec, *, wait: bool = False, timeout: int = 300) -> None:
        self.kube.helm_upgrade(
            helm.release, helm.chart, helm.namespace,
            version=helm.version, values_files=helm.values_files,
            set_values=helm.set_values, repo_name=helm.repo_name,
            repo_url=helm.repo_url, wait=wait, timeout=timeout,
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
        ensured: set = set()
        for nid in self.graph.node_order:
            rname = self.graph.node_res[nid]
            if rname not in targets:
                continue
            resource = self.config.resource_map[rname]
            step = self.graph.node_step[nid]
            if rname not in ensured:
                self.kube.ensure_namespace(resource.namespace)
                ensured.add(rname)
            self._apply_step(resource, step)
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
        ensured: set = set()
        # 1) re-apply manifests/helm/runner config non-destructively, in order.
        for nid in self.graph.node_order:
            rname = self.graph.node_res[nid]
            if rname not in targets:
                continue
            resource = self.config.resource_map[rname]
            if rname not in ensured:
                self.kube.ensure_namespace(resource.namespace)
                ensured.add(rname)
            self._reload_step(resource, self.graph.node_step[nid])
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
        for rname in targets:
            resource = self.config.resource_map[rname]
            for step in resource.steps:
                if step.kind == "helm" and step.helm:
                    self.kube.ensure_namespace(step.helm.namespace)
                    self._step_header(resource, step, "helm")
                    self._helm_upgrade(step.helm)
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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_tree(engine: Kflow) -> Tree:
    cfg = engine.config
    root = Tree(f"[bold]{cfg.path.name}[/bold] [dim]({len(cfg.resources)} resources)[/dim]")
    by_phase: Dict[str, List[ResourceDef]] = defaultdict(list)
    for rname in engine.graph.resource_order:
        res = cfg.resource_map[rname]
        by_phase[res.phase_name].append(res)
    for pidx, pname in enumerate(engine.graph.phase_names):
        if pname not in by_phase:
            continue
        label = "default" if pname == DEFAULT_PHASE else pname
        pnode = root.add(f"[bold magenta]phase {pidx}: {label}[/bold magenta]")
        for res in by_phase[pname]:
            deps = engine.graph.res_depends.get(res.name, set())
            dep_txt = f"  [dim]→ {', '.join(sorted(deps))}[/dim]" if deps else ""
            rnode = pnode.add(
                f"[cyan]{res.name}[/cyan] [dim]ns={res.namespace}[/dim]{dep_txt}"
            )
            for step in res.steps:
                sdeps = f" [dim]depends: {', '.join(step.depends_on)}[/dim]" if step.depends_on else ""
                rnode.add(f"[green]{step.name}[/green] [dim]({step.kind})[/dim]{sdeps}")
    return root


def render_dot(engine: Kflow) -> str:
    lines = ["digraph kflow {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
    for pidx, pname in enumerate(engine.graph.phase_names):
        nodes = [n for n in engine.graph.node_order
                 if engine.graph.phase_idx[n] == pidx]
        if not nodes:
            continue
        label = "default" if pname == DEFAULT_PHASE else pname
        lines.append(f'  subgraph cluster_{pidx} {{ label="phase {pidx}: {label}";')
        for n in nodes:
            lines.append(f'    "{n}";')
        lines.append("  }")
    for dependent, dependency in engine.graph.edges:
        lines.append(f'  "{dependency}" -> "{dependent}";')
    lines.append("}")
    return "\n".join(lines)


def render_order(engine: Kflow) -> Table:
    table = Table(box=box.SIMPLE, title="execution order (apply)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("phase", style="magenta")
    table.add_column("resource", style="cyan")
    table.add_column("step", style="green")
    table.add_column("kind", style="dim")
    table.add_column("depends on", style="dim")
    for i, nid in enumerate(engine.graph.node_order, 1):
        res = engine.config.resource_map[engine.graph.node_res[nid]]
        step = engine.graph.node_step[nid]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        table.add_row(str(i), label, res.name, step.name, step.kind,
                      ", ".join(step.depends_on) or "-")
    return table


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@dataclass
class AppCtx:
    config_path: str
    dry_run: bool
    context: Optional[str]
    verbose: bool
    assume_yes: bool
    _engine: Optional[Kflow] = None

    def engine(self) -> Kflow:
        if self._engine is None:
            try:
                self._engine = Kflow.load(
                    self.config_path, dry_run=self.dry_run,
                    context=self.context, verbose=self.verbose,
                )
            except (ConfigError, RunnerLoadError) as exc:
                raise click.ClickException(str(exc))
            for warning in self._engine.graph.warnings:
                err_console.print(f"[yellow]warning:[/yellow] {warning}")
        return self._engine


pass_app = click.make_pass_decorator(AppCtx)


def _handle_errors(func):
    """Wrap a command body to render kflow/command errors cleanly."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (KflowError, RunnerLoadError) as exc:
            raise click.ClickException(str(exc))
        except CommandError as exc:
            raise click.ClickException(str(exc))

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="kflow")
@click.option("-c", "--config", "config_path", default="kflow.yaml",
              show_default=True, envvar="KFLOW_CONFIG",
              help="Path to the root kflow config file.")
@click.option("--dry-run", is_flag=True,
              help="Print mutating commands without executing them.")
@click.option("--context", default=None, envvar="KFLOW_CONTEXT",
              help="kubeconfig context to use (overrides config).")
@click.option("-v", "--verbose", is_flag=True, help="Show command output.")
@click.option("-y", "--yes", "assume_yes", is_flag=True,
              help="Do not prompt for confirmation.")
@click.pass_context
def cli(ctx, config_path, dry_run, context, verbose, assume_yes):
    """kflow - declarative Kubernetes workflow orchestration."""
    ctx.obj = AppCtx(config_path=config_path, dry_run=dry_run, context=context,
                     verbose=verbose, assume_yes=assume_yes)


# -- lifecycle commands ----------------------------------------------------- #


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True, help="Do not pull in dependencies of selected resources.")
@click.option("--no-wait", is_flag=True, help="Do not wait for rollouts to become ready.")
@click.option("--timeout", default=300, show_default=True, help="Rollout wait timeout (seconds).")
@pass_app
@_handle_errors
def apply(app, names, no_deps, no_wait, timeout):
    """Apply manifests and helm charts in dependency order."""
    app.engine().apply(list(names), with_deps=not no_deps,
                       wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True, help="Do not pull in dependents of selected resources.")
@click.option("--delete-namespaces", is_flag=True,
              help="Also delete namespaces (skips 'default' and keepNamespace resources).")
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def destroy(app, names, no_deps, delete_namespaces, timeout):
    """Tear down resources in reverse dependency order."""
    engine = app.engine()
    targets = engine.resolve_targets(list(names), operation="destroy",
                                     with_deps=not no_deps)
    if not _confirm(app, f"Destroy {len(targets)} resource(s): {', '.join(targets)}?"):
        return
    engine.destroy(list(names), with_deps=not no_deps,
                   delete_namespaces=delete_namespaces, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--with-deps", is_flag=True, help="Also restart dependencies.")
@click.option("--no-wait", is_flag=True)
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def restart(app, names, with_deps, no_wait, timeout):
    """Rollout-restart pods without applying any configuration."""
    app.engine().restart(list(names), with_deps=with_deps,
                         wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@click.option("--no-wait", is_flag=True)
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def reload(app, names, no_deps, no_wait, timeout):
    """Re-apply config non-destructively, then restart affected pods."""
    app.engine().reload(list(names), with_deps=not no_deps,
                        wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@pass_app
@_handle_errors
def helm(app, names, no_deps):
    """Run helm upgrade --install for helm-backed resources."""
    app.engine().helm_sync(list(names), with_deps=not no_deps)
    _done(app)


# -- inspection commands ---------------------------------------------------- #


@cli.command()
@click.argument("names", nargs=-1)
@pass_app
@_handle_errors
def status(app, names):
    """Show kflow state and live workload readiness."""
    rows = app.engine().status(list(names))
    table = Table(box=box.SIMPLE, title="status")
    for col in ("resource", "phase", "namespace", "state", "helm", "ready", "drift", "last applied"):
        table.add_column(col)
    for r in rows:
        drift = f"[yellow]{r['drift']}[/yellow]" if r["drift"] else "0"
        state_style = {"applied": "green", "destroyed": "red"}.get(r["state"], "yellow")
        table.add_row(r["name"], r["phase"], r["namespace"],
                      f"[{state_style}]{r['state']}[/{state_style}]",
                      r["helm"], r["workloads"], drift, r["last"])
    console.print(table)


@cli.command()
@click.argument("names", nargs=-1)
@pass_app
@_handle_errors
def health(app, names):
    """Check workload (and runner) health; exit non-zero if unhealthy."""
    results = app.engine().health(list(names))
    table = Table(box=box.SIMPLE, title="health")
    for col in ("resource", "namespace", "health", "detail"):
        table.add_column(col)
    unhealthy = 0
    for r in results:
        if r["healthy"] is True:
            mark = "[green]healthy[/green]"
        elif r["healthy"] is False:
            mark = "[red]unhealthy[/red]"
            unhealthy += 1
        else:
            mark = "[dim]unknown[/dim]"
        table.add_row(r["name"], r["namespace"], mark, r["detail"])
    console.print(table)
    if unhealthy:
        raise click.ClickException(f"{unhealthy} resource(s) unhealthy")


@cli.command()
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Stream logs.")
@click.option("--tail", type=int, default=None, help="Lines of recent logs to show.")
@click.option("--since", default=None, help="Show logs since e.g. 10m, 1h.")
@click.option("-c", "--container", default=None, help="Container name.")
@click.option("--selector", default=None, help="Override label selector.")
@click.option("--previous", is_flag=True, help="Show logs from a previous container.")
@pass_app
@_handle_errors
def logs(app, name, follow, tail, since, container, selector, previous):
    """Tail or fetch logs for a resource's pods."""
    result = app.engine().logs(name, follow=follow, tail=tail, since=since,
                               container=container, selector=selector,
                               previous=previous)
    if not follow and result.stdout:
        console.print(result.stdout.rstrip())
    if result.returncode != 0 and result.stderr:
        err_console.print(f"[yellow]{result.stderr.strip()}[/yellow]")


@cli.command()
@click.option("--format", "fmt", type=click.Choice(["tree", "order", "dot"]),
              default="tree", show_default=True, help="Rendering format.")
@pass_app
@_handle_errors
def graph(app, fmt):
    """Render the dependency tree / execution order."""
    engine = app.engine()
    if fmt == "tree":
        console.print(render_tree(engine))
    elif fmt == "order":
        console.print(render_order(engine))
    else:
        click.echo(render_dot(engine))


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@pass_app
@_handle_errors
def plan(app, names, no_deps):
    """Show the resolved execution order for a selection."""
    engine = app.engine()
    targets = set(engine.resolve_targets(list(names), operation="apply",
                                         with_deps=not no_deps))
    table = Table(box=box.SIMPLE, title="plan")
    for col in ("#", "phase", "resource", "step", "kind"):
        table.add_column(col)
    i = 0
    for nid in engine.graph.node_order:
        if engine.graph.node_res[nid] not in targets:
            continue
        i += 1
        res = engine.config.resource_map[engine.graph.node_res[nid]]
        step = engine.graph.node_step[nid]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        table.add_row(str(i), label, res.name, step.name, step.kind)
    console.print(table)


@cli.command(name="list")
@pass_app
@_handle_errors
def list_(app):
    """List phases and resources."""
    engine = app.engine()
    table = Table(box=box.SIMPLE, title="resources")
    for col in ("resource", "phase", "namespace", "steps", "depends on"):
        table.add_column(col)
    for rname in engine.graph.resource_order:
        res = engine.config.resource_map[rname]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        deps = ", ".join(sorted(engine.graph.res_depends.get(rname, set()))) or "-"
        table.add_row(rname, label, res.namespace,
                      ", ".join(s.name for s in res.steps), deps)
    console.print(table)


@cli.command()
@pass_app
@_handle_errors
def validate(app):
    """Validate configuration and report warnings."""
    engine = app.engine()
    console.print(
        f"[green]✓[/green] config OK: {len(engine.config.resources)} resources, "
        f"{len([p for p in engine.graph.phase_names if p != DEFAULT_PHASE])} declared phases, "
        f"{len(engine.graph.node_order)} steps"
    )
    missing = []
    for res in engine.config.resources:
        for step in res.steps:
            for m in step.manifests:
                if _is_url(m):
                    continue  # can't check remote URLs at validate time
                if not Path(m).exists():
                    missing.append(str(m))
            if step.kind == "kustomize" and step.kustomize:
                if not step.kustomize.path.exists():
                    missing.append(str(step.kustomize.path))
            if step.kind == "docker-build" and step.docker_build:
                if not step.docker_build.context.exists():
                    missing.append(str(step.docker_build.context))
    if missing:
        for m in missing:
            err_console.print(f"[yellow]warning:[/yellow] path not found: {m}")
    if engine.graph.warnings:
        for w in engine.graph.warnings:
            err_console.print(f"[yellow]warning:[/yellow] {w}")
    elif not missing:
        console.print("[green]✓[/green] no warnings")


@cli.command()
@pass_app
@_handle_errors
def runners(app):
    """List custom runners discovered from the configuration."""
    engine = app.engine()
    table = Table(box=box.SIMPLE, title="runners")
    for col in ("name", "description", "source"):
        table.add_column(col)
    for name, cls in engine.registry.items():
        src = getattr(cls, "__kflow_source__", "?")
        table.add_row(name, cls.description or "-", str(src))
    if not engine.registry.items():
        console.print("[dim]no runners registered[/dim]")
    else:
        console.print(table)


@cli.group()
def state():
    """Inspect or manage local kflow state."""


@state.command("show")
@pass_app
@_handle_errors
def state_show(app):
    """Print the local state for the active cluster."""
    engine = app.engine()
    console.print(Panel(
        json.dumps(engine.state.cluster, indent=2),
        title=f"state: {engine.state.path} [{engine.state.cluster_key}]",
        border_style="dim",
    ))


@state.command("path")
@pass_app
@_handle_errors
def state_path(app):
    """Print the path to the state file."""
    click.echo(str(app.engine().state.path))


@state.command("clear")
@pass_app
@_handle_errors
def state_clear(app):
    """Clear local state for the active cluster."""
    engine = app.engine()
    if not _confirm(app, f"Clear state for cluster {engine.state.cluster_key!r}?"):
        return
    engine.state.clear()
    console.print("[green]✓[/green] state cleared")


# -- helpers ---------------------------------------------------------------- #


def _confirm(app: AppCtx, message: str) -> bool:
    if app.assume_yes or app.dry_run:
        return True
    return click.confirm(message, default=False)


def _done(app: AppCtx) -> None:
    if app.dry_run:
        console.print("[dim](dry-run: no changes were made)[/dim]")
    else:
        console.print("[green]✓ done[/green]")


def _engine_banner(self: Kflow, op: str, targets: List[str]) -> None:
    mode = " [yellow](dry-run)[/yellow]" if self.dry_run else ""
    self.console.rule(f"[bold]{op}[/bold]{mode} [dim]{len(targets)} resource(s)[/dim]")


Kflow._banner = _engine_banner  # attach as method


def main(argv=None) -> int:
    """Console-script entry point."""
    try:
        cli.main(args=argv, prog_name="kflow", standalone_mode=False)
        return 0
    except click.ClickException as exc:
        err_console.print(f"[red]error:[/red] {exc.format_message()}")
        return 1
    except click.exceptions.Abort:
        err_console.print("[red]aborted[/red]")
        return 130
    except KflowError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
