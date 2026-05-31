"""Example custom runner.

Project-specific behaviour (create a database, seed it, run migrations) lives
here, not in kflow itself. Registered globally in the root ``kflow.yaml`` and
attached to the ``app`` resource as a ``migrate`` step.

Every hook receives a :class:`~kflow.runners.RunnerContext` (``ctx``) exposing
the same kubectl/helm primitives the engine uses, plus the resource namespace,
this runner's ``config`` block, and the ``dry_run`` flag (mutating ``ctx.*``
calls are automatically skipped in dry-run).
"""

from __future__ import annotations

from kflow.runners import BaseRunner


class DatabaseRunner(BaseRunner):
    """Create an application database and optionally seed / migrate it."""

    description = "Create an app database and (optionally) seed/migrate it."

    @property
    def _selector(self) -> str:
        return self.config.get("selector", "app=postgres")

    @property
    def _database(self) -> str:
        return self.config.get("database", "appdb")

    def apply(self, ctx):
        db = self._database
        ctx.log(f"ensuring database {db!r} exists")
        ctx.kubectl_exec(
            ["sh", "-c", f"createdb {db} 2>/dev/null || true"],
            selector=self._selector,
        )

    def post_apply(self, ctx):
        if self.config.get("seed"):
            ctx.log(f"seeding database {self._database!r}")
            ctx.kubectl_exec(
                ["sh", "-c", "echo '-- seed data would run here --'"],
                selector=self._selector,
            )

    def reload(self, ctx):
        # Reload = run migrations to pick up new schema, without dropping data.
        ctx.log(f"running migrations on {self._database!r}")
        ctx.kubectl_exec(
            ["sh", "-c", "echo '-- migrations would run here --'"],
            selector=self._selector,
        )

    def destroy(self, ctx):
        db = self._database
        ctx.log(f"dropping database {db!r}")
        ctx.kubectl_exec(
            ["sh", "-c", f"dropdb {db} 2>/dev/null || true"],
            selector=self._selector,
        )

    def health(self, ctx):
        res = ctx.kubectl(
            ["get", "pods", "-n", ctx.namespace, "-l", self._selector],
            check=False,
        )
        return res.returncode == 0
