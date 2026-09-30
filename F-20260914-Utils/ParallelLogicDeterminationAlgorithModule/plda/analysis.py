"""Bounded, conservative analyses. SAFE always refers to the supplied complete IR."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from .model import Access, Instruction, Loop, MachineModel


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    reasons: tuple[str, ...]


@dataclass
class DependencyReport:
    order: list[str]
    edges: list[Edge]
    levels: list[list[str]]
    work: float
    span: float
    parallelism_bound: float
    unknown_pairs: list[tuple[str, str]] = field(default_factory=list)

    def predecessors(self) -> dict[str, set[str]]:
        result = {x: set() for x in self.order}
        for edge in self.edges:
            result[edge.target].add(edge.source)
        return result


def overlaps(a: Access, b: Access) -> bool:
    if a.resource is not None and b.resource is not None and a.resource != b.resource:
        return False
    # Unknown allocation also means unknown relative offsets.
    if a.resource is None or b.resource is None or a.start is None or b.start is None:
        return True
    return a.start < b.end and b.start < a.end


def conflicts(a: tuple[Access, ...], b: tuple[Access, ...]) -> set[str]:
    reasons = set()
    for left in a:
        for right in b:
            if overlaps(left, right) and "write" in {left.mode, right.mode}:
                kind = {("write", "read"): "RAW", ("read", "write"): "WAR",
                        ("write", "write"): "WAW"}[left.mode, right.mode]
                reasons.add(f"{kind}:{left.resource or '?'}:{right.resource or '?'}")
    return reasons


def dependencies(nodes, costs=None) -> DependencyReport:
    """Preserve reference-list order for hazards; explicit edges may use any order."""
    ids = [x.id for x in nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate operation ids")
    edges: dict[tuple[str, str], set[str]] = {}
    unknown = []
    for i, node in enumerate(nodes):
        for dep in node.depends_on:
            if dep not in ids or dep == node.id:
                raise ValueError(f"invalid dependency {dep} -> {node.id}")
            edges.setdefault((dep, node.id), set()).add("explicit")
        for previous in nodes[:i]:
            reasons = conflicts(previous.accesses, node.accesses)
            if not previous.effects_complete or not node.effects_complete:
                reasons.add("unknown-effects:preserve-order")
                unknown.append((previous.id, node.id))
            if previous.barrier or node.barrier:
                reasons.add("barrier:preserve-order")
            if reasons:
                edges.setdefault((previous.id, node.id), set()).update(reasons)
    pred = {x: set() for x in ids}
    for a, b in edges:
        pred[b].add(a)
    remaining = set(ids)
    order, levels, finish = [], [], {}
    costs = {x: 1.0 for x in ids} if costs is None else costs
    if set(costs) != set(ids) or any(not math.isfinite(v) or v <= 0 for v in costs.values()):
        raise ValueError("costs must cover exactly all operations with positive finite durations")
    while remaining:
        ready = [x for x in ids if x in remaining and not (pred[x] & remaining)]
        if not ready:
            raise ValueError("dependency cycle (including resource/control constraints)")
        levels.append(ready)
        for x in ready:
            finish[x] = costs[x] + max((finish[p] for p in pred[x]), default=0)
        order.extend(ready)
        remaining.difference_update(ready)
    work, span = sum(costs.values()), max(finish.values(), default=0)
    return DependencyReport(order, [Edge(a, b, tuple(sorted(r))) for (a, b), r in edges.items()],
                            levels, work, span, work / span if span else 0.0, unknown)


def analyze_tlp(operations, costs=None) -> DependencyReport:
    """Analyze arbitrary operation contracts without requiring executable tensor kernels."""
    return dependencies(operations, costs)


def analyze_ilp(instructions: tuple[Instruction, ...], machine: MachineModel | None = None) -> dict:
    machine = machine or MachineModel()
    graph = dependencies(instructions, {x.id: float(x.latency) for x in instructions})
    by_id = {x.id: x for x in instructions}
    for ins in instructions:
        if ins.unit not in machine.units:
            raise ValueError(f"instruction {ins.id}: missing execution unit {ins.unit}")
    pred = graph.predecessors()
    ports = {k: [0] * count for k, count in machine.units.items()}
    starts, finishes, schedule = {}, {}, []
    pending = set(graph.order)
    cycle = 0
    while pending:
        issued = 0
        for ident in graph.order:
            if ident not in pending:
                continue
            if any(p not in finishes or finishes[p] > cycle for p in pred[ident]):
                continue
            ins = by_id[ident]
            free = next((i for i, t in enumerate(ports[ins.unit]) if t <= cycle), None)
            if free is None or issued == machine.issue_width:
                continue
            starts[ident], finishes[ident] = cycle, cycle + ins.latency
            ports[ins.unit][free] = cycle + ins.occupancy
            schedule.append({"id": ident, "issue": cycle, "finish": finishes[ident],
                             "unit": ins.unit, "port": free})
            pending.remove(ident)
            issued += 1
        if pending:
            # Event jump avoids scanning huge latency ranges one cycle at a time.
            if issued:
                cycle += 1
            else:
                future = [t for t in finishes.values() if t > cycle]
                future += [t for ts in ports.values() for t in ts if t > cycle]
                cycle = min(future) if future else cycle + 1
    cycles = max(finishes.values(), default=0)
    return {"scope": "declared basic-block IR; conservative list scheduling; no machine-code emission",
            "graph": graph, "schedule": schedule, "cycles": cycles,
            "ipc": len(instructions) / cycles if cycles else 0,
            "same_cycle_groups": [[x.id for x in instructions if starts[x.id] == c]
                                  for c in sorted(set(starts.values()))],
            "assumptions": ["complete register/flag/memory effects and control barriers",
                            "declared latencies/units; no cache-miss, branch or ROB simulation",
                            "WAR/WAW retained; supply verified SSA names to express renaming"]}


def analyze_simd(loop: Loop, max_access_bytes: int = 250_000) -> dict:
    """Exact finite affine byte-access check, bounded before allocating its footprint map.

    This checks full iteration independence, a sufficient condition for ordinary
    loop widening. Dependence-preserving wavefront/scan transformations are out of scope.
    """
    if max_access_bytes < 1:
        raise ValueError("analysis budget must be positive")
    result = {"id": loop.id, "verdict": "unknown", "reason": "", "witness": None,
              "vector_width": loop.vector_width, "vector_iterations": 0, "scalar_tail": 0,
              "profitable": None, "scope": "ordinary loop widening with independent iterations"}

    def answer(verdict, reason, witness=None):
        return {**result, "verdict": verdict, "reason": reason, "witness": witness}

    if not loop.effects_complete or loop.control == "unknown":
        return answer("unknown", "effects or control flow are incomplete")
    if loop.control == "masked" and not loop.target_supports_masks:
        return answer("unknown", "masked control requires target predication semantics")
    if loop.trip_count is None:
        return answer("unknown", "trip count is unknown; specialize after a runtime guard")
    n = loop.trip_count
    result.update(vector_iterations=n // loop.vector_width, scalar_tail=n % loop.vector_width)
    if n <= 1:
        return answer("safe", "at most one iteration; no useful iteration parallelism")
    if loop.reduction is not None:
        if loop.reduction not in {"sum", "product"}:
            return answer("unknown", "unsupported reduction algebra")
        if loop.dtype.startswith("float") and not loop.allow_reassociation:
            return answer("unsafe", "this reduction scheme reorders strict floating-point operations")
    if any(a.resource is None or a.stride is None for a in loop.accesses):
        return answer("unknown", "unresolved alias or non-affine address")
    # Constant-time-in-trip-count proof for equal-stride accesses, including
    # in-place elementwise loops and negative strides with nonnegative bounds.
    pair_work = sum(a.width * b.width for a in loop.accesses for b in loop.accesses
                    if a.resource == b.resource and "write" in {a.mode, b.mode})
    unresolved = False
    if pair_work <= max_access_bytes:
        for ai, a in enumerate(loop.accesses):
            if min(a.offset, a.stride * (n - 1) + a.offset) < 0:
                return answer("unknown", "negative address: bounds/allocation proof required")
            for bi in range(ai, len(loop.accesses)):
                b = loop.accesses[bi]
                if a.resource != b.resource or "write" not in {a.mode, b.mode}:
                    continue
                if a.stride != b.stride:
                    unresolved = True
                    continue
                for ab in range(a.width):
                    for bb in range(b.width):
                        difference = b.offset + bb - a.offset - ab
                        if a.stride == 0:
                            delta = 1 if difference == 0 else n
                        elif difference % a.stride:
                            continue
                        else:
                            delta = difference // a.stride  # i - j
                        if delta and abs(delta) < n:
                            i, j = max(delta, 0), max(-delta, 0)
                            return answer("unsafe", "loop-carried affine dependence", {
                                "resource": a.resource, "byte": a.stride * i + a.offset + ab,
                                "iterations": [i, j], "accesses": [ai, bi], "modes": [a.mode, b.mode]})
        if not unresolved:
            return answer("safe", "equal-stride affine proof excludes all cross-iteration write conflicts" +
                          ("; reduction uses the declared reassociation contract" if loop.reduction else ""))
    if n * sum(a.width for a in loop.accesses) > max_access_bytes:
        return answer("unknown", "exact analysis budget exceeded; no safety claim")
    # Keep the earliest reader/writer iteration for each byte. Same-iteration
    # reads and writes are legal because statement order within a lane is retained.
    seen: dict[tuple[str, int], dict[str, tuple[int, int]]] = {}
    for iteration in range(n):
        for index, access in enumerate(loop.accesses):
            begin = access.stride * iteration + access.offset
            if begin < 0:
                return answer("unknown", "negative address: bounds/allocation proof required")
            for byte in range(begin, begin + access.width):
                history = seen.setdefault((access.resource, byte), {})
                modes = ("write", "read") if access.mode == "write" else ("write",)
                for mode in modes:
                    if mode in history and history[mode][0] != iteration:
                        old_i, old_a = history[mode]
                        return answer("unsafe", "loop-carried memory dependence", {
                            "resource": access.resource, "byte": byte,
                            "iterations": [old_i, iteration], "accesses": [old_a, index],
                            "modes": [mode, access.mode]})
                history.setdefault(access.mode, (iteration, index))
    return answer("safe", "no cross-iteration write conflict in the complete finite footprint" +
                  ("; reduction uses the declared reassociation contract" if loop.reduction else ""))
