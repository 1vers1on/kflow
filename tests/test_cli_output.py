"""CLI tests that exercise table rendering and exit codes for inspection cmds."""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from kflow.cli import cli


def _invoke(config, *args, input=None):
    return CliRunner().invoke(cli, ["-c", str(config), *args], input=input)


def test_status_table_renders(tmp_project, recorder):
    result = _invoke(tmp_project, "status")
    assert result.exit_code == 0
    assert "status" in result.output
    assert "web" in result.output and "db" in result.output


def test_health_unknown_when_no_workloads(tmp_project, recorder):
    # the fake cluster returns no workloads, so health is "unknown" (not a failure)
    result = _invoke(tmp_project, "health")
    assert result.exit_code == 0
    assert "unknown" in result.output


def test_health_exit_nonzero_when_unhealthy(tmp_path):
    # build a project whose runner reports unhealthy so the command exits non-zero
    (tmp_path / "runner.py").write_text(
        "from kflow.runners import BaseRunner\n\n\n"
        "class Bad(BaseRunner):\n"
        "    name = 'Bad'\n"
        "    def health(self, ctx):\n        return False\n"
    )
    (tmp_path / "r.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "r", "namespace": "ns",
        "steps": [{"name": "go", "runner": {"class": "Bad"}}],
    }))
    config = tmp_path / "kflow.yaml"
    config.write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "Config"},
        "state": {"dir": str(tmp_path / "state")},
        "runners": ["runner.py"],
        "resources": ["r.yaml"],
    }))
    result = _invoke(config, "health")
    assert result.exit_code != 0
    assert "unhealthy" in result.output


def test_validate_warns_on_missing_manifest(tmp_path):
    # manifest path referenced but the file does not exist -> validate warns
    (tmp_path / "r.yaml").write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "ResourceDefinition"},
        "name": "r", "namespace": "ns",
        "steps": [{"name": "go", "manifests": ["missing.yaml"]}],
    }))
    config = tmp_path / "kflow.yaml"
    config.write_text(yaml.safe_dump({
        "kflow": {"version": "v1", "kind": "Config"},
        "state": {"dir": str(tmp_path / "state")},
        "resources": ["r.yaml"],
    }))
    result = _invoke(config, "validate")
    assert result.exit_code == 0
    assert "path not found" in result.output


def test_logs_command_outputs(tmp_project, recorder):
    result = _invoke(tmp_project, "logs", "web", "--tail", "5")
    assert result.exit_code == 0
