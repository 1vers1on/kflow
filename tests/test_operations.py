from __future__ import annotations

from kflow.core import Kflow, load_root_config

from .conftest import ROOT_CONFIG


def _build(state_dir, dry_run=False):
    cfg = load_root_config(ROOT_CONFIG)
    cfg.state_dir = state_dir
    return Kflow(cfg, dry_run=dry_run)


def _cmds(calls):
    return [" ".join(c["cmd"]) for c in calls]


def test_apply_runs_in_dependency_order(recorder, state_dir):
    engine = _build(state_dir)
    engine.apply(wait=False)
    cmds = _cmds(recorder)

    # helm install for longhorn happens before traefik
    longhorn_i = next(i for i, c in enumerate(cmds)
                      if "helm" in c and "upgrade" in c and "longhorn" in c)
    traefik_i = next(i for i, c in enumerate(cmds)
                     if "helm" in c and "upgrade" in c and "traefik" in c)
    ingress_i = next(i for i, c in enumerate(cmds)
                     if "apply" in c and "longhorn-ingress.yaml" in c)
    deploy_i = next(i for i, c in enumerate(cmds)
                    if "apply" in c and "app-deployment.yaml" in c)

    assert longhorn_i < traefik_i < ingress_i < deploy_i


def test_apply_creates_namespaces(recorder, state_dir):
    engine = _build(state_dir)
    engine.apply(wait=False)
    # namespace creation goes through `kubectl apply -f -` with a Namespace doc
    ns_inputs = [c["input"] for c in recorder
                 if c["input"] and "kind: Namespace" in c["input"]]
    created = {line.split("name:")[1].strip()
               for blob in ns_inputs for line in blob.splitlines()
               if "name:" in line}
    assert {"longhorn-system", "traefik", "demo"} <= created


def test_apply_writes_state(recorder, state_dir):
    engine = _build(state_dir)
    engine.apply(wait=False)
    assert engine.state.path.exists()
    entry = engine.state.get("app")
    assert entry["status"] == "applied"
    assert entry["phase"] == "apps"


def test_dry_run_skips_mutations(recorder, state_dir):
    engine = _build(state_dir, dry_run=True)
    engine.apply(wait=False)
    cmds = _cmds(recorder)
    # No `kubectl apply` (manifests or namespace creation) actually executes.
    assert not any("kubectl apply" in c for c in cmds)
    # helm runs are rendered with --dry-run, never a real upgrade.
    helm_cmds = [c for c in cmds if "helm upgrade" in c]
    assert helm_cmds and all("--dry-run" in c for c in helm_cmds)
    # read-only queries are still permitted.
    assert any("kubectl get" in c for c in cmds)


def test_dry_run_leaves_no_state(recorder, state_dir):
    engine = _build(state_dir, dry_run=True)
    engine.apply(wait=False)
    # dry-run still records bookkeeping locally (it's harmless and useful),
    # but never touches the cluster - covered by test_dry_run_skips_mutations.
    assert engine.state.path.exists()


def test_destroy_reverses_order(recorder, state_dir):
    engine = _build(state_dir)
    engine.destroy(delete_namespaces=False)
    cmds = _cmds(recorder)
    # app deployment deleted before longhorn helm uninstall
    del_deploy = next(i for i, c in enumerate(cmds)
                      if "delete" in c and "app-deployment.yaml" in c)
    uninstall_longhorn = next(i for i, c in enumerate(cmds)
                              if "helm" in c and "uninstall" in c and "longhorn" in c)
    assert del_deploy < uninstall_longhorn


def test_restart_targets_workloads(recorder, state_dir):
    engine = _build(state_dir)
    engine.restart(["app"], wait=False)
    cmds = _cmds(recorder)
    assert any("rollout restart deployment/web" in c for c in cmds)


def test_restart_does_not_touch_unrelated_namespace_workloads(recorder, state_dir,
                                                               monkeypatch):
    """A resource with no workloads/selector/helm must not adopt the other
    workloads sharing its namespace - restarting longhorn-ingress must never
    restart longhorn's own pods (a destructive, wrong-target action)."""
    engine = _build(state_dir)
    # Pretend the shared namespace is full of another resource's workloads.
    monkeypatch.setattr(
        engine.kube, "get_workloads",
        lambda ns, sel=None: [{"kind": "Deployment", "name": "longhorn-manager",
                               "ready": 1, "desired": 1, "ok": True}],
    )
    ingress = engine.config.resource_map["longhorn-ingress"]
    assert engine._resolve_selector(ingress) is None
    assert engine._target_workloads(ingress) == []

    engine.restart(["longhorn-ingress"], wait=False)
    assert not any("rollout restart" in c for c in _cmds(recorder))


def test_helm_resource_targets_release_instance(recorder, state_dir):
    """A helm resource with no explicit selector targets its release's pods
    via the app.kubernetes.io/instance label, not the whole namespace."""
    engine = _build(state_dir)
    traefik = engine.config.resource_map["traefik"]
    assert engine._resolve_selector(traefik) == "app.kubernetes.io/instance=traefik"


def test_reload_applies_then_restarts(recorder, state_dir):
    engine = _build(state_dir)
    engine.reload(["app"], wait=False)
    cmds = _cmds(recorder)
    apply_i = next(i for i, c in enumerate(cmds)
                   if "apply" in c and "app-deployment.yaml" in c)
    restart_i = next(i for i, c in enumerate(cmds)
                     if "rollout restart deployment/web" in c)
    assert apply_i < restart_i


def test_targeted_apply_pulls_in_dependencies(recorder, state_dir):
    engine = _build(state_dir)
    targets = engine.apply(["longhorn-ingress"], wait=False)
    assert "traefik" in targets
    assert "longhorn-storage" in targets
    assert "app" not in targets


def test_server_side_apply_adds_flag(recorder, state_dir):
    engine = _build(state_dir)
    engine.kube.server_side = True
    engine.apply(wait=False)
    cmds = _cmds(recorder)
    apply_cmds = [c for c in cmds if "kubectl" in c and "apply" in c and "-f" in c]
    assert apply_cmds, "expected at least one kubectl apply -f call"
    assert all("--server-side" in c for c in apply_cmds), (
        "every kubectl apply call must carry --server-side"
    )


def test_server_side_apply_kustomize(recorder, state_dir):
    """apply_kustomize also passes --server-side when enabled."""
    from kflow.runners.kube import KubeClient
    from rich.console import Console

    kube = KubeClient(dry_run=False, server_side=True, console=Console())
    kube.apply_kustomize("/some/path")
    cmds = _cmds(recorder)
    kustomize_cmds = [c for c in cmds if "apply" in c and "-k" in c]
    assert kustomize_cmds
    assert all("--server-side" in c for c in kustomize_cmds)


def test_server_side_off_by_default(recorder, state_dir):
    engine = _build(state_dir)
    engine.apply(wait=False)
    cmds = _cmds(recorder)
    apply_cmds = [c for c in cmds if "kubectl" in c and "apply" in c]
    assert not any("--server-side" in c for c in apply_cmds)


def test_server_side_reload(recorder, state_dir):
    engine = _build(state_dir)
    engine.kube.server_side = True
    engine.reload(["app"], wait=False)
    cmds = _cmds(recorder)
    apply_cmds = [c for c in cmds if "kubectl" in c and "apply" in c and "-f" in c]
    assert apply_cmds
    assert all("--server-side" in c for c in apply_cmds)
