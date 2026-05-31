"""Low-level subprocess execution used by kflow and custom runners."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Sequence


def format_command(cmd: Sequence[str]) -> str:
    """Render a command list as a copy-pasteable shell string."""
    return " ".join(shlex.quote(str(part)) for part in cmd)


@dataclass
class CommandResult:
    """The outcome of running a command."""

    cmd: list = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False  # True when not executed (e.g. dry-run)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def pretty(self) -> str:
        return format_command(self.cmd)


class CommandError(RuntimeError):
    """Raised when a checked command exits non-zero (or cannot be run)."""

    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = f"command failed (exit {returncode}): {format_command(cmd)}"
        detail = (stderr or stdout or "").strip()
        if detail:
            message = f"{message}\n{detail}"
        super().__init__(message)


def run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    input_text: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> CommandResult:
    """Run ``cmd`` and return a :class:`CommandResult`.

    When ``capture`` is False the child inherits stdout/stderr (used for
    streaming, e.g. ``kubectl logs -f``). ``check`` raises
    :class:`CommandError` on a non-zero exit.
    """
    cmd = [str(part) for part in cmd]
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=capture,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        stderr = f"executable not found: {cmd[0]} ({exc})"
        if check:
            raise CommandError(cmd, 127, "", stderr) from exc
        # Unchecked callers (read queries) degrade gracefully instead of
        # crashing when kubectl/helm is absent or the cluster is unreachable.
        return CommandResult(cmd=cmd, returncode=127, stdout="", stderr=stderr)

    result = CommandResult(
        cmd=cmd,
        returncode=proc.returncode,
        stdout=(proc.stdout or "") if capture else "",
        stderr=(proc.stderr or "") if capture else "",
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, result.stdout, result.stderr)
    return result
