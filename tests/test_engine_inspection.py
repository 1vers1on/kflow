"""Tests for the engine's inspection ops: status, health, logs, helm_sync."""

from __future__ import annotations

import pytest

from kflow.core import Kflow, load_root_config
from kflow.models import KflowError


def _engine(tmp_project, **kw):
    cfg = load_root_config(tmp_project)
    return Kflow(cfg, **kw)


# -- status ----------------------------------------------------------------- #


def test_status_reports_state_and_workloads(tmp_project, recorder, monkeypatch):
    engine = _engine(tmp_project)
    engine.apply(wait=False)
    monkeypatch.setattr(
        engine.kube, "get_workloads",
        lambda ns, sel=None: [{"kind": "Deployment", "name": "web",
                               "ready": 1, "desired": 1, "ok": True}],
    )
    monkeypatch.setattr(engine.kube, "helm_status",
                        lambda rel, ns: {"info": {"status": "deployed"}})
    rows = {r["name"]: r for r in engine.status()}
    assert rows["web"]["state"] == "applied"
    assert rows["web"]["workloads"] == "1/1"
    assert rows["db"]["helm"] == "deployed"


def test_status_no_selector_shows_dash(tmp_project, recorder):
    engine = _engine(tmp_project)
    rows = {r["name"]: r for r in engine.status()}
    # db has no explicit selector and is helm-backed -> derives one -> "0/0"
    # web has a selector -> "0/0"; neither has live workloads in the fake cluster
    assert rows["web"]["workloads"] in ("0/0", "-")


# -- health ----------------------------------------------------------------- #


def test_health_all_healthy(tmp_project, recorder, monkeypatch):
    engine = _engine(tmp_project)
    monkeypatch.setattr(
        engine.kube, "get_workloads",
        lambda ns, sel=None: [{"kind": "Deployment", "name": "web", "ready": 1,
                               "desired": 1, "ok": True}],
    )
    results = {r["name"]: r for r in engine.health(["web"])}
    assert results["web"]["healthy"] is True


def test_health_unhealthy_when_workload_not_ok(tmp_project, recorder, monkeypatch):
    engine = _engine(tmp_project)
    monkeypatch.setattr(
        engine.kube, "get_workloads",
        lambda ns, sel=None: [{"kind": "Deployment", "name": "web", "ready": 0,
                               "desired": 2, "ok": False}],
    )
    results = {r["name"]: r for r in engine.health(["web"])}
    assert results["web"]["healthy"] is False
    assert "deployment/web 0/2" in results["web"]["detail"]


def test_health_runner_hook_can_fail(tmp_project, recorder, monkeypatch):
    # DemoRunner.health returns the 'healthy' config value; flip it to False.
    cfg = load_root_config(tmp_project)
    web = cfg.resource_map["web"]
    migrate = next(s for s in web.steps if s.kind == "runner")
    migrate.runner.config["healthy"] = False
    engine = Kflow(cfg)
    monkeypatch.setattr(engine.kube, "get_workloads", lambda ns, sel=None: [])
    results = {r["name"]: r for r in engine.health(["web"])}
    assert results["web"]["healthy"] is False


# -- logs ------------------------------------------------------------------- #


def test_logs_uses_resource_selector(tmp_project, recorder):
    engine = _engine(tmp_project)
    engine.logs("web", tail=5)
    cmds = [" ".join(c["cmd"]) for c in recorder]
    log_cmd = next(c for c in cmds if "logs" in c)
    assert "-l app=web" in log_cmd
    assert "--tail 5" in log_cmd


def test_logs_unknown_resource_raises(tmp_project, recorder):
    engine = _engine(tmp_project)
    with pytest.raises(KflowError, match="no resource named"):
        engine.logs("ghost")


def test_logs_helm_resource_uses_instance_label(tmp_project, recorder):
    engine = _engine(tmp_project)
    engine.logs("db")
    cmds = [" ".join(c["cmd"]) for c in recorder]
    log_cmd = next(c for c in cmds if "logs" in c)
    assert "app.kubernetes.io/instance=db" in log_cmd


# -- helm_sync -------------------------------------------------------------- #


def test_helm_sync_only_touches_helm_resources(tmp_project, recorder):
    engine = _engine(tmp_project)
    touched = engine.helm_sync()
    assert "db" in touched
    assert "web" not in touched
    cmds = [" ".join(c["cmd"]) for c in recorder]
    assert any("upgrade --install db" in c for c in cmds)


def test_helm_sync_targeted_no_helm_is_empty(tmp_project, recorder):
    engine = _engine(tmp_project)
    touched = engine.helm_sync(["web"], with_deps=False)
    assert touched == []


# -- restart drives runner hooks -------------------------------------------- #


def test_restart_invokes_runner_restart_hook(tmp_project, recorder, monkeypatch):
    engine = _engine(tmp_project)
    seen = []
    # patch the registry so we can observe the runner restart hook firing
    real = engine.registry.instantiate

    def spy(name, config=None):
        inst = real(name, config)
        orig = inst.restart
        inst.restart = lambda ctx: (seen.append(name), orig(ctx))
        return inst

    monkeypatch.setattr(engine.registry, "instantiate", spy)
    engine.restart(["web"], wait=False)
    assert "DemoRunner" in seen
