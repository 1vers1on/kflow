"""Tests for the rich rendering helpers (tree, dot, order)."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from kflow.core import Kflow
from kflow.render import render_dot, render_order, render_tree

from .conftest import ROOT_CONFIG


def _engine():
    return Kflow.load(ROOT_CONFIG)


def _to_text(renderable) -> str:
    console = Console(width=200, record=True)
    console.print(renderable)
    return console.export_text()


def test_render_tree_is_tree_and_lists_resources():
    tree = render_tree(_engine())
    assert isinstance(tree, Tree)
    text = _to_text(tree)
    for name in ("longhorn-storage", "traefik", "app"):
        assert name in text
    assert "phase" in text


def test_render_order_is_table_with_a_row_per_node():
    engine = _engine()
    table = render_order(engine)
    assert isinstance(table, Table)
    assert table.row_count == len(engine.graph.node_order)


def test_render_dot_structure():
    engine = _engine()
    dot = render_dot(engine)
    assert dot.startswith("digraph kflow {")
    assert dot.rstrip().endswith("}")
    assert "rankdir=LR;" in dot
    # one subgraph cluster per non-empty phase
    assert dot.count("subgraph cluster_") >= 1
    # every edge is rendered
    assert dot.count("->") == len(engine.graph.edges)


def test_render_dot_includes_all_nodes():
    engine = _engine()
    dot = render_dot(engine)
    for nid in engine.graph.node_order:
        assert f'"{nid}"' in dot
