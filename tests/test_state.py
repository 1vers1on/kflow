"""Unit tests for the local JSON state manager."""

from __future__ import annotations

from pathlib import Path

from kflow.models import (
    HelmSpec,
    ResourceDef,
    SecretSpec,
    StepDef,
)
from kflow.state import StateManager


def _manifest_resource(tmp_path) -> ResourceDef:
    m = tmp_path / "m.yaml"
    m.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n")
    step = StepDef(name="apply", kind="manifest", manifests=[m])
    return ResourceDef(name="r", namespace="ns", phase=None, steps=[step])


def test_load_missing_returns_default(tmp_path):
    sm = StateManager(tmp_path, "ctx")
    assert sm.data == {"version": 1, "clusters": {}}
    assert sm.all() == {}


def test_record_apply_and_get(tmp_path):
    sm = StateManager(tmp_path / "s", "ctx")
    res = _manifest_resource(tmp_path)
    sm.record_apply(res)
    entry = sm.get("r")
    assert entry["status"] == "applied"
    assert entry["last_operation"] == "apply"
    assert entry["namespace"] == "ns"
    assert "apply" in entry["steps"]
    assert entry["steps"]["apply"]["kind"] == "manifest"
    # the manifest hash is recorded
    assert list(entry["steps"]["apply"]["manifests"].values())[0]


def test_record_apply_records_helm_release(tmp_path):
    helm = HelmSpec(release="rel", chart="c", namespace="ns")
    step = StepDef(name="install", kind="helm", helm=helm)
    res = ResourceDef(name="r", namespace="ns", phase=None, steps=[step])
    sm = StateManager(tmp_path, "ctx")
    sm.record_apply(res)
    entry = sm.get("r")
    assert entry["helm_release"] == "rel"
    assert entry["steps"]["install"] == {"kind": "helm", "release": "rel"}


def test_record_apply_secret_uses_step_name_when_unnamed(tmp_path):
    step = StepDef(name="tok", kind="secret", secret=SecretSpec())
    res = ResourceDef(name="r", namespace="ns", phase=None, steps=[step])
    sm = StateManager(tmp_path, "ctx")
    sm.record_apply(res)
    assert sm.get("r")["steps"]["tok"]["name"] == "tok"


def test_record_operation_marks_destroyed(tmp_path):
    sm = StateManager(tmp_path, "ctx")
    sm.record_operation("r", "destroy")
    entry = sm.get("r")
    assert entry["status"] == "destroyed"
    assert entry["last_operation"] == "destroy"
    assert "last_destroy" in entry


def test_save_and_reload_roundtrip(tmp_path):
    sm = StateManager(tmp_path / "s", "ctx")
    sm.record_apply(_manifest_resource(tmp_path))
    sm.save()
    assert sm.path.exists()
    reloaded = StateManager(tmp_path / "s", "ctx")
    assert reloaded.get("r")["status"] == "applied"


def test_corrupt_state_file_falls_back_to_default(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "state.json").write_text("{not valid json")
    sm = StateManager(sdir, "ctx")
    assert sm.data == {"version": 1, "clusters": {}}


def test_clusters_are_isolated_by_key(tmp_path):
    sdir = tmp_path / "s"
    a = StateManager(sdir, "ctx-a")
    a.record_apply(_manifest_resource(tmp_path))
    a.save()
    b = StateManager(sdir, "ctx-b")
    assert b.get("r") is None  # different cluster key -> separate bucket
    # but the original cluster is preserved in the shared file
    a2 = StateManager(sdir, "ctx-a")
    assert a2.get("r") is not None


def test_drift_detects_changed_manifest(tmp_path):
    res = _manifest_resource(tmp_path)
    sm = StateManager(tmp_path / "s", "ctx")
    sm.record_apply(res)
    assert sm.drift(res) == []  # nothing changed yet
    # mutate the manifest on disk
    res.steps[0].manifests[0].write_text("apiVersion: v1\nkind: ConfigMap\n"
                                         "metadata:\n  name: changed\n")
    drift = sm.drift(res)
    assert len(drift) == 1


def test_drift_ignores_url_manifests(tmp_path):
    step = StepDef(name="apply", kind="manifest",
                   manifests=["https://example.com/m.yaml"])
    res = ResourceDef(name="r", namespace="ns", phase=None, steps=[step])
    sm = StateManager(tmp_path, "ctx")
    sm.record_apply(res)
    assert sm.drift(res) == []  # remote manifests are never drift-checked


def test_drift_empty_for_unknown_resource(tmp_path):
    sm = StateManager(tmp_path, "ctx")
    res = _manifest_resource(tmp_path)
    assert sm.drift(res) == []


def test_clear_wipes_cluster_and_persists(tmp_path):
    sdir = tmp_path / "s"
    sm = StateManager(sdir, "ctx")
    sm.record_apply(_manifest_resource(tmp_path))
    sm.save()
    sm.clear()
    assert sm.all() == {}
    assert StateManager(sdir, "ctx").all() == {}
