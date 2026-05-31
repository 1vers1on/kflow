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
