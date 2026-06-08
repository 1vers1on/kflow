"""Unit tests for the kubectl/helm wrapper (KubeClient) and helm value flatten."""

from __future__ import annotations

import json

import pytest

from kflow.runners.kube import KubeClient, _flatten_set_values
from kflow.runners.shell import CommandResult


class FakeRunner:
    """Records commands and returns canned results keyed by a matcher."""

    def __init__(self):
        self.calls = []
        self._responses = []

    def respond(self, match, *, stdout="", returncode=0):
        self._responses.append((match, stdout, returncode))

    def __call__(self, cmd, *, check=True, capture=True, input_text=None,
                 timeout=None, env=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append({"cmd": cmd, "input": input_text})
        joined = " ".join(cmd)
        for match, stdout, rc in self._responses:
            if match in joined:
                if check and rc != 0:
                    from kflow.runners.shell import CommandError
                    raise CommandError(cmd, rc, stdout, "")
                return CommandResult(cmd=cmd, returncode=rc, stdout=stdout)
        return CommandResult(cmd=cmd, returncode=0, stdout="")

    @property
    def cmds(self):
        return [" ".join(c["cmd"]) for c in self.calls]


@pytest.fixture
def fake(monkeypatch):
    fr = FakeRunner()
    monkeypatch.setattr("kflow.runners.kube.run_command", fr)
    return fr


# -- helm value flattening -------------------------------------------------- #


def test_flatten_nested_dict():
    pairs = _flatten_set_values({"a": {"b": 1, "c": "x"}})
    assert "a.b=1" in pairs
    assert "a.c=x" in pairs


def test_flatten_bool_and_none():
    pairs = _flatten_set_values({"on": True, "off": False, "nil": None})
    assert "on=true" in pairs
    assert "off=false" in pairs
    assert "nil=null" in pairs


def test_flatten_list_indexes():
    pairs = _flatten_set_values({"xs": ["a", "b"]})
    assert "xs[0]=a" in pairs
    assert "xs[1]=b" in pairs


# -- base command construction --------------------------------------------- #


def test_base_uses_context_flag_for_kubectl(fake):
    kube = KubeClient(context="prod")
    kube.kubectl(["get", "pods"], check=False)
    assert "kubectl --context prod get pods" in fake.cmds[0]


def test_base_uses_kube_context_flag_for_helm(fake):
    kube = KubeClient(context="prod")
    kube.helm(["status", "rel"], check=False)
    assert "helm --kube-context prod status rel" in fake.cmds[0]


def test_kubeconfig_flag_added(fake):
    kube = KubeClient(kubeconfig="/tmp/kc")
    kube.kubectl(["get", "ns"], check=False)
    assert "--kubeconfig /tmp/kc" in fake.cmds[0]


# -- namespaces ------------------------------------------------------------- #


def test_namespace_exists_true(fake):
    fake.respond("get namespace prod", returncode=0)
    kube = KubeClient()
    assert kube.namespace_exists("prod") is True


def test_namespace_exists_false(fake):
    fake.respond("get namespace ghost", returncode=1)
    kube = KubeClient()
    assert kube.namespace_exists("ghost") is False


def test_ensure_namespace_skips_default(fake):
    KubeClient().ensure_namespace("default")
    assert fake.calls == []


def test_ensure_namespace_creates_when_missing(fake):
    fake.respond("get namespace new", returncode=1)
    kube = KubeClient()
    kube.ensure_namespace("new")
    # an apply of a Namespace manifest was piped via stdin
    assert any("apply" in c and "-f -" in c for c in fake.cmds)
    ns_input = [c["input"] for c in fake.calls if c["input"]]
    assert any("kind: Namespace" in i and "name: new" in i for i in ns_input)


def test_namespace_apply_includes_labels_and_annotations(fake):
    kube = KubeClient()
    kube.namespace_apply("ns1", labels={"team": "x"}, annotations={"a": "b"})
    inp = fake.calls[0]["input"]
    assert "app.kubernetes.io/managed-by: kflow" in inp
    assert "team: x" in inp
    assert "annotations:" in inp
    assert "a: b" in inp


def test_delete_namespace_no_wait_flag(fake):
    KubeClient().delete_namespace("ns1")
    assert "--wait=false" in fake.cmds[0]
    assert "--ignore-not-found" in fake.cmds[0]


def test_resource_exists(fake):
    fake.respond("get secret tok -n ns", returncode=0)
    assert KubeClient().resource_exists("secret", "tok", "ns") is True


# -- manifests / apply ------------------------------------------------------ #


def test_apply_file_with_namespace(fake):
    KubeClient().apply_file("m.yaml", namespace="ns")
    assert "apply -n ns -f m.yaml" in fake.cmds[0]


def test_apply_file_server_side(fake):
    KubeClient(server_side=True).apply_file("m.yaml")
    assert "--server-side" in fake.cmds[0]


def test_delete_file_with_namespace(fake):
    KubeClient().delete_file("m.yaml", namespace="ns")
    assert "delete -n ns -f m.yaml" in fake.cmds[0]
    assert "--ignore-not-found" in fake.cmds[0]


def test_apply_kustomize_server_side(fake):
    KubeClient(server_side=True).apply_kustomize("/k")
    assert "apply --server-side -k /k" in fake.cmds[0]


# -- waits ------------------------------------------------------------------ #


def test_wait_for_condition(fake):
    KubeClient().wait_for("deploy/web", condition="available", namespace="ns",
                          timeout=30)
    assert "--for=condition=available" in fake.cmds[0]
    assert "--timeout=30s" in fake.cmds[0]
    assert "-n ns" in fake.cmds[0]


def test_wait_for_jsonpath(fake):
    KubeClient().wait_for("pod/x", jsonpath="{.status.phase}=Running")
    assert "--for=jsonpath={.status.phase}=Running" in fake.cmds[0]


def test_wait_for_requires_condition_or_jsonpath(fake):
    with pytest.raises(ValueError):
        KubeClient().wait_for("pod/x")


# -- rollouts --------------------------------------------------------------- #


def test_rollout_restart_lowercases_kind(fake):
    KubeClient().rollout_restart("Deployment", "web", "ns")
    assert "rollout restart deployment/web -n ns" in fake.cmds[0]


def test_rollout_wait_all_runs_status_per_object(fake):
    # `get deployment -o name` returns two objects; each gets a status call
    fake.respond("get deployment -o name", stdout="deployment/a\ndeployment/b\n")
    KubeClient().rollout_wait_all("ns", kinds=["deployment"])
    status_cmds = [c for c in fake.cmds if "rollout status" in c]
    assert any("deployment/a" in c for c in status_cmds)
    assert any("deployment/b" in c for c in status_cmds)


def test_rollout_wait_all_raises_on_failure(fake):
    from kflow.runners.shell import CommandError
    fake.respond("get deployment -o name", stdout="deployment/a\n")
    fake.respond("rollout status deployment/a", returncode=1)
    with pytest.raises(CommandError):
        KubeClient().rollout_wait_all("ns", kinds=["deployment"])


# -- queries ---------------------------------------------------------------- #


def test_get_workloads_parses_deployment_and_daemonset(fake):
    data = {
        "items": [
            {"kind": "Deployment", "metadata": {"name": "web"},
             "spec": {"replicas": 2}, "status": {"readyReplicas": 2}},
            {"kind": "DaemonSet", "metadata": {"name": "agent"},
             "spec": {}, "status": {"numberReady": 1, "desiredNumberScheduled": 3}},
        ]
    }
    fake.respond("-o json", stdout=json.dumps(data))
    workloads = KubeClient().get_workloads("ns")
    web = next(w for w in workloads if w["name"] == "web")
    agent = next(w for w in workloads if w["name"] == "agent")
    assert web["ok"] is True and web["ready"] == 2 and web["desired"] == 2
    assert agent["ok"] is False and agent["ready"] == 1 and agent["desired"] == 3


def test_get_workloads_includes_replicasets_only_when_requested(fake):
    fake.respond("-o json", stdout="{}")
    kube = KubeClient()
    kube.get_workloads("ns", kinds=["replicaset"])
    assert any("replicasets" in c for c in fake.cmds)
    fake.calls.clear()
    kube.get_workloads("ns")
    assert not any("replicasets" in c for c in fake.cmds)


def test_get_json_handles_bad_json(fake):
    fake.respond("-o json", stdout="not json")
    assert KubeClient().get_json(["get", "pods"]) == {}


def test_get_pods_readiness(fake):
    data = {"items": [
        {"metadata": {"name": "p1"}, "status": {"phase": "Running",
         "containerStatuses": [{"ready": True}]}},
        {"metadata": {"name": "p2"}, "status": {"phase": "Pending",
         "containerStatuses": [{"ready": False}]}},
    ]}
    fake.respond("-o json", stdout=json.dumps(data))
    pods = KubeClient().get_pods("ns")
    assert pods[0]["ready"] is True
    assert pods[1]["ready"] is False


# -- logs ------------------------------------------------------------------- #


def test_logs_builds_selector_args(fake):
    KubeClient().logs("ns", selector="app=web", tail=10, since="5m",
                      previous=True)
    c = fake.cmds[0]
    assert "logs -n ns" in c
    assert "-l app=web" in c
    assert "--tail 10" in c
    assert "--since 5m" in c
    assert "--previous" in c
    assert "--all-containers=true" in c


def test_logs_pod_target_no_prefix(fake):
    KubeClient().logs("ns", pod="p1", container="c1")
    c = fake.cmds[0]
    assert "p1" in c
    assert "-c c1" in c
    assert "--prefix=true" not in c


# -- exec ------------------------------------------------------------------- #


def test_exec_resolves_selector_to_running_pod(fake):
    data = {"items": [
        {"metadata": {"name": "p1"}, "status": {"phase": "Pending"}},
        {"metadata": {"name": "p2"}, "status": {"phase": "Running",
         "containerStatuses": [{"ready": True}]}},
    ]}
    fake.respond("get pods", stdout=json.dumps(data))
    res = KubeClient().exec("ns", command=["echo", "hi"], selector="app=web")
    # exec targets the running pod p2
    exec_cmd = next(c for c in fake.cmds if "exec" in c)
    assert "p2" in exec_cmd
    assert "-- echo hi" in exec_cmd
    assert res.returncode == 0


def test_exec_no_pod_no_selector_errors(fake):
    res = KubeClient().exec("ns", command=["echo"])
    assert res.returncode == 1
    assert "requires either pod or selector" in res.stderr


def test_exec_selector_no_pods_errors(fake):
    fake.respond("get pods", stdout="{}")
    res = KubeClient().exec("ns", command=["echo"], selector="app=ghost")
    assert res.returncode == 1
    assert "no pods match" in res.stderr


# -- secrets / configmaps --------------------------------------------------- #


def test_secret_apply_generates_then_applies(fake):
    fake.respond("create secret generic tok", stdout="apiVersion: v1\nkind: Secret\n")
    KubeClient().secret_apply("tok", "ns", literals={"k": "v"})
    assert any("create secret generic tok" in c and "--dry-run=client" in c
               for c in fake.cmds)
    assert any("--from-literal=k=v" in c for c in fake.cmds)
    # the rendered yaml is piped back into apply
    assert any(c["input"] and "kind: Secret" in c["input"] for c in fake.calls)


def test_configmap_apply_from_dir_recurses(fake, tmp_path):
    d = tmp_path / "cfg"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "sub" / "b.txt").write_text("b")
    fake.respond("create configmap cm", stdout="apiVersion: v1\nkind: ConfigMap\n")
    KubeClient().configmap_apply("cm", "ns", from_dir=d)
    gen_cmd = next(c for c in fake.cmds if "create configmap cm" in c)
    assert "--from-file=a.txt=" in gen_cmd
    # nested files encode the path separator as ---
    assert "sub---b.txt=" in gen_cmd


