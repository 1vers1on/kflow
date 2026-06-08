"""Additional dependency-graph edge cases beyond the example config."""

from __future__ import annotations

import pytest
import yaml

from kflow.graph import DependencyGraph
from kflow.loader import load_root_config
from kflow.models import ConfigError


def _project(tmp_path, resources, phases=None, config_extra=None):
    (tmp_path / "m.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n"
    )
    names = []
    for r in resources:
        r = {"kflow": {"version": "v1", "kind": "ResourceDefinition"}, **r}
        fname = f"{r['name']}.yaml"
        (tmp_path / fname).write_text(yaml.safe_dump(r))
        names.append(fname)
    cfg = {"kflow": {"version": "v1", "kind": "Config"}, "resources": names}
    if phases:
        cfg["phases"] = phases
    if config_extra:
        cfg.update(config_extra)
    (tmp_path / "kflow.yaml").write_text(yaml.safe_dump(cfg))
    return load_root_config(tmp_path / "kflow.yaml")


def test_bare_dep_resolves_to_resource_last_node(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "one", "manifests": ["m.yaml"]},
                   {"name": "two", "manifests": ["m.yaml"]}]},
        {"name": "b", "namespace": "x", "dependsOn": ["a"],
         "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
    ])
    graph = DependencyGraph(cfg)
    order = graph.node_order
    # b.go waits for the *last* step of a
    assert order.index("a.two") < order.index("b.go")


def test_qualified_step_dependency(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "one", "manifests": ["m.yaml"]}]},
        {"name": "b", "namespace": "x",
         "steps": [{"name": "go", "manifests": ["m.yaml"],
                    "dependsOn": ["a.one"]}]},
    ])
    graph = DependencyGraph(cfg)
    assert graph.node_order.index("a.one") < graph.node_order.index("b.go")


def test_unknown_qualified_step_raises(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "go", "manifests": ["m.yaml"],
                    "dependsOn": ["a.ghost"]}]},
    ])
    with pytest.raises(ConfigError, match="unknown step"):
        DependencyGraph(cfg)


def test_unknown_bare_dependency_raises(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "go", "manifests": ["m.yaml"],
                    "dependsOn": ["ghost"]}]},
    ])
    with pytest.raises(ConfigError, match="unknown step/resource"):
        DependencyGraph(cfg)


def test_forward_cross_phase_dependency_is_ignored_with_warning(tmp_path):
    # a (phase p1) depends on b (phase p2, later) -> forward dep, ignored
    cfg = _project(
        tmp_path,
        [
            {"name": "a", "namespace": "x", "phase": "p1", "dependsOn": ["b"],
             "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
            {"name": "b", "namespace": "x", "phase": "p2",
             "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
        ],
        phases=[{"name": "p1"}, {"name": "p2"}],
    )
    graph = DependencyGraph(cfg)
    # phase order wins: a still runs before b
    assert graph.node_order.index("a.go") < graph.node_order.index("b.go")
    assert any("later phase" in w for w in graph.warnings)


def test_resource_with_no_steps_used_as_dependency_raises(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "empty", "namespace": "x", "steps": []},
        {"name": "b", "namespace": "x", "dependsOn": ["empty"],
         "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
    ])
    with pytest.raises(ConfigError, match="no steps"):
        DependencyGraph(cfg)


def test_closure_dependents_direction(tmp_path):
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
        {"name": "b", "namespace": "x", "dependsOn": ["a"],
         "steps": [{"name": "go", "manifests": ["m.yaml"]}]},
    ])
    graph = DependencyGraph(cfg)
    # b depends on a
    assert "a" in graph.closure({"b"}, dependents=False)
    assert "b" in graph.closure({"a"}, dependents=True)


def test_duplicate_edges_deduplicated(tmp_path):
    # an explicit dependsOn that coincides with the implicit sequential edge
    cfg = _project(tmp_path, [
        {"name": "a", "namespace": "x",
         "steps": [{"name": "one", "manifests": ["m.yaml"]},
                   {"name": "two", "manifests": ["m.yaml"],
                    "dependsOn": ["one"]}]},
    ])
    graph = DependencyGraph(cfg)
    assert graph.edges.count(("a.two", "a.one")) == 1
