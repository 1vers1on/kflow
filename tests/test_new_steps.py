"""Tests for new step kinds added in this release:
secret, configmap, exec, docker-build, URL manifests, wait jsonpath, rollout-wait.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kflow.core import (
    ConfigError,
    DockerBuildSpec,
    ExecSpec,
    NamespaceSpec,
    SecretSpec,
    ConfigMapSpec,
    WaitSpec,
    RolloutWaitSpec,
    _is_url,
    _parse_docker_build,
    _parse_exec,
    _parse_namespace,
    _parse_secret,
    _parse_configmap,
    _parse_wait,
    _parse_rollout_wait,
    _parse_manifests,
    load_root_config,
    DependencyGraph,
    Kflow,
)

from .conftest import ROOT_CONFIG


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #

def _write_resource(tmp_path: Path, name: str, steps: list,
                    phase: str = "p") -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": name,
        "namespace": "test",
        "phase": phase,
        "steps": steps,
    }))
    return p


def _write_config(tmp_path: Path, resources: list, phases=None) -> Path:
    cfg = {
        "kflow": {"version": "v1", "kind": "Config"},
        "phases": phases or [{"name": "p"}],
        "resources": resources,
    }
    p = tmp_path / "kflow.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# --------------------------------------------------------------------------- #
# URL manifests
# --------------------------------------------------------------------------- #

def test_is_url():
    assert _is_url("https://example.com/foo.yaml")
    assert _is_url("http://example.com/foo.yaml")
    assert not _is_url("/local/path.yaml")
    assert not _is_url("relative/path.yaml")


def test_url_manifests_kept_as_strings(tmp_path):
    url = "https://raw.githubusercontent.com/metallb/metallb/main/config/manifests/metallb-native.yaml"
    _write_resource(tmp_path, "metallb", [
        {"name": "install", "manifests": [url]},
    ])
    _write_config(tmp_path, [str(tmp_path / "metallb.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["metallb"].steps[0]
    assert step.kind == "manifest"
    assert len(step.manifests) == 1
    assert step.manifests[0] == url  # preserved as string, not coerced to Path


def test_url_manifests_mixed_with_local(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    url = "https://example.com/remote.yaml"
    _write_resource(tmp_path, "mixed", [
        {"name": "apply", "manifests": [str(local), url]},
    ])
    _write_config(tmp_path, [str(tmp_path / "mixed.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    manifests = cfg.resource_map["mixed"].steps[0].manifests
    assert isinstance(manifests[0], Path)
    assert manifests[1] == url


def test_url_manifests_not_flagged_by_validate(tmp_path, monkeypatch):
    url = "https://example.com/remote.yaml"
    _write_resource(tmp_path, "remote", [
        {"name": "install", "manifests": [url]},
    ])
    _write_config(tmp_path, [str(tmp_path / "remote.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)
    # drift check should not blow up on URL entries
    drift = engine.state.drift(cfg.resource_map["remote"])
    assert drift == []


# --------------------------------------------------------------------------- #
# wait step - jsonpath
# --------------------------------------------------------------------------- #

def test_parse_wait_condition():
    spec = {"for": "deployment/foo", "condition": "available", "timeout": 60}
    w = _parse_wait(spec, "res")
    assert w.for_resource == "deployment/foo"
    assert w.condition == "available"
    assert w.jsonpath is None
    assert w.timeout == 60


def test_parse_wait_jsonpath():
    spec = {"for": "endpoints/webhook", "jsonpath": "{.subsets[0].addresses[0].ip}"}
    w = _parse_wait(spec, "res")
    assert w.condition is None
    assert w.jsonpath == "{.subsets[0].addresses[0].ip}"


def test_parse_wait_requires_condition_or_jsonpath():
    with pytest.raises(ConfigError, match="requires 'condition' or 'jsonpath'"):
        _parse_wait({"for": "pod/foo"}, "res")


def test_wait_jsonpath_in_resource(tmp_path):
    _write_resource(tmp_path, "webhook-waiter", [
        {"name": "wait-ep",
         "wait": {"for": "endpoints/my-svc",
                  "jsonpath": "{.subsets[0].addresses[0].ip}",
                  "timeout": 30}},
    ])
    _write_config(tmp_path, [str(tmp_path / "webhook-waiter.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["webhook-waiter"].steps[0]
    assert step.kind == "wait"
    assert step.wait.jsonpath == "{.subsets[0].addresses[0].ip}"
    assert step.wait.condition is None


def test_wait_result_raises_on_failure(tmp_path, monkeypatch, recorder):
    """A failed wait step must propagate as an error, not silently continue."""
    _write_resource(tmp_path, "waiter", [
        {"name": "w", "wait": {"for": "deployment/foo", "condition": "available"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "waiter.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    from kflow.runners.shell import CommandResult, CommandError as CE
    monkeypatch.setattr(
        engine.kube, "wait_for",
        lambda *a, **kw: CommandResult(
            cmd=["kubectl", "wait"], returncode=1,
            stdout="", stderr="timed out waiting for the condition"
        ),
    )
    with pytest.raises(CE):
        engine.apply(wait=False)


# --------------------------------------------------------------------------- #
# secret step
# --------------------------------------------------------------------------- #

def test_parse_secret_literals(tmp_path):
    spec = {"literals": {"token": "abc123"}}
    s = _parse_secret(spec, tmp_path, "res", "step")
    assert s.literals == {"token": "abc123"}
    assert s.from_env == {}
    assert not s.if_not_exists


def test_parse_secret_from_env(tmp_path):
    spec = {"fromEnv": {"api-token": "MY_TOKEN_ENV"}}
    s = _parse_secret(spec, tmp_path, "res", "step")
    assert s.from_env == {"api-token": "MY_TOKEN_ENV"}


def test_parse_secret_if_not_exists(tmp_path):
    spec = {"literals": {"key": "val"}, "ifNotExists": True}
    s = _parse_secret(spec, tmp_path, "res", "step")
    assert s.if_not_exists is True


def test_parse_secret_custom_name(tmp_path):
    spec = {"name": "my-secret", "namespace": "other-ns", "literals": {}}
    s = _parse_secret(spec, tmp_path, "res", "step")
    assert s.name == "my-secret"
    assert s.namespace == "other-ns"


def test_secret_step_in_resource(tmp_path):
    _write_resource(tmp_path, "myapp", [
        {"name": "creds",
         "secret": {"name": "app-creds", "literals": {"password": "s3cr3t"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.kind == "secret"
    assert step.secret.name == "app-creds"
    assert step.secret.literals == {"password": "s3cr3t"}


def test_secret_apply_calls_kube(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "creds", "secret": {"literals": {"key": "val"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(
        engine.kube, "secret_apply",
        lambda name, ns, **kw: calls.append((name, ns, kw)) or None,
    )
    engine.apply(wait=False)
    assert len(calls) == 1
    assert calls[0][0] == "creds"  # name defaults to step name
    assert calls[0][2]["literals"] == {"key": "val"}


def test_secret_from_env_reads_environment(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "cf-secret",
         "secret": {"fromEnv": {"api-token": "CLOUDFLARE_TOKEN"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    monkeypatch.setenv("CLOUDFLARE_TOKEN", "tok-abc123")
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(
        engine.kube, "secret_apply",
        lambda name, ns, **kw: calls.append(kw) or None,
    )
    engine.apply(wait=False)
    assert calls[0]["literals"]["api-token"] == "tok-abc123"


def test_secret_from_env_raises_if_missing(tmp_path, monkeypatch, recorder):
    from kflow.core import KflowError
    _write_resource(tmp_path, "myapp", [
        {"name": "cf-secret",
         "secret": {"fromEnv": {"api-token": "MISSING_VAR_XYZ"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)
    monkeypatch.setattr(engine.kube, "secret_apply", lambda *a, **kw: None)
    with pytest.raises(KflowError, match="MISSING_VAR_XYZ"):
        engine.apply(wait=False)


def test_secret_if_not_exists_skips_when_present(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "memberlist",
         "secret": {"ifNotExists": True, "literals": {"key": "val"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    monkeypatch.setattr(engine.kube, "resource_exists", lambda *a, **kw: True)
    apply_calls = []
    monkeypatch.setattr(engine.kube, "secret_apply",
                        lambda *a, **kw: apply_calls.append(1) or None)
    engine.apply(wait=False)
    assert apply_calls == []  # skipped because it already exists


def test_secret_destroy_deletes_unless_if_not_exists(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "my-secret", "secret": {"literals": {"k": "v"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    delete_calls = []
    monkeypatch.setattr(engine.kube, "secret_delete",
                        lambda name, ns, **kw: delete_calls.append(name) or None)
    monkeypatch.setattr(engine.kube, "secret_apply", lambda *a, **kw: None)
    engine.apply(wait=False)
    engine.destroy()
    assert "my-secret" in delete_calls


# --------------------------------------------------------------------------- #
# configmap step
# --------------------------------------------------------------------------- #

def test_parse_configmap_literals(tmp_path):
    spec = {"literals": {"key": "value"}}
    cm = _parse_configmap(spec, tmp_path, "res", "step")
    assert cm.literals == {"key": "value"}
    assert not cm.if_not_exists


def test_parse_configmap_from_dir(tmp_path):
    (tmp_path / "conf").mkdir()
    spec = {"fromDir": "conf"}
    cm = _parse_configmap(spec, tmp_path, "res", "step")
    assert cm.from_dir == tmp_path / "conf"


def test_configmap_step_in_resource(tmp_path):
    _write_resource(tmp_path, "app", [
        {"name": "templates",
         "configmap": {"name": "velocity-templates", "fromDir": "."}},
    ])
    _write_config(tmp_path, [str(tmp_path / "app.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["app"].steps[0]
    assert step.kind == "configmap"
    assert step.configmap.name == "velocity-templates"
    assert step.configmap.from_dir is not None


def test_configmap_apply_calls_kube(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "app", [
        {"name": "cm",
         "configmap": {"literals": {"app.conf": "debug=true"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "app.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(engine.kube, "configmap_apply",
                        lambda name, ns, **kw: calls.append((name, kw)) or None)
    engine.apply(wait=False)
    assert calls[0][0] == "cm"
    assert calls[0][1]["literals"] == {"app.conf": "debug=true"}


# --------------------------------------------------------------------------- #
# exec step
# --------------------------------------------------------------------------- #

def test_parse_exec_string_command(tmp_path):
    spec = {"command": "bao operator init", "pod": "bao-0"}
    e = _parse_exec(spec, "res")
    assert e.command == ["sh", "-c", "bao operator init"]
    assert e.pod == "bao-0"


def test_parse_exec_list_command(tmp_path):
    spec = {"command": ["bao", "operator", "init"], "pod": "bao-0"}
    e = _parse_exec(spec, "res")
    assert e.command == ["bao", "operator", "init"]


def test_parse_exec_requires_pod_or_selector():
    with pytest.raises(ConfigError, match="requires 'pod' or 'selector'"):
        _parse_exec({"command": "echo hi"}, "res")


def test_parse_exec_missing_command():
    with pytest.raises(ConfigError, match="missing 'command'"):
        _parse_exec({"pod": "foo"}, "res")


def test_parse_exec_on_destroy(tmp_path):
    spec = {"command": "init", "pod": "p", "onDestroy": "cleanup"}
    e = _parse_exec(spec, "res")
    assert e.on_destroy == ["sh", "-c", "cleanup"]


def test_parse_exec_on_destroy_null(tmp_path):
    spec = {"command": "init", "pod": "p", "onDestroy": None}
    e = _parse_exec(spec, "res")
    assert e.on_destroy is None


def test_exec_step_in_resource(tmp_path):
    _write_resource(tmp_path, "vault", [
        {"name": "init",
         "exec": {"pod": "openbao-transit-0",
                  "command": ["sh", "-c", "bao operator init"]}},
    ])
    _write_config(tmp_path, [str(tmp_path / "vault.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["vault"].steps[0]
    assert step.kind == "exec"
    assert step.exec_spec.pod == "openbao-transit-0"
    assert step.exec_spec.command == ["sh", "-c", "bao operator init"]


def test_exec_step_calls_kube_exec(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "vault", [
        {"name": "init",
         "exec": {"pod": "bao-0", "command": "bao status"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "vault.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    from kflow.runners.shell import CommandResult
    calls = []

    def fake_exec(ns, *, command, pod=None, selector=None, container=None):
        calls.append({"pod": pod, "command": command})
        return CommandResult(cmd=["kubectl", "exec"], returncode=0)

    monkeypatch.setattr(engine.kube, "exec", fake_exec)
    engine.apply(wait=False)
    assert calls[0]["pod"] == "bao-0"
    assert calls[0]["command"] == ["sh", "-c", "bao status"]


def test_exec_step_raises_on_failure(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "vault", [
        {"name": "init", "exec": {"pod": "bao-0", "command": "bao status"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "vault.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    from kflow.runners.shell import CommandResult, CommandError as CE

    monkeypatch.setattr(engine.kube, "exec",
                        lambda *a, **kw: CommandResult(
                            cmd=["kubectl", "exec"], returncode=1,
                            stderr="exec failed"))
    with pytest.raises(CE):
        engine.apply(wait=False)


def test_exec_step_skips_destroy_when_on_destroy_none(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "vault", [
        {"name": "init",
         "exec": {"pod": "bao-0", "command": "init", "onDestroy": None}},
    ])
    _write_config(tmp_path, [str(tmp_path / "vault.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    from kflow.runners.shell import CommandResult
    exec_calls = []
    monkeypatch.setattr(engine.kube, "exec",
                        lambda *a, **kw: exec_calls.append(kw) or
                        CommandResult(cmd=[], returncode=0))
    engine.apply(wait=False)
    exec_calls.clear()
    engine.destroy()
    assert exec_calls == []  # on_destroy=None means skip


# --------------------------------------------------------------------------- #
# docker-build step
# --------------------------------------------------------------------------- #

def test_parse_docker_build_basic(tmp_path):
    (tmp_path / "docker").mkdir()
    spec = {"context": "docker", "tag": "myimage:latest"}
    db = _parse_docker_build(spec, tmp_path, "res")
    assert db.context == tmp_path / "docker"
    assert db.tag == "myimage:latest"
    assert not db.push
    assert db.platform is None


def test_parse_docker_build_full(tmp_path):
    (tmp_path / "docker").mkdir()
    spec = {
        "context": "docker",
        "tag": "myimage:1.0",
        "buildArgs": {"VERSION": "1.0"},
        "push": True,
        "platform": "linux/amd64",
        "target": "prod",
    }
    db = _parse_docker_build(spec, tmp_path, "res")
    assert db.build_args == {"VERSION": "1.0"}
    assert db.push is True
    assert db.platform == "linux/amd64"
    assert db.target == "prod"


def test_parse_docker_build_missing_context(tmp_path):
    with pytest.raises(ConfigError, match="missing 'context'"):
        _parse_docker_build({"tag": "x"}, tmp_path, "res")


def test_parse_docker_build_missing_tag(tmp_path):
    with pytest.raises(ConfigError, match="missing 'tag'"):
        _parse_docker_build({"context": "."}, tmp_path, "res")


def test_docker_build_step_in_resource(tmp_path):
    _write_resource(tmp_path, "myapp", [
        {"name": "build",
         "dockerBuild": {"context": ".", "tag": "myapp:dev"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.kind == "docker-build"
    assert step.docker_build.tag == "myapp:dev"


def test_docker_build_dry_run_does_not_exec(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "build", "dockerBuild": {"context": ".", "tag": "myapp:dev"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg, dry_run=True)

    ran = []
    monkeypatch.setattr("kflow.engine.run_command",
                        lambda cmd, **kw: ran.append(cmd) or None)
    engine.apply(wait=False)
    assert ran == []  # dry_run skips docker build


def test_docker_build_runs_docker(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "build", "dockerBuild": {"context": ".", "tag": "myapp:dev"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    ran = []
    monkeypatch.setattr("kflow.engine.run_command",
                        lambda cmd, **kw: ran.append(cmd) or None)
    engine.apply(wait=False)
    assert any("docker" in str(c) and "build" in str(c) for c in ran), ran


def test_docker_build_also_pushes(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "build",
         "dockerBuild": {"context": ".", "tag": "myapp:prod", "push": True}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    ran = []
    monkeypatch.setattr("kflow.engine.run_command",
                        lambda cmd, **kw: ran.append(cmd) or None)
    engine.apply(wait=False)
    cmds = [" ".join(str(t) for t in c) for c in ran]
    assert any("docker build" in c for c in cmds)
    assert any("docker push" in c for c in cmds)


# --------------------------------------------------------------------------- #
# create-namespace step
# --------------------------------------------------------------------------- #

def test_parse_namespace_defaults():
    spec = _parse_namespace({}, "res")
    assert spec.name is None
    assert spec.labels == {}
    assert spec.annotations == {}
    assert spec.if_not_exists is False
    assert spec.delete_on_destroy is False


def test_parse_namespace_all_fields():
    spec = _parse_namespace({
        "name": "my-ns",
        "labels": {"env": "prod"},
        "annotations": {"team": "platform"},
        "ifNotExists": True,
        "deleteOnDestroy": True,
    }, "res")
    assert spec.name == "my-ns"
    assert spec.labels == {"env": "prod"}
    assert spec.annotations == {"team": "platform"}
    assert spec.if_not_exists is True
    assert spec.delete_on_destroy is True


def test_create_namespace_step_in_resource(tmp_path):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns", "labels": {"env": "prod"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.kind == "create-namespace"
    assert step.namespace_spec.name == "my-ns"
    assert step.namespace_spec.labels == {"env": "prod"}


def test_create_namespace_bare_mapping(tmp_path):
    """createNamespace: {} (no sub-fields) is valid and uses all defaults."""
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.kind == "create-namespace"
    assert step.namespace_spec.name is None


def test_create_namespace_apply_calls_namespace_apply(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns", "labels": {"env": "prod"}}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(
        engine.kube, "namespace_apply",
        lambda name, **kw: calls.append((name, kw)) or None,
    )
    engine.apply(wait=False)
    assert len(calls) == 1
    assert calls[0][0] == "my-ns"
    assert calls[0][1]["labels"] == {"env": "prod"}


def test_create_namespace_defaults_to_resource_namespace(tmp_path, monkeypatch, recorder):
    """When name is omitted the resource's own namespace is used."""
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(engine.kube, "namespace_apply",
                        lambda name, **kw: calls.append(name) or None)
    engine.apply(wait=False)
    assert calls == ["test"]  # "test" is the namespace set by _write_resource


