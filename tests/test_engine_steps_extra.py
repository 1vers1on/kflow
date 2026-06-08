"""Coverage for script execution and fromCommand-driven secret/configmap steps."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kflow.core import Kflow, load_root_config
from kflow.models import KflowError


def _project(tmp_path, step, *, namespace="ns"):
    (tmp_path / "m.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n"
    )
    (tmp_path / "r.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "r", "namespace": namespace,
        "steps": [step],
    }))
    (tmp_path / "kflow.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "Config"},
        "state": {"dir": str(tmp_path / "state")},
        "resources": ["r.yaml"],
    }))
    cfg = load_root_config(tmp_path / "kflow.yaml")
    return cfg


def _cmds(recorder):
    return [" ".join(c["cmd"]) for c in recorder]


# -- script step ------------------------------------------------------------ #


def test_script_apply_runs_command(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s", "script": {"run": "echo hi"}})
    Kflow(cfg).apply(wait=False)
    assert any(c["cmd"][:2] == ["sh", "-c"] and "echo hi" in c["cmd"][2]
               for c in recorder)


def test_script_destroy_runs_on_destroy(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s",
                              "script": {"run": "echo up", "onDestroy": "echo down"}})
    Kflow(cfg).destroy(delete_namespaces=False)
    assert any("echo down" in c["cmd"][-1] for c in recorder if len(c["cmd"]) == 3)


def test_script_destroy_noop_without_on_destroy(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s", "script": {"run": "echo up"}})
    Kflow(cfg).destroy(delete_namespaces=False)
    assert not any("echo up" in c["cmd"][-1] for c in recorder if len(c["cmd"]) == 3)


def test_script_reload_falls_back_to_run(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s", "script": {"run": "echo run"}})
    Kflow(cfg).reload(wait=False)
    assert any("echo run" in c["cmd"][-1] for c in recorder if len(c["cmd"]) == 3)


def test_script_reload_uses_on_reload(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s",
                              "script": {"run": "echo run", "onReload": "echo reload"}})
    Kflow(cfg).reload(wait=False)
    cmds3 = [c["cmd"][-1] for c in recorder if len(c["cmd"]) == 3]
    assert any("echo reload" in c for c in cmds3)
    assert not any("echo run" in c for c in cmds3)


def test_script_dry_run_does_not_execute(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "s", "script": {"run": "echo hi"}})
    Kflow(cfg, dry_run=True).apply(wait=False)
    assert not any(c["cmd"][:2] == ["sh", "-c"] for c in recorder)


# -- secret fromCommand ----------------------------------------------------- #


def test_secret_from_command_populates_literal(tmp_path, monkeypatch):
    cfg = _project(tmp_path, {
        "name": "tok",
        "secret": {"fromCommand": {"TOKEN": "printf abc123"}},
    })
    engine = Kflow(cfg)
    captured = {}
    monkeypatch.setattr(
        engine.kube, "secret_apply",
        lambda name, ns, *, literals=None, from_files=None, from_env_file=None:
            captured.update(literals or {}),
    )
    engine.apply(wait=False)
    assert captured["TOKEN"] == "abc123"


def test_secret_from_command_failure_raises(tmp_path):
    cfg = _project(tmp_path, {
        "name": "tok",
        "secret": {"fromCommand": {"TOKEN": "exit 7"}},
    })
    with pytest.raises(KflowError, match="fromCommand"):
        Kflow(cfg).apply(wait=False)


# -- configmap destroy ------------------------------------------------------ #


def test_configmap_destroy_deletes(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "cm", "configmap": {"literals": {"a": "b"}}})
    Kflow(cfg).destroy(delete_namespaces=False)
    assert any("delete configmap cm" in c for c in _cmds(recorder))


def test_configmap_if_not_exists_not_deleted(tmp_path, recorder):
    cfg = _project(tmp_path, {"name": "cm",
                              "configmap": {"literals": {"a": "b"}, "ifNotExists": True}})
    Kflow(cfg).destroy(delete_namespaces=False)
    assert not any("delete configmap" in c for c in _cmds(recorder))
