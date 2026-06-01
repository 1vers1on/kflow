"""Click command-line interface and entry point."""

from __future__ import annotations

import functools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .engine import Kflow
from .loader import _is_url, load_root_config
from .models import ConfigError, DEFAULT_PHASE, KflowError
from .render import render_dot, render_order, render_tree
from .runners.registry import RunnerLoadError
from .runners.shell import CommandError

__version__ = "1.0.3"

console = Console()
err_console = Console(stderr=True)


@dataclass
class AppCtx:
    config_path: str
    dry_run: bool
    context: Optional[str]
    verbose: bool
    assume_yes: bool
    _engine: Optional[Kflow] = None

    def engine(self) -> Kflow:
        if self._engine is None:
            try:
                self._engine = Kflow.load(
                    self.config_path, dry_run=self.dry_run,
                    context=self.context, verbose=self.verbose,
                )
            except (ConfigError, RunnerLoadError) as exc:
                raise click.ClickException(str(exc))
            for warning in self._engine.graph.warnings:
                err_console.print(f"[yellow]warning:[/yellow] {warning}")
        return self._engine


pass_app = click.make_pass_decorator(AppCtx)


def _handle_errors(func):
    """Wrap a command body to render kflow/command errors cleanly."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (KflowError, RunnerLoadError) as exc:
            raise click.ClickException(str(exc))
        except CommandError as exc:
            raise click.ClickException(str(exc))
    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="kflow")
@click.option("-c", "--config", "config_path", default="kflow.yaml",
              show_default=True, envvar="KFLOW_CONFIG",
              help="Path to the root kflow config file.")
@click.option("--dry-run", is_flag=True,
              help="Print mutating commands without executing them.")
@click.option("--context", default=None, envvar="KFLOW_CONTEXT",
              help="kubeconfig context to use (overrides config).")
@click.option("-v", "--verbose", is_flag=True, help="Show command output.")
@click.option("-y", "--yes", "assume_yes", is_flag=True,
              help="Do not prompt for confirmation.")
@click.pass_context
def cli(ctx, config_path, dry_run, context, verbose, assume_yes):
    """kflow - declarative Kubernetes workflow orchestration."""
    ctx.obj = AppCtx(config_path=config_path, dry_run=dry_run, context=context,
                     verbose=verbose, assume_yes=assume_yes)


# -- lifecycle commands ----------------------------------------------------- #


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True, help="Do not pull in dependencies of selected resources.")
@click.option("--no-wait", is_flag=True, help="Do not wait for rollouts to become ready.")
@click.option("--timeout", default=300, show_default=True, help="Rollout wait timeout (seconds).")
@pass_app
@_handle_errors
def apply(app, names, no_deps, no_wait, timeout):
    """Apply manifests and helm charts in dependency order."""
    app.engine().apply(list(names), with_deps=not no_deps,
                       wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True, help="Do not pull in dependents of selected resources.")
@click.option("--delete-namespaces", is_flag=True,
              help="Also delete namespaces (skips 'default' and keepNamespace resources).")
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def destroy(app, names, no_deps, delete_namespaces, timeout):
    """Tear down resources in reverse dependency order."""
    engine = app.engine()
    targets = engine.resolve_targets(list(names), operation="destroy",
                                     with_deps=not no_deps)
    if not _confirm(app, f"Destroy {len(targets)} resource(s): {', '.join(targets)}?"):
        return
    engine.destroy(list(names), with_deps=not no_deps,
                   delete_namespaces=delete_namespaces, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--with-deps", is_flag=True, help="Also restart dependencies.")
@click.option("--no-wait", is_flag=True)
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def restart(app, names, with_deps, no_wait, timeout):
    """Rollout-restart pods without applying any configuration."""
    app.engine().restart(list(names), with_deps=with_deps,
                         wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@click.option("--no-wait", is_flag=True)
@click.option("--timeout", default=300, show_default=True)
@pass_app
@_handle_errors
def reload(app, names, no_deps, no_wait, timeout):
    """Re-apply config non-destructively, then restart affected pods."""
    app.engine().reload(list(names), with_deps=not no_deps,
                        wait=not no_wait, timeout=timeout)
    _done(app)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@pass_app
@_handle_errors
def helm(app, names, no_deps):
    """Run helm upgrade --install for helm-backed resources."""
    app.engine().helm_sync(list(names), with_deps=not no_deps)
    _done(app)


# -- inspection commands ---------------------------------------------------- #


@cli.command()
@click.argument("names", nargs=-1)
@pass_app
@_handle_errors
def status(app, names):
    """Show kflow state and live workload readiness."""
    rows = app.engine().status(list(names))
    table = Table(box=box.SIMPLE, title="status")
    for col in ("resource", "phase", "namespace", "state", "helm", "ready", "drift", "last applied"):
        table.add_column(col)
    for r in rows:
        drift = f"[yellow]{r['drift']}[/yellow]" if r["drift"] else "0"
        state_style = {"applied": "green", "destroyed": "red"}.get(r["state"], "yellow")
        table.add_row(r["name"], r["phase"], r["namespace"],
                      f"[{state_style}]{r['state']}[/{state_style}]",
                      r["helm"], r["workloads"], drift, r["last"])
    console.print(table)


@cli.command()
@click.argument("names", nargs=-1)
@pass_app
@_handle_errors
def health(app, names):
    """Check workload (and runner) health; exit non-zero if unhealthy."""
    results = app.engine().health(list(names))
    table = Table(box=box.SIMPLE, title="health")
    for col in ("resource", "namespace", "health", "detail"):
        table.add_column(col)
    unhealthy = 0
    for r in results:
        if r["healthy"] is True:
            mark = "[green]healthy[/green]"
        elif r["healthy"] is False:
            mark = "[red]unhealthy[/red]"
            unhealthy += 1
        else:
            mark = "[dim]unknown[/dim]"
        table.add_row(r["name"], r["namespace"], mark, r["detail"])
    console.print(table)
    if unhealthy:
        raise click.ClickException(f"{unhealthy} resource(s) unhealthy")


@cli.command()
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Stream logs.")
@click.option("--tail", type=int, default=None, help="Lines of recent logs to show.")
@click.option("--since", default=None, help="Show logs since e.g. 10m, 1h.")
@click.option("-c", "--container", default=None, help="Container name.")
@click.option("--selector", default=None, help="Override label selector.")
@click.option("--previous", is_flag=True, help="Show logs from a previous container.")
@pass_app
@_handle_errors
def logs(app, name, follow, tail, since, container, selector, previous):
    """Tail or fetch logs for a resource's pods."""
    result = app.engine().logs(name, follow=follow, tail=tail, since=since,
                               container=container, selector=selector,
                               previous=previous)
    if not follow and result.stdout:
        console.print(result.stdout.rstrip())
    if result.returncode != 0 and result.stderr:
        err_console.print(f"[yellow]{result.stderr.strip()}[/yellow]")


