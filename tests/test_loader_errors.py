"""Validation/error-path coverage for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kflow.loader import (
    _is_url,
    _resolve,
    file_hash,
    load_root_config,
    now_iso,
)
from kflow.models import ConfigError


def _write_config(base: Path, resources, **extra):
    doc = {"kflow": {"version": "v1", "kind": "Config"}, "resources": resources}
    doc.update(extra)
    (base / "kflow.yaml").write_text(yaml.safe_dump(doc))
    return base / "kflow.yaml"


def _write_resource(base: Path, name, fname, steps, **extra):
    doc = {
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": name,
        "namespace": "ns",
        "steps": steps,
    }
    doc.update(extra)
    (base / fname).write_text(yaml.safe_dump(doc))
    return base / fname


# -- root config errors ----------------------------------------------------- #


def test_missing_root_config(tmp_path):
    with pytest.raises(ConfigError, match="root config not found"):
        load_root_config(tmp_path / "kflow.yaml")


def test_root_must_be_config_kind(tmp_path):
    p = tmp_path / "kflow.yaml"
    p.write_text("apiVersion: v1\nkind: Deployment\n")
    with pytest.raises(ConfigError, match="not a kflow Config"):
        load_root_config(p)


def test_no_resources_listed(tmp_path):
    p = _write_config(tmp_path, [])
    with pytest.raises(ConfigError, match="lists no resources"):
        load_root_config(p)


def test_resource_path_not_found(tmp_path):
    p = _write_config(tmp_path, ["ghost.yaml"])
    with pytest.raises(ConfigError, match="resource path not found"):
        load_root_config(p)


def test_empty_resource_directory(tmp_path):
    (tmp_path / "res").mkdir()
    p = _write_config(tmp_path, ["res"])
    with pytest.raises(ConfigError, match="no YAML files"):
        load_root_config(p)


def test_directory_of_resources_loaded(tmp_path):
    d = tmp_path / "res"
    d.mkdir()
    (tmp_path / "m.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n")
    _write_resource(d, "a", "a.yaml", [{"name": "x", "manifests": ["../m.yaml"]}])
    _write_resource(d, "b", "b.yaml", [{"name": "x", "manifests": ["../m.yaml"]}])
    cfg = load_root_config(_write_config(tmp_path, ["res"]))
    assert {r.name for r in cfg.resources} == {"a", "b"}


def test_duplicate_resource_name(tmp_path):
    _write_resource(tmp_path, "dup", "a.yaml", [{"name": "x", "script": {"run": "true"}}])
    _write_resource(tmp_path, "dup", "b.yaml", [{"name": "x", "script": {"run": "true"}}])
    p = _write_config(tmp_path, ["a.yaml", "b.yaml"])
    with pytest.raises(ConfigError, match="duplicate resource name"):
        load_root_config(p)


def test_non_resource_document_in_resource_file(tmp_path):
    (tmp_path / "a.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n")
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="non-resource document"):
        load_root_config(p)


def test_invalid_phase_entry(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"name": "x", "script": {"run": "true"}}])
    p = _write_config(tmp_path, ["a.yaml"], phases=[123])
    with pytest.raises(ConfigError, match="invalid phase entry"):
        load_root_config(p)


def test_unknown_phase_referenced(tmp_path):
    # Phase validity is enforced when the dependency graph is built, not by the
    # bare loader, so go through DependencyGraph here.
    from kflow.graph import DependencyGraph
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "script": {"run": "true"}}], phase="ghost")
    p = _write_config(tmp_path, ["a.yaml"], phases=[{"name": "real"}])
    cfg = load_root_config(p)
    with pytest.raises(ConfigError, match="unknown phase"):
        DependencyGraph(cfg)


def test_invalid_yaml(tmp_path):
    p = tmp_path / "kflow.yaml"
    p.write_text("kflow: {version: v1, kind: Config\nresources: [")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_root_config(p)


# -- resource / step errors ------------------------------------------------- #


def test_resource_missing_name(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "steps:\n  - name: x\n    script:\n      run: 'true'\n"
    )
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'name'"):
        load_root_config(p)


def test_step_missing_name(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"script": {"run": "true"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'name'"):
        load_root_config(p)


def test_step_with_no_recognized_kind(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"name": "x", "bogus": True}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="must define one of"):
        load_root_config(p)


def test_duplicate_step_names(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "script": {"run": "true"}},
                     {"name": "x", "script": {"run": "true"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="duplicate step names"):
        load_root_config(p)


def test_helm_missing_chart(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"name": "x", "helm": {"release": "r"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'chart'"):
        load_root_config(p)


def test_runner_missing_class(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"name": "x", "runner": {"config": {}}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'class'"):
        load_root_config(p)


def test_kustomize_missing_path(tmp_path):
    # non-empty (truthy) block so the kind is detected, but 'path' is absent
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "kustomize": {"other": "v"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'path'"):
        load_root_config(p)


def test_wait_missing_for(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "wait": {"condition": "available"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'for'"):
        load_root_config(p)


def test_script_missing_run(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "script": {"onDestroy": "cleanup"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'run'"):
        load_root_config(p)


def test_exec_missing_command(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml", [{"name": "x", "exec": {"pod": "p"}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'command'"):
        load_root_config(p)


def test_docker_build_missing_tag(tmp_path):
    _write_resource(tmp_path, "a", "a.yaml",
                    [{"name": "x", "dockerBuild": {"context": "."}}])
    p = _write_config(tmp_path, ["a.yaml"])
    with pytest.raises(ConfigError, match="missing 'tag'"):
        load_root_config(p)


# -- small helpers ---------------------------------------------------------- #


def test_is_url():
    assert _is_url("https://x") and _is_url("http://x")
    assert not _is_url("/local/path")


def test_resolve_absolute_and_relative(tmp_path):
    assert _resolve(tmp_path, "/abs/path") == Path("/abs/path")
    assert _resolve(tmp_path, "rel").is_absolute()


def test_resolve_expands_user(tmp_path):
    assert str(_resolve(tmp_path, "~/x")).startswith(str(Path.home()))


def test_file_hash_stable_and_none_for_url(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("hello")
    h1 = file_hash(f)
    h2 = file_hash(f)
    assert h1 == h2 and len(h1) == 16
    assert file_hash("https://example.com/x.yaml") is None
    assert file_hash(tmp_path / "missing") is None


def test_now_iso_format():
    s = now_iso()
    assert s.endswith("Z") and "T" in s and len(s) == 20
