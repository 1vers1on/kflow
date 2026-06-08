"""YAML helpers and configuration loader."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

import yaml

from .models import (
    ConfigError,
    ConfigMapSpec,
    DEFAULT_PHASE,
    DockerBuildSpec,
    ExecSpec,
    HelmSpec,
    KIND_CONFIG,
    KIND_RESOURCE,
    KFLOW_KEY,
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
# Step parsers
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


_VALID_ROLLOUT_KINDS = {"deployment", "statefulset", "daemonset", "replicaset"}


def _parse_rollout_wait(spec: dict, resource_name: str) -> RolloutWaitSpec:
    raw_kinds = spec.get("kinds")
    if raw_kinds:
        kinds = list(raw_kinds)
        bad = {k.lower() for k in kinds} - _VALID_ROLLOUT_KINDS
        if bad:
            raise ConfigError(
                f"rollout-wait for {resource_name!r} contains unsupported kinds: "
                f"{sorted(bad)}. Valid kinds: {sorted(_VALID_ROLLOUT_KINDS)}"
            )
    else:
        kinds = ["deployment", "statefulset", "daemonset"]
    return RolloutWaitSpec(
        kinds=kinds,
        namespace=spec.get("namespace"),
        selector=spec.get("selector"),
        timeout=int(spec.get("timeout", 300)),
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


def _parse_namespace(spec: dict, resource_name: str) -> NamespaceSpec:
    return NamespaceSpec(
        name=spec.get("name"),
        labels=dict(spec.get("labels") or {}),
        annotations=dict(spec.get("annotations") or {}),
        if_not_exists=bool(spec.get("ifNotExists", False)),
        delete_on_destroy=bool(spec.get("deleteOnDestroy", False)),
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
    ns_raw = spec.get("namespace")
    ns_override = str(ns_raw) if isinstance(ns_raw, str) and ns_raw else None
    no_ns = bool(spec.get("noNamespace", False))
    server_side = bool(spec.get("serverSide", False))
    common = {"namespace": ns_override, "no_namespace": no_ns, "server_side": server_side}
    if spec.get("manifests"):
        return StepDef(name=name, kind="manifest", depends_on=depends_on,
                       manifests=_parse_manifests(spec["manifests"], base), **common)
    if spec.get("helm"):
        return StepDef(name=name, kind="helm", depends_on=depends_on,
                       helm=_parse_helm(spec["helm"], default_ns, base, resource_name), **common)
    if spec.get("kustomize"):
        return StepDef(name=name, kind="kustomize", depends_on=depends_on,
                       kustomize=_parse_kustomize(spec["kustomize"], base, resource_name), **common)
    if spec.get("wait"):
        return StepDef(name=name, kind="wait", depends_on=depends_on,
                       wait=_parse_wait(spec["wait"], resource_name), **common)
    if spec.get("rolloutWait") is not None:
        raw = spec["rolloutWait"] if isinstance(spec["rolloutWait"], dict) else {}
        return StepDef(name=name, kind="rollout-wait", depends_on=depends_on,
                       rollout_wait=_parse_rollout_wait(raw, resource_name), **common)
    if spec.get("script"):
        return StepDef(name=name, kind="script", depends_on=depends_on,
                       script=_parse_script(spec["script"], base, resource_name), **common)
    if spec.get("runner"):
        return StepDef(name=name, kind="runner", depends_on=depends_on,
                       runner=_parse_runner(spec["runner"], base, resource_name), **common)
    if spec.get("secret"):
        return StepDef(name=name, kind="secret", depends_on=depends_on,
                       secret=_parse_secret(spec["secret"], base, resource_name, name), **common)
    if spec.get("configmap"):
        return StepDef(name=name, kind="configmap", depends_on=depends_on,
                       configmap=_parse_configmap(spec["configmap"], base, resource_name, name), **common)
    if spec.get("exec"):
        return StepDef(name=name, kind="exec", depends_on=depends_on,
                       exec_spec=_parse_exec(spec["exec"], resource_name), **common)
    if spec.get("dockerBuild"):
        return StepDef(name=name, kind="docker-build", depends_on=depends_on,
                       docker_build=_parse_docker_build(spec["dockerBuild"], base, resource_name), **common)
    if "createNamespace" in spec:
        raw = spec["createNamespace"] if isinstance(spec["createNamespace"], dict) else {}
        return StepDef(name=name, kind="create-namespace", depends_on=depends_on,
                       namespace_spec=_parse_namespace(raw, resource_name), **common)
    raise ConfigError(
        f"step {name!r} in {resource_name!r} must define one of: "
        "manifests, helm, kustomize, wait, rolloutWait, script, runner, "
        "secret, configmap, exec, dockerBuild, createNamespace"
    )


# --------------------------------------------------------------------------- #
# Resource and root config loading
# --------------------------------------------------------------------------- #


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

    acn_raw = doc.get("autoCreateNamespace")
    auto_create_ns = None if acn_raw is None else bool(acn_raw)
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
        auto_create_namespace=auto_create_ns,
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
        auto_create_namespace=bool(doc.get("autoCreateNamespace", False)),
    )