def test_secret_delete(fake):
    KubeClient().secret_delete("tok", "ns")
    assert "delete secret tok -n ns" in fake.cmds[0]
    assert "--ignore-not-found" in fake.cmds[0]


# -- helm ------------------------------------------------------------------- #


def test_helm_upgrade_builds_install_args(fake):
    KubeClient().helm_upgrade("rel", "repo/chart", "ns", version="1.2.3",
                              set_values={"a": 1}, repo_name="repo",
                              repo_url="https://x")
    cmds = fake.cmds
    assert any("repo add repo https://x" in c for c in cmds)
    upgrade = next(c for c in cmds if "upgrade --install rel repo/chart" in c)
    assert "--create-namespace" in upgrade
    assert "--version 1.2.3" in upgrade
    assert "--set a=1" in upgrade


def test_helm_upgrade_dry_run_adds_flag_and_does_not_mutate(fake):
    res = KubeClient(dry_run=True).helm_upgrade("rel", "c", "ns")
    upgrade = next(c for c in fake.cmds if "upgrade" in c)
    assert "--dry-run" in upgrade
    assert res.skipped is False  # dry-run render is actually executed


def test_helm_uninstall(fake):
    KubeClient().helm_uninstall("rel", "ns")
    assert "uninstall rel -n ns" in fake.cmds[0]
    assert "--ignore-not-found" in fake.cmds[0]


def test_helm_status_parses_json(fake):
    fake.respond("status rel", stdout=json.dumps({"info": {"status": "deployed"}}))
    out = KubeClient().helm_status("rel", "ns")
    assert out["info"]["status"] == "deployed"


def test_helm_status_bad_json_returns_empty(fake):
    fake.respond("status rel", stdout="oops")
    assert KubeClient().helm_status("rel", "ns") == {}


# -- dry-run skips mutations ------------------------------------------------ #


def test_dry_run_skips_mutating_apply(fake):
    res = KubeClient(dry_run=True).apply_file("m.yaml")
    assert res.skipped is True
    assert fake.calls == []  # nothing actually executed


def test_dry_run_still_runs_reads(fake):
    fake.respond("get namespace prod", returncode=0)
    KubeClient(dry_run=True).namespace_exists("prod")
    assert fake.calls  # the read query ran
