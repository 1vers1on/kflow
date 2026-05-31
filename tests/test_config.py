from __future__ import annotations

import pytest

from kflow.core import (
    ConfigError,
    detect_doc_type,
    is_kflow_doc,
    load_root_config,
)

from .conftest import ROOT_CONFIG


def test_identifies_kflow_files_vs_manifests():
    config_doc = {"kflow": {"version": "v1", "kind": "Config"}}
    resource_doc = {"kflow": {"version": "v1", "kind": "ResourceDefinition"}}
    manifest_doc = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {}}

    assert is_kflow_doc(config_doc)
    assert not is_kflow_doc(manifest_doc)
    assert detect_doc_type(config_doc) == "config"
    assert detect_doc_type(resource_doc) == "resource"
    assert detect_doc_type(manifest_doc) == "manifest"
    assert detect_doc_type({"random": True}) == "unknown"


def test_loads_example_config():
    cfg = load_root_config(ROOT_CONFIG)
    names = {r.name for r in cfg.resources}
    assert names == {"longhorn-storage", "traefik", "longhorn-ingress", "app"}
    assert [p.name for p in cfg.phases] == [
        "storage", "ingress-controller", "ingress", "apps",
    ]
    # runner file registered globally
    assert any(p.name.endswith("db_runner.py") for p in cfg.runner_files)


def test_namespace_declared_on_resource_not_manifest():
    cfg = load_root_config(ROOT_CONFIG)
    rmap = cfg.resource_map
    assert rmap["longhorn-storage"].namespace == "longhorn-system"
    assert rmap["app"].namespace == "demo"


def test_helm_and_step_parsing():
    cfg = load_root_config(ROOT_CONFIG)
    longhorn = cfg.resource_map["longhorn-storage"]
    assert longhorn.helm is not None
    assert longhorn.helm.chart == "longhorn/longhorn"
    assert longhorn.helm.repo_url == "https://charts.longhorn.io"
    assert longhorn.helm.namespace == "longhorn-system"

    app = cfg.resource_map["app"]
    step_names = [s.name for s in app.steps]
    assert step_names == ["config", "deploy", "wait-ready", "migrate"]
    migrate = app.steps[-1]
    assert migrate.kind == "runner"
    assert migrate.runner.class_name == "DatabaseRunner"

    wait_step = app.steps[2]
    assert wait_step.kind == "wait"
    assert wait_step.wait.for_resource == "deployment/web"
    assert wait_step.wait.condition == "available"


def test_missing_config_raises():
    with pytest.raises(ConfigError):
        load_root_config("/nonexistent/kflow.yaml")
