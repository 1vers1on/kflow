"""Dependency graph with phase ordering and cycle-breaking."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from .models import ConfigError, DEFAULT_PHASE, ResourceDef, RootConfig, StepDef


class DependencyGraph:
    """Step-level dependency graph with strict phase ordering.

    Nodes are ``"resource.step"`` ids. Edges encode "depends on" relationships
    (the dependency runs first). Phases are a strict outer ordering: every step
    of phase *N* runs before any step of phase *N+1*. Backward cross-phase
    dependencies are satisfied automatically; forward ones are reported and
    ignored (this is how circular relationships like longhorn↔traefik are
    resolved without erroring). Genuine same-phase cycles are broken
    deterministically with a warning rather than raising.
    """

    def __init__(self, config: RootConfig):
        self.config = config
        self.resources = config.resource_map
        self.warnings: List[str] = []

        self._assign_phases()
        self._build_nodes()
        self._build_edges()
        self.node_order = self._compute_order()
        self._build_resource_views()

    # -- phases -----------------------------------------------------------

    def _assign_phases(self) -> None:
        phase_names = [p.name for p in self.config.phases]
        needs_default = any(r.phase is None for r in self.config.resources)
        if needs_default or not phase_names:
            phase_names = phase_names + [DEFAULT_PHASE]
        self.phase_names = phase_names
        index = {name: i for i, name in enumerate(phase_names)}
        for res in self.config.resources:
            pname = res.phase or DEFAULT_PHASE
            if pname not in index:
                raise ConfigError(
                    f"resource {res.name!r} references unknown phase {pname!r}. "
                    f"Declared phases: {', '.join(n for n in phase_names if n != DEFAULT_PHASE) or '(none)'}"
                )
            res.phase_name = pname
            res.phase_index = index[pname]

    # -- nodes / edges ----------------------------------------------------

    @staticmethod
    def node_id(resource: str, step: str) -> str:
        return f"{resource}.{step}"

    def _build_nodes(self) -> None:
        self.nodes: List[str] = []
        self.node_res: Dict[str, str] = {}
        self.node_step: Dict[str, StepDef] = {}
        self.phase_idx: Dict[str, int] = {}
        self.seq: Dict[str, int] = {}
        counter = 0
        for res in self.config.resources:
            for step in res.steps:
                nid = self.node_id(res.name, step.name)
                self.nodes.append(nid)
                self.node_res[nid] = res.name
                self.node_step[nid] = step
                self.phase_idx[nid] = res.phase_index
                self.seq[nid] = counter
                counter += 1

    def _resolve_ref(self, ref: str, resource: ResourceDef) -> str:
        """Resolve a dependency reference to a node id."""
        if "." in ref:
            res_name, _, step_name = ref.partition(".")
            target = self.node_id(res_name, step_name)
            if target not in self.node_step:
                raise ConfigError(
                    f"{resource.name!r} depends on unknown step {ref!r}"
                )
            return target
        # bare ref: prefer a step in the same resource, else a resource name.
        same = self.node_id(resource.name, ref)
        if same in self.node_step:
            return same
        if ref in self.resources:
            return self._last_node(ref)
        raise ConfigError(
            f"{resource.name!r} depends on unknown step/resource {ref!r}"
        )

    def _last_node(self, resource_name: str) -> str:
        res = self.resources[resource_name]
        if not res.steps:
            raise ConfigError(
                f"resource {resource_name!r} has no steps but is used as a dependency"
            )
        return self.node_id(resource_name, res.steps[-1].name)

    def _first_node(self, resource_name: str) -> str:
        res = self.resources[resource_name]
        if not res.steps:
            raise ConfigError(f"resource {resource_name!r} has no steps")
        return self.node_id(resource_name, res.steps[0].name)

    def _build_edges(self) -> None:
        # edges as (dependent, dependency): dependency must run before dependent
        self.edges: List[tuple] = []
        for res in self.config.resources:
            # sequential ordering within a resource
            for i, step in enumerate(res.steps):
                nid = self.node_id(res.name, step.name)
                if i > 0:
                    prev = self.node_id(res.name, res.steps[i - 1].name)
                    self.edges.append((nid, prev))
                for ref in step.depends_on:
                    self.edges.append((nid, self._resolve_ref(ref, res)))
            # resource-level dependency: first step waits for the whole target
            if res.steps:
                first = self._first_node(res.name)
                for ref in res.depends_on:
                    self.edges.append((first, self._resolve_ref(ref, res)))
        # Deduplicate while preserving order (an explicit dependsOn may coincide
        # with the implicit sequential edge between adjacent steps).
        seen: set = set()
        deduped: List[tuple] = []
        for edge in self.edges:
            if edge not in seen:
                seen.add(edge)
                deduped.append(edge)
        self.edges = deduped

    # -- ordering ---------------------------------------------------------

    def _compute_order(self) -> List[str]:
        order: List[str] = []
        by_phase: Dict[int, List[str]] = defaultdict(list)
        for nid in self.nodes:
            by_phase[self.phase_idx[nid]].append(nid)

        for pidx in sorted(by_phase):
            phase_nodes = set(by_phase[pidx])
            intra: List[tuple] = []
            for dependent, dependency in self.edges:
                if dependent not in phase_nodes:
                    continue
                if dependency in phase_nodes:
                    intra.append((dependent, dependency))
                elif self.phase_idx[dependency] > pidx:
                    self.warnings.append(
                        f"{dependent} depends on {dependency} in a later phase; "
                        "ignoring (phase order takes precedence)"
                    )
                # earlier-phase dependency is already satisfied
            order.extend(self._topo(phase_nodes, intra))
        return order

    def _topo(self, nodes: set, edges: List[tuple]) -> List[str]:
        indeg = {n: 0 for n in nodes}
        adj: Dict[str, List[str]] = defaultdict(list)
        for dependent, dependency in edges:
            adj[dependency].append(dependent)
            indeg[dependent] += 1

        remaining = set(nodes)
        out: List[str] = []
        while remaining:
            avail = sorted((n for n in remaining if indeg[n] == 0),
                           key=lambda n: self.seq[n])
            if not avail:
                forced = min(remaining, key=lambda n: self.seq[n])
                self.warnings.append(
                    "circular dependency among "
                    f"{sorted(remaining, key=lambda n: self.seq[n])}; "
                    f"breaking at {forced}"
                )
                indeg[forced] = 0
                avail = [forced]
            nxt = avail[0]
            out.append(nxt)
            remaining.discard(nxt)
            for m in adj[nxt]:
                if m in remaining:
                    indeg[m] -= 1
        return out

    # -- resource-level views --------------------------------------------

    def _build_resource_views(self) -> None:
        self.resource_order: List[str] = []
        seen = set()
        for nid in self.node_order:
            r = self.node_res[nid]
            if r not in seen:
                seen.add(r)
                self.resource_order.append(r)
        self.last_node: Dict[str, str] = {}
        for nid in self.node_order:
            self.last_node[self.node_res[nid]] = nid

        self.res_depends: Dict[str, set] = defaultdict(set)
        for dependent, dependency in self.edges:
            ra, rb = self.node_res[dependent], self.node_res[dependency]
            if ra != rb:
                self.res_depends[ra].add(rb)
        self.res_dependents: Dict[str, set] = defaultdict(set)
        for r, deps in self.res_depends.items():
            for d in deps:
                self.res_dependents[d].add(r)

    def closure(self, names, *, dependents: bool) -> set:
        graph = self.res_dependents if dependents else self.res_depends
        out = set(names)
        stack = list(names)
        while stack:
            cur = stack.pop()
            for nxt in graph.get(cur, ()):  # noqa: B007
                if nxt not in out:
                    out.add(nxt)
                    stack.append(nxt)
        return out
