"""Base class and execution context for custom runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .kube import KubeClient


@dataclass
class RunnerContext:
    """Everything a runner hook needs to do its job.

    A fresh context is created for every hook invocation. It exposes the same
    ``kubectl``/``helm`` primitives the engine uses, the resource's namespace,
    the runner's own ``config`` block, and the global ``dry_run`` flag.
    """

    resource: str
    namespace: str
    config: dict = field(default_factory=dict)
    kube: "KubeClient" = None  # type: ignore[assignment]
    console: Any = None
    dry_run: bool = False
    operation: str = ""          # the lifecycle op currently running
    workdir: Path = field(default_factory=Path)  # dir of the resource file
    state: dict = field(default_factory=dict)     # read-only snapshot of kflow state
    extra: dict = field(default_factory=dict)     # resource metadata (phase, labels…)

    # -- logging ----------------------------------------------------------

    def log(self, message: str, *, style: str = "cyan") -> None:
        text = f"    [{style}]·[/{style}] [{style}]{self.resource}[/{style}] {message}"
        if self.console is not None:
            self.console.print(text)
        else:  # pragma: no cover - fallback when no console wired up
            print(f"    · {self.resource} {message}")

    def warn(self, message: str) -> None:
        self.log(message, style="yellow")

    # -- cluster primitives (delegate to KubeClient) ----------------------

    def kubectl(self, args: Sequence[str], **kwargs):
        return self.kube.kubectl(args, **kwargs)

    def helm(self, args: Sequence[str], **kwargs):
        return self.kube.helm(args, **kwargs)

    def apply_manifest(self, text: str):
        return self.kube.apply_stdin(text, namespace=self.namespace)

    def rollout_restart(self, kind: str, name: str):
        return self.kube.rollout_restart(kind, name, self.namespace)

    def kubectl_exec(self, command: Sequence[str], *, selector: Optional[str] = None,
                     pod: Optional[str] = None, container: Optional[str] = None):
        return self.kube.exec(self.namespace, command=list(command),
                              selector=selector, pod=pod, container=container)

    def path(self, relative: str) -> Path:
        """Resolve a path relative to the resource definition file."""
        p = Path(relative)
        return p if p.is_absolute() else (self.workdir / p)


class BaseRunner:
    """Base class for project-specific runners.

    Subclass this and override the lifecycle hooks you need. All hooks receive
    a :class:`RunnerContext`. Defaults are no-ops so a runner only implements
    what it cares about.

    Hook ordering during ``apply``:    ``pre_apply`` → ``apply`` → ``post_apply``
    Hook ordering during ``destroy``:  ``pre_destroy`` → ``destroy`` → ``post_destroy``
    (destroy runs in reverse dependency order).

    ``reload`` defaults to re-running ``apply`` so config-producing runners
    (e.g. "render and apply a Secret") pick up changes without extra code.
    """

    #: Optional registry name; defaults to the class name when empty.
    name: str = ""

    #: Human-readable description shown by ``kflow runners``.
    description: str = ""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}

    # -- apply ------------------------------------------------------------

    def pre_apply(self, ctx: RunnerContext) -> None:
        """Run before the resource's manifests/helm are applied."""

    def apply(self, ctx: RunnerContext) -> None:
        """Main apply work (create database, run migration, …)."""

    def post_apply(self, ctx: RunnerContext) -> None:
        """Run after manifests/helm are applied (seed data, smoke test, …)."""

    # -- destroy ----------------------------------------------------------

    def pre_destroy(self, ctx: RunnerContext) -> None:
        """Run before the resource is torn down (backup, drain, …)."""

    def destroy(self, ctx: RunnerContext) -> None:
        """Main teardown work (drop database, deregister, …)."""

    def post_destroy(self, ctx: RunnerContext) -> None:
        """Run after teardown (cleanup external state, …)."""

    # -- restart / reload -------------------------------------------------

    def restart(self, ctx: RunnerContext) -> None:
        """Restart whatever this runner owns (no config change)."""

    def reload(self, ctx: RunnerContext) -> None:
        """Re-apply config non-destructively. Defaults to ``apply``."""
        self.apply(ctx)

    # -- introspection ----------------------------------------------------

    def status(self, ctx: RunnerContext) -> Optional[str]:
        """Return a short status string (or None)."""
        return None

    def health(self, ctx: RunnerContext) -> bool:
        """Return True if this runner's external state is healthy."""
        return True

    @classmethod
    def registry_name(cls) -> str:
        return cls.name or cls.__name__