@cli.command()
@click.option("--format", "fmt", type=click.Choice(["tree", "order", "dot"]),
              default="tree", show_default=True, help="Rendering format.")
@pass_app
@_handle_errors
def graph(app, fmt):
    """Render the dependency tree / execution order."""
    engine = app.engine()
    if fmt == "tree":
        console.print(render_tree(engine))
    elif fmt == "order":
        console.print(render_order(engine))
    else:
        click.echo(render_dot(engine))


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--no-deps", is_flag=True)
@pass_app
@_handle_errors
def plan(app, names, no_deps):
    """Show the resolved execution order for a selection."""
    engine = app.engine()
    targets = set(engine.resolve_targets(list(names), operation="apply",
                                         with_deps=not no_deps))
    table = Table(box=box.SIMPLE, title="plan")
    for col in ("#", "phase", "resource", "step", "kind"):
        table.add_column(col)
    i = 0
    for nid in engine.graph.node_order:
        if engine.graph.node_res[nid] not in targets:
            continue
        i += 1
        res = engine.config.resource_map[engine.graph.node_res[nid]]
        step = engine.graph.node_step[nid]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        table.add_row(str(i), label, res.name, step.name, step.kind)
    console.print(table)


@cli.command(name="list")
@pass_app
@_handle_errors
def list_(app):
    """List phases and resources."""
    engine = app.engine()
    table = Table(box=box.SIMPLE, title="resources")
    for col in ("resource", "phase", "namespace", "steps", "depends on"):
        table.add_column(col)
    for rname in engine.graph.resource_order:
        res = engine.config.resource_map[rname]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        deps = ", ".join(sorted(engine.graph.res_depends.get(rname, set()))) or "-"
        table.add_row(rname, label, res.namespace,
                      ", ".join(s.name for s in res.steps), deps)
    console.print(table)


