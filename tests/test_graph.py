from __future__ import annotations

from pathlib import Path

import yaml

from kflow.core import DependencyGraph, load_root_config

from .conftest import ROOT_CONFIG


def _resource_positions(graph):
    """First index at which each resource appears in the node order."""
    pos = {}
    for i, nid in enumerate(graph.node_order):
        r = graph.node_res[nid]
        pos.setdefault(r, i)
    return pos


def test_phase_ordering_solves_longhorn_traefik():
    graph = DependencyGraph(load_root_config(ROOT_CONFIG))
    pos = _resource_positions(graph)
    # storage before controller before ingress before app
    assert pos["longhorn-storage"] < pos["traefik"]
    assert pos["traefik"] < pos["longhorn-ingress"]
    assert pos["longhorn-ingress"] < pos["app"]


def test_strict_phase_boundaries():
    graph = DependencyGraph(load_root_config(ROOT_CONFIG))
    # every node's phase index is non-decreasing along the order
    phases = [graph.phase_idx[n] for n in graph.node_order]
    assert phases == sorted(phases)


def test_step_level_order_within_resource():
    graph = DependencyGraph(load_root_config(ROOT_CONFIG))
    order = graph.node_order
    assert order.index("app.config") < order.index("app.deploy")
    assert order.index("app.deploy") < order.index("app.wait-ready")
    assert order.index("app.wait-ready") < order.index("app.migrate")


def test_cross_resource_step_dependency():
    graph = DependencyGraph(load_root_config(ROOT_CONFIG))
    order = graph.node_order
    # app.deploy depends on longhorn-ingress
    assert order.index("longhorn-ingress.apply") < order.index("app.deploy")


def test_resource_closure_for_targeting():
    graph = DependencyGraph(load_root_config(ROOT_CONFIG))
    # applying just longhorn-ingress should pull in traefik (+ longhorn-storage)
    deps = graph.closure({"longhorn-ingress"}, dependents=False)
    assert {"traefik", "longhorn-storage"} <= deps
    # destroying traefik should pull in its dependents
    dependents = graph.closure({"traefik"}, dependents=True)
    assert "longhorn-ingress" in dependents


def test_circular_dependency_is_broken_not_raised(tmp_path):
    """Same-phase cycles must not raise; they are broken with a warning."""
    base = tmp_path
    (base / "a.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "a", "namespace": "x", "phase": "p",
        "dependsOn": ["b"],
        "steps": [{"name": "apply", "manifests": ["m.yaml"]}],
    }))
    (base / "b.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "b", "namespace": "x", "phase": "p",
        "dependsOn": ["a"],
        "steps": [{"name": "apply", "manifests": ["m.yaml"]}],
    }))
    (base / "m.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n")
    (base / "kflow.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "Config"},
        "phases": [{"name": "p"}],
        "resources": ["a.yaml", "b.yaml"],
    }))

    graph = DependencyGraph(load_root_config(base / "kflow.yaml"))
    assert len(graph.node_order) == 2
    assert any("circular" in w for w in graph.warnings)


def test_default_phase_when_none_declared(tmp_path):
    base = tmp_path
    (base / "m.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n")
    (base / "a.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "a", "namespace": "x",
        "steps": [{"name": "apply", "manifests": ["m.yaml"]}],
    }))
    (base / "kflow.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "Config"},
        "resources": ["a.yaml"],
    }))
    graph = DependencyGraph(load_root_config(base / "kflow.yaml"))
    assert graph.node_order == ["a.apply"]