def test_create_namespace_if_not_exists_skips_when_present(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns", "ifNotExists": True}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    monkeypatch.setattr(engine.kube, "namespace_exists", lambda ns: True)
    calls = []
    monkeypatch.setattr(engine.kube, "namespace_apply",
                        lambda *a, **kw: calls.append(1) or None)
    engine.apply(wait=False)
    assert calls == []


def test_create_namespace_destroy_default_is_noop(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    monkeypatch.setattr(engine.kube, "namespace_apply", lambda *a, **kw: None)
    delete_calls = []
    monkeypatch.setattr(engine.kube, "delete_namespace",
                        lambda ns, **kw: delete_calls.append(ns) or None)
    engine.apply(wait=False)
    engine.destroy()
    assert delete_calls == []


def test_create_namespace_destroy_deletes_when_opted_in(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns", "deleteOnDestroy": True}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    monkeypatch.setattr(engine.kube, "namespace_apply", lambda *a, **kw: None)
    delete_calls = []
    monkeypatch.setattr(engine.kube, "delete_namespace",
                        lambda ns, **kw: delete_calls.append(ns) or None)
    engine.apply(wait=False)
    engine.destroy()
    assert "my-ns" in delete_calls


def test_create_namespace_reload_reapplies(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "ns", "createNamespace": {"name": "my-ns"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(engine.kube, "namespace_apply",
                        lambda name, **kw: calls.append(name) or None)
    engine.apply(wait=False)
    engine.reload(wait=False)
    assert calls.count("my-ns") == 2


def test_docker_build_destroy_is_noop(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "build", "dockerBuild": {"context": ".", "tag": "myapp:dev"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    ran = []
    monkeypatch.setattr("kflow.engine.run_command",
                        lambda cmd, **kw: ran.append(cmd) or None)
    engine.apply(wait=False)
    ran.clear()
    engine.destroy()
    # docker images are not removed on destroy
    assert all("docker" not in str(c) for c in ran), ran


# --------------------------------------------------------------------------- #
# rollout-wait step
# --------------------------------------------------------------------------- #

def test_parse_rollout_wait_defaults():
    spec = _parse_rollout_wait({}, "res")
    assert spec.kinds == ["deployment", "statefulset", "daemonset"]
    assert spec.selector is None
    assert spec.namespace is None
    assert spec.timeout == 300


def test_parse_rollout_wait_explicit_kinds():
    spec = _parse_rollout_wait({"kinds": ["deployment", "replicaset"]}, "res")
    assert spec.kinds == ["deployment", "replicaset"]


def test_parse_rollout_wait_replicaset_valid():
    spec = _parse_rollout_wait({"kinds": ["replicaset"]}, "res")
    assert spec.kinds == ["replicaset"]


def test_parse_rollout_wait_all_supported_kinds():
    kinds = ["deployment", "statefulset", "daemonset", "replicaset"]
    spec = _parse_rollout_wait({"kinds": kinds}, "res")
    assert spec.kinds == kinds


def test_parse_rollout_wait_invalid_kind_raises():
    with pytest.raises(ConfigError, match="unsupported kinds"):
        _parse_rollout_wait({"kinds": ["deployment", "job"]}, "res")


def test_parse_rollout_wait_selector_and_timeout():
    spec = _parse_rollout_wait(
        {"selector": "app=myapp", "timeout": 60, "namespace": "prod"}, "res"
    )
    assert spec.selector == "app=myapp"
    assert spec.timeout == 60
    assert spec.namespace == "prod"


def test_rollout_wait_step_in_resource(tmp_path):
    _write_resource(tmp_path, "myapp", [
        {"name": "wait-all",
         "rolloutWait": {"kinds": ["deployment", "replicaset"], "selector": "app=x"}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.kind == "rollout-wait"
    assert step.rollout_wait.kinds == ["deployment", "replicaset"]
    assert step.rollout_wait.selector == "app=x"


def test_rollout_wait_empty_mapping_uses_defaults(tmp_path):
    _write_resource(tmp_path, "myapp", [{"name": "wait", "rolloutWait": {}}])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    step = cfg.resource_map["myapp"].steps[0]
    assert step.rollout_wait.kinds == ["deployment", "statefulset", "daemonset"]
    assert step.rollout_wait.timeout == 300


def test_rollout_wait_calls_rollout_wait_all(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "wait", "rolloutWait": {"kinds": ["deployment", "replicaset"]}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(engine.kube, "rollout_wait_all",
                        lambda ns, kinds=None, selector=None, timeout=300:
                        calls.append({"ns": ns, "kinds": kinds}))
    engine.apply(wait=False)
    assert len(calls) == 1
    assert calls[0]["kinds"] == ["deployment", "replicaset"]


def test_rollout_wait_destroy_is_noop(tmp_path, monkeypatch, recorder):
    _write_resource(tmp_path, "myapp", [
        {"name": "wait", "rolloutWait": {}},
    ])
    _write_config(tmp_path, [str(tmp_path / "myapp.yaml")])
    cfg = load_root_config(tmp_path / "kflow.yaml")
    cfg.state_dir = tmp_path / "state"
    engine = Kflow(cfg)

    calls = []
    monkeypatch.setattr(engine.kube, "rollout_wait_all",
                        lambda *a, **kw: calls.append(1))
    engine.apply(wait=False)
    calls.clear()
    engine.destroy()
    assert calls == []  # rollout-wait is a no-op on destroy


def test_get_workloads_includes_replicasets_when_requested(monkeypatch):
    from kflow.runners.kube import KubeClient

    kube = KubeClient(dry_run=False)
    captured = []

    def fake_get_json(args):
        captured.append(args)
        return {}

    monkeypatch.setattr(kube, "get_json", fake_get_json)
    kube.get_workloads("my-ns", kinds=["deployment", "replicaset"])

    assert len(captured) == 1
    resource_arg = captured[0][1]  # second element is the resource list
    assert "deployments" in resource_arg
    assert "replicasets" in resource_arg


def test_get_workloads_default_excludes_replicasets(monkeypatch):
    from kflow.runners.kube import KubeClient

    kube = KubeClient(dry_run=False)
    captured = []

    monkeypatch.setattr(kube, "get_json", lambda args: captured.append(args) or {})
    kube.get_workloads("my-ns")

    resource_arg = captured[0][1]
    assert "replicasets" not in resource_arg
    assert "deployments" in resource_arg