@cli.command()
@pass_app
@_handle_errors
def validate(app):
    """Validate configuration and report warnings."""
    engine = app.engine()
    console.print(
        f"[green]✓[/green] config OK: {len(engine.config.resources)} resources, "
        f"{len([p for p in engine.graph.phase_names if p != DEFAULT_PHASE])} declared phases, "
        f"{len(engine.graph.node_order)} steps"
    )
    missing = []
    for res in engine.config.resources:
        for step in res.steps:
            for m in step.manifests:
                if _is_url(m):
                    continue  # can't check remote URLs at validate time
                if not Path(m).exists():
                    missing.append(str(m))
            if step.kind == "kustomize" and step.kustomize:
                if not step.kustomize.path.exists():
                    missing.append(str(step.kustomize.path))
            if step.kind == "docker-build" and step.docker_build:
                if not step.docker_build.context.exists():
                    missing.append(str(step.docker_build.context))
    if missing:
        for m in missing:
            err_console.print(f"[yellow]warning:[/yellow] path not found: {m}")
    if engine.graph.warnings:
        for w in engine.graph.warnings:
            err_console.print(f"[yellow]warning:[/yellow] {w}")
    elif not missing:
        console.print("[green]✓[/green] no warnings")


@cli.command()
@pass_app
@_handle_errors
def runners(app):
    """List custom runners discovered from the configuration."""
    engine = app.engine()
    table = Table(box=box.SIMPLE, title="runners")
    for col in ("name", "description", "source"):
        table.add_column(col)
    for name, cls in engine.registry.items():
        src = getattr(cls, "__kflow_source__", "?")
        table.add_row(name, cls.description or "-", str(src))
    if not engine.registry.items():
        console.print("[dim]no runners registered[/dim]")
    else:
        console.print(table)


@cli.group()
def state():
    """Inspect or manage local kflow state."""


@state.command("show")
@pass_app
@_handle_errors
def state_show(app):
    """Print the local state for the active cluster."""
    engine = app.engine()
    console.print(Panel(
        json.dumps(engine.state.cluster, indent=2),
        title=f"state: {engine.state.path} [{engine.state.cluster_key}]",
        border_style="dim",
    ))


@state.command("path")
@pass_app
@_handle_errors
def state_path(app):
    """Print the path to the state file."""
    click.echo(str(app.engine().state.path))


@state.command("clear")
@pass_app
@_handle_errors
def state_clear(app):
    """Clear local state for the active cluster."""
    engine = app.engine()
    if not _confirm(app, f"Clear state for cluster {engine.state.cluster_key!r}?"):
        return
    engine.state.clear()
    console.print("[green]✓[/green] state cleared")


# -- helpers ---------------------------------------------------------------- #


def _confirm(app: AppCtx, message: str) -> bool:
    if app.assume_yes or app.dry_run:
        return True
    return click.confirm(message, default=False)


def _done(app: AppCtx) -> None:
    if app.dry_run:
        console.print("[dim](dry-run: no changes were made)[/dim]")
    else:
        console.print("[green]✓ done[/green]")


def main(argv=None) -> int:
    """Console-script entry point."""
    try:
        cli.main(args=argv, prog_name="kflow", standalone_mode=False)
        return 0
    except click.ClickException as exc:
        err_console.print(f"[red]error:[/red] {exc.format_message()}")
        return 1
    except click.exceptions.Abort:
        err_console.print("[red]aborted[/red]")
        return 130
    except KflowError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
