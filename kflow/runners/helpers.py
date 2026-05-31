"""Convenience helpers for authoring custom runners.

These are optional sugar - a runner can do everything through
``ctx.kube``/``ctx.kubectl`` - but they cover the most common project-specific
chores (rendering ConfigMaps/Secrets, base64, simple polling).
"""

from __future__ import annotations

import base64
import time
from typing import Callable, Mapping, Optional

import yaml


def b64(value: str) -> str:
    """Base64-encode a string (for Secret ``data`` fields)."""
    return base64.b64encode(value.encode()).decode()


def configmap_manifest(name: str, namespace: str, data: Mapping[str, str],
                       labels: Optional[Mapping[str, str]] = None) -> str:
    """Render a ConfigMap manifest as YAML."""
    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(name, namespace, labels),
        "data": {k: str(v) for k, v in data.items()},
    }
    return yaml.safe_dump(doc, sort_keys=False)


def secret_manifest(name: str, namespace: str, data: Mapping[str, str],
                    *, string_data: bool = True,
                    labels: Optional[Mapping[str, str]] = None) -> str:
    """Render a Secret manifest as YAML.

    With ``string_data`` (default) values are passed through ``stringData`` so
    the caller does not have to base64-encode; otherwise values are encoded
    into ``data``.
    """
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _metadata(name, namespace, labels),
        "type": "Opaque",
    }
    if string_data:
        doc["stringData"] = {k: str(v) for k, v in data.items()}
    else:
        doc["data"] = {k: b64(str(v)) for k, v in data.items()}
    return yaml.safe_dump(doc, sort_keys=False)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 120.0,
             interval: float = 3.0, description: str = "condition") -> bool:
    """Poll ``predicate`` until it returns True or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _metadata(name: str, namespace: str,
              labels: Optional[Mapping[str, str]]) -> dict:
    meta = {"name": name, "namespace": namespace}
    merged = {"app.kubernetes.io/managed-by": "kflow"}
    if labels:
        merged.update(labels)
    meta["labels"] = merged
    return meta
