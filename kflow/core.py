"""Backward-compatible re-export shim.

All symbols that used to live in this single module are now split across
dedicated sub-modules. This file re-exports everything so that existing
``from kflow.core import ...`` statements continue to work unchanged.
"""

from .cli import AppCtx, cli, main  # noqa: F401
from .engine import Kflow  # noqa: F401
from .graph import DependencyGraph  # noqa: F401
from .loader import (  # noqa: F401
    _is_url,
    _parse_configmap,
    _parse_docker_build,
    _parse_exec,
    _parse_helm,
    _parse_kustomize,
    _parse_manifests,
    _parse_namespace,
    _parse_rollout_wait,
    _parse_runner,
    _parse_script,
    _parse_secret,
    _parse_step,
    _parse_wait,
    _resolve,
    detect_doc_type,
    file_hash,
    is_kflow_doc,
    kflow_kind,
    load_root_config,
    now_iso,
)
from .models import (  # noqa: F401
    ConfigError,
    ConfigMapSpec,
    DEFAULT_PHASE,
    DockerBuildSpec,
    ExecSpec,
    HelmSpec,
    KIND_CONFIG,
    KIND_RESOURCE,
    KFLOW_KEY,
    KflowError,
    KustomizeSpec,
    NamespaceSpec,
    PhaseDef,
    ResourceDef,
    RolloutWaitSpec,
    RootConfig,
    RunnerSpec,
    ScriptSpec,
    SecretSpec,
    StepDef,
    WaitSpec,
)
from .render import render_dot, render_order, render_tree  # noqa: F401
from .runners.shell import run_command  # noqa: F401
from .state import StateManager  # noqa: F401

__version__ = "v1.1.4"
