"""Rich rendering: tree, dot graph, and execution order table."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List

from rich import box
from rich.table import Table
from rich.tree import Tree

from .models import DEFAULT_PHASE

if TYPE_CHECKING:
    from .engine import Kflow


def render_tree(engine: Kflow) -> Tree:
    cfg = engine.config
    root = Tree(f"[bold]{cfg.path.name}[/bold] [dim]({len(cfg.resources)} resources)[/dim]")
    by_phase: Dict[str, List] = defaultdict(list)
    for rname in engine.graph.resource_order:
        res = cfg.resource_map[rname]
        by_phase[res.phase_name].append(res)
    for pidx, pname in enumerate(engine.graph.phase_names):
        if pname not in by_phase:
            continue
        label = "default" if pname == DEFAULT_PHASE else pname
        pnode = root.add(f"[bold magenta]phase {pidx}: {label}[/bold magenta]")
        for res in by_phase[pname]:
            deps = engine.graph.res_depends.get(res.name, set())
            dep_txt = f"  [dim]→ {', '.join(sorted(deps))}[/dim]" if deps else ""
            rnode = pnode.add(
                f"[cyan]{res.name}[/cyan] [dim]ns={res.namespace}[/dim]{dep_txt}"
            )
            for step in res.steps:
                sdeps = f" [dim]depends: {', '.join(step.depends_on)}[/dim]" if step.depends_on else ""
                rnode.add(f"[green]{step.name}[/green] [dim]({step.kind})[/dim]{sdeps}")
    return root


def render_dot(engine: Kflow) -> str:
    lines = ["digraph kflow {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
    for pidx, pname in enumerate(engine.graph.phase_names):
        nodes = [n for n in engine.graph.node_order
                 if engine.graph.phase_idx[n] == pidx]
        if not nodes:
            continue
        label = "default" if pname == DEFAULT_PHASE else pname
        lines.append(f'  subgraph cluster_{pidx} {{ label="phase {pidx}: {label}";')
        for n in nodes:
            lines.append(f'    "{n}";')
        lines.append("  }")
    for dependent, dependency in engine.graph.edges:
        lines.append(f'  "{dependency}" -> "{dependent}";')
    lines.append("}")
    return "\n".join(lines)


def render_order(engine: Kflow) -> Table:
    table = Table(box=box.SIMPLE, title="execution order (apply)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("phase", style="magenta")
    table.add_column("resource", style="cyan")
    table.add_column("step", style="green")
    table.add_column("kind", style="dim")
    table.add_column("depends on", style="dim")
    for i, nid in enumerate(engine.graph.node_order, 1):
        res = engine.config.resource_map[engine.graph.node_res[nid]]
        step = engine.graph.node_step[nid]
        label = "default" if res.phase_name == DEFAULT_PHASE else res.phase_name
        table.add_row(str(i), label, res.name, step.name, step.kind,
                      ", ".join(step.depends_on) or "-")
    return table
