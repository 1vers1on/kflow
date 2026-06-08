"""Shared test fixtures.

The whole suite runs without a real cluster by faking ``subprocess.run`` at the
one place every command goes through (``kflow.runners.shell``). The fake records
each invocation and returns canned output so ordering and command shape can be
asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
ROOT_CONFIG = EXAMPLES / "kflow.yaml"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def recorder(monkeypatch):
    """Patch subprocess.run; return the list of recorded {cmd, input} calls."""
    calls = []

    def fake_run(cmd, input=None, capture_output=True, text=True,
                 timeout=None, env=None, cwd=None):
        cmd = [str(c) for c in cmd]
        calls.append({"cmd": cmd, "input": input})
        # `kubectl get namespace X` (no -o json) -> pretend it's missing so that
        # namespace creation is exercised.
        if "get" in cmd and "namespace" in cmd and "-o" not in cmd:
            return FakeProc(returncode=1, stderr="NotFound")
        # JSON queries -> empty list / object.
        if "json" in cmd:
            return FakeProc(returncode=0, stdout="{}")
        return FakeProc(returncode=0, stdout="")

    monkeypatch.setattr("kflow.runners.shell.subprocess.run", fake_run)
    return calls


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect kflow state into a temp dir."""
    return tmp_path / "state"


# A self-contained kflow project that exercises many step kinds. Used by the
# CLI and inspection tests so they never read the real examples/ tree or write
# to the developer's ~/.kflow.
RUNNER_SRC = '''
from kflow.runners import BaseRunner


class DemoRunner(BaseRunner):
    name = "DemoRunner"
    description = "demo runner for tests"

    def apply(self, ctx):
        ctx.log("apply")

    def health(self, ctx):
        return self.config.get("healthy", True)
'''


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temp kflow project and return its root config Path.

    The project keeps state inside the temp dir, declares two phases, and a
    handful of resources covering manifest/helm/secret/runner steps.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "state").mkdir()
    (proj / "manifest.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: m\n"
    )
    (proj / "runner.py").write_text(RUNNER_SRC)

    (proj / "db.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "name: db\nnamespace: data\nphase: base\n"
        "description: a helm-backed database\n"
        "steps:\n"
        "  - name: install\n"
        "    helm:\n"
        "      chart: bitnami/postgres\n"
        "      repo:\n"
        "        name: bitnami\n"
        "        url: https://charts.example.com\n"
    )
    (proj / "web.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "name: web\nnamespace: apps\nphase: app\n"
        "selector: app=web\nworkloads:\n  - deployment/web\n"
        "dependsOn:\n  - db\n"
        "steps:\n"
        "  - name: config\n    manifests:\n      - manifest.yaml\n"
        "  - name: token\n    secret:\n      literals:\n        k: v\n"
        "  - name: migrate\n    dependsOn:\n      - config\n"
        "    runner:\n      class: DemoRunner\n      config:\n        healthy: true\n"
    )

    config = proj / "kflow.yaml"
    config.write_text(
        "kflow:\n  version: v1\n  kind: Config\n"
        f"state:\n  dir: {proj / 'state'}\n"
        "autoCreateNamespace: true\n"
        "runners:\n  - runner.py\n"
        "phases:\n  - name: base\n  - name: app\n"
        "resources:\n  - db.yaml\n  - web.yaml\n"
    )
    return config
