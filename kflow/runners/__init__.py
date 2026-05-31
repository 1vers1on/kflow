"""kflow custom-runner API (sub-library).

This package is the public, stable surface that project-specific runner files
import from. Custom runners subclass :class:`BaseRunner` and implement the
lifecycle hooks they care about (``apply``, ``destroy``, ``restart``,
``reload`` and the ``pre_*``/``post_*`` variants).

The shell / kubectl / helm helpers (:class:`KubeClient`, :func:`run_command`)
are intentionally part of this sub-library: they are shared by both the kflow
core engine and by custom runners, so a runner author has the exact same
primitives the engine uses.

Example
-------
::

    from kflow.runners import BaseRunner

    class SeedDatabase(BaseRunner):
        def post_apply(self, ctx):
            ctx.log("seeding database")
            ctx.kubectl_exec(
                selector="app=postgres",
                command=["psql", "-f", "/seed/schema.sql"],
            )
"""

from .shell import CommandError, CommandResult, format_command, run_command
from .kube import KubeClient
from .base import BaseRunner, RunnerContext
from .registry import RunnerRegistry
from . import helpers

__all__ = [
    "BaseRunner",
    "RunnerContext",
    "RunnerRegistry",
    "KubeClient",
    "CommandError",
    "CommandResult",
    "run_command",
    "format_command",
    "helpers",
]
