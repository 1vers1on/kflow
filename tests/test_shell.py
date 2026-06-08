"""Tests for the low-level subprocess wrapper (kflow.runners.shell)."""

from __future__ import annotations

import pytest

from kflow.runners.shell import (
    CommandError,
    CommandResult,
    format_command,
    run_command,
)


def test_format_command_quotes_spaces():
    assert format_command(["echo", "a b"]) == "echo 'a b'"


def test_command_result_ok_and_pretty():
    r = CommandResult(cmd=["ls", "-la"], returncode=0)
    assert r.ok is True
    assert r.pretty == "ls -la"
    assert CommandResult(cmd=["x"], returncode=2).ok is False


def test_run_command_success():
    r = run_command(["true"])
    assert r.returncode == 0
    assert r.ok


def test_run_command_captures_stdout():
    r = run_command(["printf", "hello"])
    assert r.stdout == "hello"


def test_run_command_check_raises_on_failure():
    with pytest.raises(CommandError) as exc:
        run_command(["sh", "-c", "exit 3"])
    assert exc.value.returncode == 3


def test_run_command_unchecked_returns_nonzero():
    r = run_command(["sh", "-c", "exit 3"], check=False)
    assert r.returncode == 3
    assert not r.ok


def test_run_command_missing_executable_checked_raises():
    with pytest.raises(CommandError) as exc:
        run_command(["this-binary-does-not-exist-xyz"])
    assert exc.value.returncode == 127


def test_run_command_missing_executable_unchecked_degrades():
    r = run_command(["this-binary-does-not-exist-xyz"], check=False)
    assert r.returncode == 127
    assert "not found" in r.stderr


def test_run_command_no_capture_leaves_streams_empty():
    r = run_command(["printf", "hi"], capture=False)
    assert r.stdout == ""
    assert r.returncode == 0


def test_run_command_input_text():
    r = run_command(["cat"], input_text="piped")
    assert r.stdout == "piped"


def test_command_error_message_includes_detail():
    err = CommandError(["kubectl", "apply"], 1, "", "boom")
    assert "exit 1" in str(err)
    assert "boom" in str(err)
