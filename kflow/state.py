"""Local JSON state tracking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .loader import _is_url, file_hash, now_iso
from .models import ResourceDef


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
            elif step.kind == "rollout-wait":
                steps[step.name] = {"kind": "rollout-wait"}
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
