"""End-to-end CLI tests driving the click command group with a faked cluster."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from kflow._version import __version__
from kflow.cli import cli


@pytest.fixture
def run(tmp_project, recorder):
    """Invoke the CLI against the temp project with subprocess faked out."""
    runner = CliRunner()

    def _invoke(*args, input=None):
        return runner.invoke(cli, ["-c", str(tmp_project), *args], input=input)

    _invoke.calls = recorder
    return _invoke


# -- version (regression test for the stale 1.0.3 literal) ------------------ #


def test_version_flag_matches_package_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    # the old hardcoded literal must never come back
    assert "1.0.3" not in result.output


# -- read-only inspection commands ------------------------------------------ #


def test_list_command(run):
    result = run("list")
    assert result.exit_code == 0
    assert "db" in result.output
    assert "web" in result.output


def test_plan_command(run):
    result = run("plan")
    assert result.exit_code == 0
    # db (phase base) is planned before web (phase app)
    assert result.output.index("db") < result.output.index("web")


def test_plan_targeted_pulls_deps(run):
    result = run("plan", "web")
    assert result.exit_code == 0
    assert "db" in result.output  # web dependsOn db


def test_graph_tree(run):
    result = run("graph", "--format", "tree")
    assert result.exit_code == 0
    assert "phase" in result.output


def test_graph_order(run):
    result = run("graph", "--format", "order")
    assert result.exit_code == 0
    assert "execution order" in result.output


def test_graph_dot(run):
    result = run("graph", "--format", "dot")
    assert result.exit_code == 0
    assert "digraph kflow" in result.output
    assert "->" in result.output


def test_validate_ok(run):
    result = run("validate")
    assert result.exit_code == 0
    assert "config OK" in result.output


def test_runners_listed(run):
    result = run("runners")
    assert result.exit_code == 0
    assert "DemoRunner" in result.output


# -- state subcommands ------------------------------------------------------ #


def test_state_path(run):
    result = run("state", "path")
    assert result.exit_code == 0
    assert "state.json" in result.output


def test_state_show_empty(run):
    result = run("state", "show")
    assert result.exit_code == 0
    assert "resources" in result.output


def test_state_clear_requires_confirmation(run):
    # declining the prompt leaves state untouched and exits cleanly
    result = run("state", "clear", input="n\n")
    assert result.exit_code == 0


def test_state_clear_with_yes(tmp_project, recorder):
    runner = CliRunner()
    result = runner.invoke(cli, ["-c", str(tmp_project), "-y", "state", "clear"])
    assert result.exit_code == 0
    assert "state cleared" in result.output


# -- lifecycle commands ----------------------------------------------------- #


def test_apply_dry_run(run):
    result = run("--dry-run", "apply")
    # NOTE: global flags must precede the subcommand for click groups
    assert result.exit_code == 0


def test_apply_via_cli_writes_state(run, tmp_project):
    result = run("apply", "--no-wait")
    assert result.exit_code == 0
    assert "done" in result.output
    state_file = tmp_project.parent / "state" / "state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    resources = next(iter(data["clusters"].values()))["resources"]
    assert resources["web"]["status"] == "applied"


def test_destroy_declined_does_nothing(run):
    result = run("destroy", input="n\n")
    assert result.exit_code == 0
    # nothing destroyed -> no helm uninstall recorded
    cmds = [" ".join(c["cmd"]) for c in run.calls]
    assert not any("uninstall" in c for c in cmds)


def test_destroy_confirmed(run):
    result = run("destroy", input="y\n")
    assert result.exit_code == 0
    cmds = [" ".join(c["cmd"]) for c in run.calls]
    assert any("helm" in c and "uninstall" in c for c in cmds)


def test_helm_command_runs_upgrade(run):
    result = run("helm")
    assert result.exit_code == 0
    cmds = [" ".join(c["cmd"]) for c in run.calls]
    assert any("upgrade" in c and "--install" in c for c in cmds)


def test_reload_via_cli(run):
    result = run("reload", "web", "--no-wait")
    assert result.exit_code == 0


def test_restart_via_cli(run):
    result = run("restart", "web", "--no-wait")
    assert result.exit_code == 0
    cmds = [" ".join(c["cmd"]) for c in run.calls]
    assert any("rollout restart deployment/web" in c for c in cmds)


# -- error handling --------------------------------------------------------- #


def test_unknown_resource_errors(run):
    result = run("apply", "does-not-exist")
    assert result.exit_code != 0


def test_missing_config_errors():
    result = CliRunner().invoke(cli, ["-c", "/nope/kflow.yaml", "list"])
    assert result.exit_code != 0


def test_apply_unknown_pattern_is_clean_error(run):
    result = run("status", "ghost*")
    # no match -> KflowError surfaced as a click error, not a traceback
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
