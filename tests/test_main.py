"""Tests for the console-script entry point and its exit codes."""

from __future__ import annotations

from kflow.cli import main


def test_main_success_returns_zero(tmp_project, recorder):
    assert main(["-c", str(tmp_project), "list"]) == 0


def test_main_click_error_returns_one(capsys):
    rc = main(["-c", "/nope/kflow.yaml", "list"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_main_abort_returns_130(tmp_project, recorder, monkeypatch):
    import click

    def boom(*a, **k):
        raise click.exceptions.Abort()

    monkeypatch.setattr("kflow.cli.cli.main", boom)
    assert main(["-c", str(tmp_project), "list"]) == 130


def test_main_kflow_error_returns_one(tmp_project, recorder):
    # an unknown resource pattern raises KflowError out of resolve_targets
    rc = main(["-c", str(tmp_project), "status", "ghost*"])
    assert rc == 1
