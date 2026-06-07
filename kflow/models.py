"""Data model: constants, exceptions, and configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

# Top-level identifier block key. Its presence marks a file as a kflow file.
KFLOW_KEY = "kflow"
KIND_CONFIG = "Config"
KIND_RESOURCE = "ResourceDefinition"
DEFAULT_PHASE = "__default__"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class KflowError(Exception):
    """Base error for user-facing kflow failures."""


class ConfigError(KflowError):
    """Raised when configuration is invalid (bad schema, missing refs, …)."""


# --------------------------------------------------------------------------- #
# Configuration dataclasses
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
class RolloutWaitSpec:
    """Wait for all rollouts of specific workload kinds to complete.

    Equivalent to running ``kubectl rollout status`` on every deployment,
    statefulset, and daemonset in the namespace (optionally filtered by
    selector).
    """
    kinds: List[str] = field(default_factory=lambda: ["deployment", "statefulset", "daemonset"])
    namespace: Optional[str] = None
    selector: Optional[str] = None
    timeout: int = 300


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
class NamespaceSpec:
    """Explicitly create (and optionally delete) a Kubernetes namespace."""
    name: Optional[str] = None          # defaults to the resource namespace
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    if_not_exists: bool = False         # skip if already exists
    delete_on_destroy: bool = False     # delete on destroy (opt-in, destructive)


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
    kind: str  # manifest | helm | kustomize | wait | rollout-wait | script | runner |
               # secret | configmap | exec | docker-build | create-namespace
    depends_on: List[str] = field(default_factory=list)
    manifests: List[Union[Path, str]] = field(default_factory=list)  # Path or URL
    helm: Optional[HelmSpec] = None
    kustomize: Optional[KustomizeSpec] = None
    wait: Optional[WaitSpec] = None
    rollout_wait: Optional[RolloutWaitSpec] = None
    script: Optional[ScriptSpec] = None
    runner: Optional[RunnerSpec] = None
    secret: Optional[SecretSpec] = None
    configmap: Optional[ConfigMapSpec] = None
    exec_spec: Optional[ExecSpec] = None
    docker_build: Optional[DockerBuildSpec] = None
    namespace_spec: Optional[NamespaceSpec] = None
    namespace: Optional[str] = None   # override resource namespace for this step
    no_namespace: bool = False         # skip -n flag (cluster-scoped resources)


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
    auto_create_namespace: Optional[bool] = None  # None = inherit from root config
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
    auto_create_namespace: bool = False  # when True, create missing namespaces on apply/reload

    @property
    def resource_map(self) -> Dict[str, ResourceDef]:
        return {r.name: r for r in self.resources}
