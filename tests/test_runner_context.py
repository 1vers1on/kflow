"""Tests for the RunnerContext primitives and BaseRunner default hooks."""

from __future__ import annotations

from pathlib import Path

from kflow.runners.base import BaseRunner, RunnerContext


class _FakeKube:
    def __init__(self):
        self.calls = []

    def kubectl(self, args, **kw):
        self.calls.append(("kubectl", list(args), kw))
        return "kubectl-result"

    def helm(self, args, **kw):
        self.calls.append(("helm", list(args), kw))
        return "helm-result"

    def apply_stdin(self, text, *, namespace=None):
        self.calls.append(("apply_stdin", text, namespace))
        return "applied"

    def rollout_restart(self, kind, name, namespace):
        self.calls.append(("rollout_restart", kind, name, namespace))

    def exec(self, namespace, *, command, selector=None, pod=None, container=None):
        self.calls.append(("exec", namespace, command, selector, pod, container))
        return "exec-result"


def _ctx(**kw):
    kube = _FakeKube()
    ctx = RunnerContext(resource="r", namespace="ns", kube=kube, **kw)
    return ctx, kube


def test_context_kubectl_delegates():
    ctx, kube = _ctx()
    assert ctx.kubectl(["get", "pods"]) == "kubectl-result"
    assert kube.calls[0][0] == "kubectl"


def test_context_helm_delegates():
    ctx, kube = _ctx()
    ctx.helm(["status", "r"])
    assert kube.calls[0][0] == "helm"


def test_context_apply_manifest_uses_namespace():
    ctx, kube = _ctx()
    ctx.apply_manifest("kind: ConfigMap")
    assert kube.calls[0] == ("apply_stdin", "kind: ConfigMap", "ns")


def test_context_rollout_restart_uses_namespace():
    ctx, kube = _ctx()
    ctx.rollout_restart("Deployment", "web")
    assert kube.calls[0] == ("rollout_restart", "Deployment", "web", "ns")


def test_context_kubectl_exec_passes_selector():
    ctx, kube = _ctx()
    ctx.kubectl_exec(["sh", "-c", "echo hi"], selector="app=x")
    name, ns, command, selector, pod, container = kube.calls[0]
    assert name == "exec" and ns == "ns"
    assert command == ["sh", "-c", "echo hi"]
    assert selector == "app=x"


def test_context_path_resolves_relative_to_workdir(tmp_path):
    ctx, _ = _ctx(workdir=tmp_path)
    assert ctx.path("sub/file.txt") == tmp_path / "sub" / "file.txt"
    assert ctx.path("/abs/x") == Path("/abs/x")


def test_context_log_writes_to_console():
    class Rec:
        def __init__(self):
            self.printed = []

        def print(self, text):
            self.printed.append(text)

    rec = Rec()
    ctx = RunnerContext(resource="r", namespace="ns", console=rec)
    ctx.log("hello")
    ctx.warn("careful")
    assert any("hello" in p for p in rec.printed)
    assert any("careful" in p and "yellow" in p for p in rec.printed)


def test_context_log_without_console_uses_print(capsys):
    ctx = RunnerContext(resource="r", namespace="ns", console=None)
    ctx.log("plain")
    assert "plain" in capsys.readouterr().out


# -- BaseRunner default hooks ----------------------------------------------- #


def test_base_runner_hooks_are_noops():
    r = BaseRunner()
    ctx = RunnerContext(resource="r", namespace="ns")
    # none of these should raise
    for hook in ("pre_apply", "apply", "post_apply", "pre_destroy", "destroy",
                 "post_destroy", "restart"):
        getattr(r, hook)(ctx)
    assert r.status(ctx) is None
    assert r.health(ctx) is True


def test_base_runner_config_defaults_to_empty():
    assert BaseRunner().config == {}
    assert BaseRunner({"a": 1}).config == {"a": 1}
